//! Zero-copy asynchronous stream framer for BitTorrent peer wire messages.

use bytes::{Buf, BytesMut};
use tokio::io::{AsyncRead, AsyncReadExt};
use tracing::debug;

use crate::peer::protocol::{decode_message, PeerMessage};

pub struct PeerStreamIterator<R> {
    reader: R,
    buffer: BytesMut,
    buffer_limit: usize,
}

impl<R: AsyncRead + Unpin> PeerStreamIterator<R> {
    pub fn new(reader: R, buffer_limit: usize) -> Self {
        Self {
            reader,
            buffer: BytesMut::with_capacity(65536),
            buffer_limit,
        }
    }

    pub async fn next_message(&mut self) -> Result<Option<PeerMessage>, String> {
        loop {
            if self.buffer.len() >= 4 {
                let length = u32::from_be_bytes([
                    self.buffer[0],
                    self.buffer[1],
                    self.buffer[2],
                    self.buffer[3],
                ]) as usize;

                if length == 0 {
                    self.buffer.advance(4);
                    return Ok(Some(PeerMessage::KeepAlive));
                }

                let total_len = 4usize.saturating_add(length);
                if total_len > self.buffer_limit {
                    return Err(format!(
                        "Peer message length ({}) exceeds buffer limit ({})",
                        total_len, self.buffer_limit
                    ));
                }

                if self.buffer.len() >= total_len {
                    let frame = self.buffer.split_to(total_len);
                    let msg_id = frame[4];
                    let payload = &frame[5..];
                    if let Some(msg) = decode_message(length, msg_id, payload) {
                        return Ok(Some(msg));
                    } else {
                        debug!("Skipping unknown or malformed peer message ID {}", msg_id);
                        continue;
                    }
                }
            }

            let n = self
                .reader
                .read_buf(&mut self.buffer)
                .await
                .map_err(|e| format!("Socket read error: {}", e))?;
            if n == 0 {
                return Ok(None);
            }
            if self.buffer.len() > self.buffer_limit {
                return Err("Peer stream buffer exceeded maximum limit".to_string());
            }
        }
    }
}
