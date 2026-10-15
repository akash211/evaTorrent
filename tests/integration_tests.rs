//! Comprehensive unit and integration test suite for evaTorrent (Rust).

use sha1::{Digest, Sha1};
use std::collections::{BTreeMap, HashMap};
use tempfile::tempdir;

use evatorrent::auth::{AuthConfig, OTPManager, SessionManager};
use evatorrent::bencoding::{bdecode, bencode, BencodeError, BencodeValue};
use evatorrent::db::Database;
use evatorrent::engine::speed::{
    match_schedule_at, validate_profiles, validate_schedule, ScheduleEntry, SpeedProfile,
};
use evatorrent::peer::protocol::{decode_message, Bitfield, Handshake, PeerMessage};
use evatorrent::search::health::compute_health_score;
use evatorrent::search::service::{apply_audio_language_filter, get_query_variations};
use evatorrent::storage::manager::{PieceManager, PRIO_HIGH, PRIO_SKIP};
use evatorrent::subtitles::service::{clean_video_title, extract_subtitle_from_bytes};
use evatorrent::torrent::{Magnet, Torrent};

fn make_sample_torrent_bytes(multi_file: bool) -> Vec<u8> {
    let piece_data = vec![b'A'; 16384];
    let mut hasher = Sha1::new();
    hasher.update(&piece_data);
    let piece_hash = hasher.finalize().to_vec();

    let mut info = BTreeMap::new();
    info.insert(
        b"name".to_vec(),
        BencodeValue::Bytes(b"sample_media".to_vec()),
    );
    info.insert(b"piece length".to_vec(), BencodeValue::Int(16384));
    info.insert(b"pieces".to_vec(), BencodeValue::Bytes(piece_hash));

    if multi_file {
        let mut f1 = BTreeMap::new();
        f1.insert(b"length".to_vec(), BencodeValue::Int(8192));
        f1.insert(
            b"path".to_vec(),
            BencodeValue::List(vec![BencodeValue::Bytes(b"ep1.mp4".to_vec())]),
        );
        let mut f2 = BTreeMap::new();
        f2.insert(b"length".to_vec(), BencodeValue::Int(8192));
        f2.insert(
            b"path".to_vec(),
            BencodeValue::List(vec![BencodeValue::Bytes(b"ep2.mp4".to_vec())]),
        );
        info.insert(
            b"files".to_vec(),
            BencodeValue::List(vec![BencodeValue::Dict(f1), BencodeValue::Dict(f2)]),
        );
    } else {
        info.insert(b"length".to_vec(), BencodeValue::Int(16384));
    }

    let mut root = BTreeMap::new();
    root.insert(
        b"announce".to_vec(),
        BencodeValue::Bytes(b"http://tracker.example.com:80/announce".to_vec()),
    );
    root.insert(b"info".to_vec(), BencodeValue::Dict(info));
    bencode(&BencodeValue::Dict(root))
}

#[test]
fn test_bencoding_roundtrip_and_strict_validation() {
    assert_eq!(bdecode(b"i42e").unwrap(), BencodeValue::Int(42));
    assert_eq!(bdecode(b"i-42e").unwrap(), BencodeValue::Int(-42));
    assert_eq!(bdecode(b"i0e").unwrap(), BencodeValue::Int(0));
    assert_eq!(bdecode(b"i-0e"), Err(BencodeError::NegativeZero));
    assert_eq!(bdecode(b"i03e"), Err(BencodeError::LeadingZero));
    assert_eq!(
        bdecode(b"4:spam").unwrap(),
        BencodeValue::Bytes(b"spam".to_vec())
    );

    let raw = b"d3:bar4:spam3:fooi42ee";
    let decoded = bdecode(raw).unwrap();
    assert_eq!(bencode(&decoded), raw);
}

#[test]
fn test_torrent_and_magnet_parsing() {
    let bytes = make_sample_torrent_bytes(true);
    let t = Torrent::from_bytes(bytes).unwrap();
    assert_eq!(t.name, "sample_media");
    assert!(t.is_multi_file);
    assert_eq!(t.total_length, 16384);
    assert_eq!(t.piece_count, 1);
    assert_eq!(t.files.len(), 2);

    let mag_hex =
        Magnet::parse("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Ubuntu+ISO&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337").unwrap();
    assert_eq!(
        mag_hex.info_hash_hex,
        "0123456789abcdef0123456789abcdef01234567"
    );
    assert_eq!(mag_hex.name.as_deref(), Some("Ubuntu ISO"));
    assert_eq!(mag_hex.trackers.len(), 1);
}

#[test]
fn test_peer_wire_protocol_and_compact_bitfield() {
    let ih = [0x11u8; 20];
    let pid = [0x22u8; 20];
    let hs = Handshake::new(ih, pid);
    let encoded = hs.encode();
    let decoded = Handshake::decode(&encoded).unwrap();
    assert_eq!(decoded.info_hash, ih);
    assert_eq!(decoded.peer_id, pid);

    let mut bf = Bitfield::with_piece_count(100);
    assert!(!bf.has_piece(0));
    bf.set_piece(0);
    bf.set_piece(63);
    bf.set_piece(99);
    assert!(bf.has_piece(0));
    assert!(bf.has_piece(63));
    assert!(bf.has_piece(99));
    assert!(!bf.has_piece(50));

    let req = PeerMessage::Request {
        index: 3,
        begin: 16384,
        length: 16384,
    };
    let wire = req.encode();
    let parsed = decode_message(13, wire[4], &wire[5..]).unwrap();
    assert_eq!(parsed, req);
}

