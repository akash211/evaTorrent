//! HTTP and HTTPS BitTorrent tracker client (BEP 0003).

use std::net::Ipv4Addr;
use std::time::Duration;

use tracing::{debug, warn};

use crate::bencoding::{bdecode, BencodeValue};
use crate::torrent::urlencoding_encode_bytes;
use crate::tracker::{Peer, TrackerResponse};

/// Parses compact binary peer list (6 bytes per peer: 4-byte IPv4 + 2-byte big-endian port).
pub fn parse_compact_peers(data: &[u8]) -> Vec<Peer> {
    if !data.len().is_multiple_of(6) {
        warn!("Malformed compact peer list length: {}", data.len());
    }
    let mut peers = Vec::with_capacity(data.len() / 6);
    for chunk in data.as_chunks::<6>().0 {
        let ip = Ipv4Addr::new(chunk[0], chunk[1], chunk[2], chunk[3]).to_string();
        let port = u16::from_be_bytes([chunk[4], chunk[5]]);
        if port > 0 {
            peers.push(Peer { ip, port });
        }
    }
    peers
}

#[derive(Debug, Clone)]
pub struct HttpTracker {
    pub url: String,
}

impl HttpTracker {
    pub fn new(announce_url: String) -> Self {
        Self { url: announce_url }
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
        let mut params = vec![
            format!("info_hash={}", urlencoding_encode_bytes(info_hash)),
            format!("peer_id={}", urlencoding_encode_bytes(peer_id)),
            format!("port={}", port),
            format!("uploaded={}", uploaded),
            format!("downloaded={}", downloaded),
            format!("left={}", left),
            "compact=1".to_string(),
            "numwant=100".to_string(),
        ];
        if let Some(ev) = event {
            if !ev.is_empty() {
                params.push(format!("event={}", ev));
            }
        }
        let delim = if self.url.contains('?') { "&" } else { "?" };
        let full_url = format!("{}{}{}", self.url, delim, params.join("&"));

        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs_f64(timeout_secs))
            .user_agent("evaTorrent/1.0.0")
            .build()
            .ok()?;

        let resp = match client.get(&full_url).send().await {
            Ok(r) => r,
            Err(e) => {
                debug!("Error querying HTTP tracker {}: {}", self.url, e);
                return None;
            }
        };

        if !resp.status().is_success() {
            debug!(
                "HTTP tracker {} responded with status {}",
                self.url,
                resp.status()
            );
            return None;
        }

        let body = resp.bytes().await.ok()?;
        let decoded = bdecode(&body).ok()?;
        let dict = decoded.as_dict()?;

        if let Some(reason) = dict
            .get(b"failure reason".as_slice())
            .and_then(|v| v.as_str())
        {
            debug!("Tracker failure from {}: {}", self.url, reason);
            return Some(TrackerResponse {
                interval: 1800,
                peers: Vec::new(),
                complete: 0,
                incomplete: 0,
                failure_reason: Some(reason),
            });
        }

        let interval = dict
            .get(b"interval".as_slice())
            .and_then(|v| v.as_int())
            .unwrap_or(1800)
            .max(0) as u32;
        let complete = dict
            .get(b"complete".as_slice())
            .and_then(|v| v.as_int())
            .unwrap_or(0)
            .max(0) as u32;
        let incomplete = dict
            .get(b"incomplete".as_slice())
            .and_then(|v| v.as_int())
            .unwrap_or(0)
            .max(0) as u32;

        let mut peers = Vec::new();
        match dict.get(b"peers".as_slice()) {
            Some(BencodeValue::Bytes(raw)) => {
                peers = parse_compact_peers(raw);
            }
            Some(BencodeValue::List(list)) => {
                for item in list {
                    if let Some(p_dict) = item.as_dict() {
                        let ip = p_dict
                            .get(b"ip".as_slice())
                            .and_then(|v| v.as_str())
                            .unwrap_or_default();
                        let p_port = p_dict
                            .get(b"port".as_slice())
                            .and_then(|v| v.as_int())
                            .unwrap_or(0);
                        if !ip.is_empty() && p_port > 0 && p_port <= 65535 {
                            peers.push(Peer {
                                ip,
                                port: p_port as u16,
                            });
                        }
                    }
                }
            }
            _ => {}
        }

        Some(TrackerResponse {
            interval,
            peers,
            complete,
            incomplete,
            failure_reason: None,
        })
    }
}
