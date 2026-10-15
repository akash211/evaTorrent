//! UDP BitTorrent tracker client (BEP 0015).

use std::time::Duration;

use rand::Rng;
use tokio::net::{lookup_host, UdpSocket};
use tokio::time::timeout;
use tracing::debug;
use url::Url;

use crate::tracker::http::parse_compact_peers;
use crate::tracker::TrackerResponse;

const PROTOCOL_MAGIC: u64 = 0x0417_2710_1980;
const ACTION_CONNECT: u32 = 0;
const ACTION_ANNOUNCE: u32 = 1;
const ACTION_ERROR: u32 = 3;

#[derive(Debug, Clone)]
pub struct UdpTracker {
    pub url: String,
    pub hostname: String,
    pub port: u16,
}

impl UdpTracker {
    pub fn new(announce_url: String) -> Self {
        let parsed = Url::parse(&announce_url).ok();
        let hostname = parsed
            .as_ref()
            .and_then(|u| u.host_str().map(|s| s.to_string()))
            .unwrap_or_default();
        let port = parsed.as_ref().and_then(|u| u.port()).unwrap_or(80);
        Self {
            url: announce_url,
            hostname,
            port,
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn announce(
        &self,
        info_hash: &[u8; 20],
        peer_id: &[u8; 20],
        port: u16,
        uploaded: u64,
        downloaded: u64,
        left: u64,
        event: Option<&str>,
        timeout_secs: f64,
    ) -> Option<TrackerResponse> {
        if self.hostname.is_empty() {
            return None;
        }

        let target = format!("{}:{}", self.hostname, self.port);
        let remote_addr = timeout(Duration::from_secs(4), lookup_host(&target))
            .await
            .ok()?
            .ok()?
            .find(|a| a.is_ipv4())?;

        let socket = UdpSocket::bind("0.0.0.0:0").await.ok()?;
        socket.connect(remote_addr).await.ok()?;

        let half_timeout = Duration::from_secs_f64((timeout_secs / 2.0).max(2.0));

        // Step 1: Connect Request (16 bytes)
        let trans_id: u32 = rand::thread_rng().gen_range(0..=0x7FFF_FFFF);
        let mut connect_req = [0u8; 16];
        connect_req[0..8].copy_from_slice(&PROTOCOL_MAGIC.to_be_bytes());
        connect_req[8..12].copy_from_slice(&ACTION_CONNECT.to_be_bytes());
        connect_req[12..16].copy_from_slice(&trans_id.to_be_bytes());

        socket.send(&connect_req).await.ok()?;

        let mut conn_resp = [0u8; 64];
        let n = timeout(half_timeout, socket.recv(&mut conn_resp))
            .await
            .ok()?
            .ok()?;
        if n < 16 {
            return None;
        }
        let action = u32::from_be_bytes([conn_resp[0], conn_resp[1], conn_resp[2], conn_resp[3]]);
        let resp_tid = u32::from_be_bytes([conn_resp[4], conn_resp[5], conn_resp[6], conn_resp[7]]);
        if action != ACTION_CONNECT || resp_tid != trans_id {
            return None;
        }
        let connection_id = u64::from_be_bytes([
            conn_resp[8],
            conn_resp[9],
            conn_resp[10],
            conn_resp[11],
            conn_resp[12],
            conn_resp[13],
            conn_resp[14],
            conn_resp[15],
        ]);

        // Step 2: Announce Request (98 bytes)
        let event_code: u32 = match event {
            Some("completed") => 1,
            Some("started") => 2,
            Some("stopped") => 3,
            _ => 0,
        };
        let ann_tid: u32 = rand::thread_rng().gen_range(0..=0x7FFF_FFFF);
        let key: u32 = rand::thread_rng().gen_range(0..=0x7FFF_FFFF);
        let num_want: i32 = 50;

        let mut ann_req = [0u8; 98];
        ann_req[0..8].copy_from_slice(&connection_id.to_be_bytes());
        ann_req[8..12].copy_from_slice(&ACTION_ANNOUNCE.to_be_bytes());
        ann_req[12..16].copy_from_slice(&ann_tid.to_be_bytes());
        ann_req[16..36].copy_from_slice(info_hash);
        ann_req[36..56].copy_from_slice(peer_id);
        ann_req[56..64].copy_from_slice(&downloaded.to_be_bytes());
        ann_req[64..72].copy_from_slice(&left.to_be_bytes());
        ann_req[72..80].copy_from_slice(&uploaded.to_be_bytes());
        ann_req[80..84].copy_from_slice(&event_code.to_be_bytes());
        ann_req[84..88].copy_from_slice(&0u32.to_be_bytes()); // IP default 0
        ann_req[88..92].copy_from_slice(&key.to_be_bytes());
        ann_req[92..96].copy_from_slice(&num_want.to_be_bytes());
        ann_req[96..98].copy_from_slice(&port.to_be_bytes());

        socket.send(&ann_req).await.ok()?;

        let mut ann_resp = [0u8; 2048];
        let n_ann = timeout(half_timeout, socket.recv(&mut ann_resp))
            .await
            .ok()?
            .ok()?;
        if n_ann < 20 {
            return None;
        }
        let ann_action = u32::from_be_bytes([ann_resp[0], ann_resp[1], ann_resp[2], ann_resp[3]]);
        let ann_resp_tid = u32::from_be_bytes([ann_resp[4], ann_resp[5], ann_resp[6], ann_resp[7]]);

        if ann_action == ACTION_ERROR {
            let err_msg = String::from_utf8_lossy(&ann_resp[8..n_ann]);
            debug!("UDP tracker {} returned error: {}", self.url, err_msg);
            return None;
        }
        if ann_action != ACTION_ANNOUNCE || ann_resp_tid != ann_tid {
            return None;
        }

        let interval = u32::from_be_bytes([ann_resp[8], ann_resp[9], ann_resp[10], ann_resp[11]]);
        let leechers = u32::from_be_bytes([ann_resp[12], ann_resp[13], ann_resp[14], ann_resp[15]]);
        let seeders = u32::from_be_bytes([ann_resp[16], ann_resp[17], ann_resp[18], ann_resp[19]]);

        let peers = parse_compact_peers(&ann_resp[20..n_ann]);
        Some(TrackerResponse {
            interval,
            peers,
            complete: seeders,
            incomplete: leechers,
            failure_reason: None,
        })
    }
}