#[test]
fn test_piece_manager_priorities_and_sequential_mode() {
    let dir = tempdir().unwrap();
    let t = Torrent::from_bytes(make_sample_torrent_bytes(true)).unwrap();
    let pm = PieceManager::new(t, dir.path(), None, None, None).unwrap();

    assert!(!pm.is_sequential());
    pm.set_sequential(true);
    assert!(pm.is_sequential());

    let mut prios = HashMap::new();
    prios.insert("sample_media/ep1.mp4".to_string(), PRIO_HIGH);
    prios.insert("sample_media/ep2.mp4".to_string(), PRIO_SKIP);
    let summary = pm.set_file_priorities(prios).unwrap();
    assert_eq!(summary.get("selected_count").unwrap().as_u64(), Some(1));
    assert_eq!(
        summary.get("high_priority_count").unwrap().as_u64(),
        Some(1)
    );

    // Verify block data and SHA-1 completion
    pm.peer_has_all_pieces("127.0.0.1:6881");
    let reqs = pm.next_requests("127.0.0.1:6881", 4);
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].piece_index, 0);

    let block_payload = vec![b'A'; 16384];
    assert!(pm.on_block_received(0, 0, &block_payload));
    assert!(pm.is_complete());
    assert_eq!(pm.progress_percentage(), 100.0);
}

#[test]
fn test_database_and_auth_persistence() {
    let dir = tempdir().unwrap();
    let db = Database::new(dir.path().join("eva.db")).unwrap();

    db.upsert_torrent(
        "0123456789abcdef0123456789abcdef01234567",
        "Linux ISO",
        1048576,
        "/downloads",
        "downloading",
        Some("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"),
    );
    db.mark_torrent_completed("0123456789abcdef0123456789abcdef01234567", 1048576, 524288);

    let summary = db.get_analytics_summary();
    assert_eq!(summary.total_torrents, 1);
    assert_eq!(summary.completed_torrents, 1);
    assert_eq!(summary.success_rate, 100.0);

    let auth_cfg = AuthConfig::new(Some(dir.path().to_path_buf()));
    auth_cfg.set_admin_email("admin@example.com");
    let sess_mgr = SessionManager::new(auth_cfg);
    let token = sess_mgr.create_token("admin@example.com", 3600);
    assert_eq!(
        sess_mgr.verify_token(&token).as_deref(),
        Some("admin@example.com")
    );

    let otp_mgr = OTPManager::new(600.0, Some(db));
    let (ok, _, code) = otp_mgr.generate_otp("admin@example.com");
    assert!(ok);
    assert!(otp_mgr.verify_otp("admin@example.com", &code.unwrap()));
}

#[test]
fn test_speed_scheduler_and_subtitles() {
    let profiles = validate_profiles(vec![
        SpeedProfile {
            name: "Day".to_string(),
            limit: Some(5 * 1024 * 1024),
        },
        SpeedProfile {
            name: "Night".to_string(),
            limit: None,
        },
    ])
    .unwrap();
    let names: Vec<String> = profiles.iter().map(|p| p.name.clone()).collect();
    let sched = validate_schedule(
        vec![ScheduleEntry {
            profile: "Night".to_string(),
            start: "22:00".to_string(),
            end: "07:00".to_string(),
            days: vec![0, 1, 2, 3, 4, 5, 6],
        }],
        &names,
    )
    .unwrap();

    // 23:30 (1410m) is inside 22:00-07:00
    assert_eq!(
        match_schedule_at(&sched, 0, 23 * 60 + 30).as_deref(),
        Some("Night")
    );
    // 14:00 (840m) is outside 22:00-07:00
    assert_eq!(match_schedule_at(&sched, 0, 14 * 60), None);

    let (title, year, season, ep) =
        clean_video_title("Breaking.Bad.S02E05.2009.1080p.BluRay.x264-GROUP.mkv");
    assert_eq!(title, "Breaking Bad");
    assert_eq!(year, Some(2009));
    assert_eq!(season, Some(2));
    assert_eq!(ep, Some(5));

    let valid_srt = b"1\n00:00:01,000 --> 00:00:04,000\nHello world!\n";
    let (extracted, ext) =
        extract_subtitle_from_bytes(valid_srt, "https://example.com/sub.srt").unwrap();
    assert_eq!(ext, ".srt");
    assert_eq!(extracted, valid_srt);

    assert!(compute_health_score(500, 50, 5, 1_000_000_000) > 0.7);
    assert_eq!(compute_health_score(0, 0, 10, 1000), 0.0);
    assert!(!get_query_variations("The Neighbour").is_empty());
    let (_, applied) = apply_audio_language_filter(Vec::new(), false, false);
    assert!(!applied);
}
