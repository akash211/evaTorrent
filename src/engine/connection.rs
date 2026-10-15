//! Peer connection worker managing async TCP socket, handshake, and pipelined block requests.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::Semaphore;
use tokio::task::JoinHandle;
use tokio::time::timeout;
use tracing::{debug, warn};

use crate::db::now_unix_secs;
use crate::engine::limits::{pipeline_per_peer, stream_buffer_limit};
use crate::peer::protocol::{Handshake, PeerMessage, HANDSHAKE_LEN};
use crate::peer::stream::PeerStreamIterator;
use crate::storage::manager::PieceManager;
use crate::tracker::Peer;

pub struct PeerConnection {
    pub peer: Peer,
    pub peer_key: String,
    pub info_hash: [u8; 20],
    pub peer_id: [u8; 20],
    pub piece_manager: PieceManager,
    pub is_choked: AtomicBool,
    pub am_interested: AtomicBool,
    pub is_connected: AtomicBool,
    pub running: AtomicBool,
    pub bytes_downloaded: AtomicU64,
    pub bytes_uploaded: AtomicU64,
    speed_state: Mutex<(f64, f64, f64, u64, u64)>, // (dl_speed, ul_speed, last_check, dl_since, ul_since)
    pending_requests: Mutex<HashMap<(usize, u32), f64>>,
    writer_tx: Mutex<Option<tokio::sync::mpsc::UnboundedSender<Vec<u8>>>>,
    task_handle: Mutex<Option<JoinHandle<()>>>,
    on_disconnect: Option<Arc<dyn Fn(&str) + Send + Sync>>,
    is_throttled: Option<Arc<dyn Fn() -> bool + Send + Sync>>,
    on_data_received: Option<Arc<dyn Fn() + Send + Sync>>,
}

impl PeerConnection {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        peer: Peer,
        info_hash: [u8; 20],
        peer_id: [u8; 20],
        piece_manager: PieceManager,
        on_disconnect: Option<Arc<dyn Fn(&str) + Send + Sync>>,
        is_throttled: Option<Arc<dyn Fn() -> bool + Send + Sync>>,
        on_data_received: Option<Arc<dyn Fn() + Send + Sync>>,
    ) -> Arc<Self> {
        let peer_key = format!("{}:{}", peer.ip, peer.port);
        Arc::new(Self {
            peer,
            peer_key,
            info_hash,
            peer_id,
            piece_manager,
            is_choked: AtomicBool::new(true),
            am_interested: AtomicBool::new(false),
            is_connected: AtomicBool::new(false),
            running: AtomicBool::new(false),
            bytes_downloaded: AtomicU64::new(0),
            bytes_uploaded: AtomicU64::new(0),
            speed_state: Mutex::new((0.0, 0.0, now_unix_secs(), 0, 0)),
            pending_requests: Mutex::new(HashMap::new()),
            writer_tx: Mutex::new(None),
            task_handle: Mutex::new(None),
            on_disconnect,
            is_throttled,
            on_data_received,
        })
    }

    pub fn start(self: &Arc<Self>) {
        self.running.store(true, Ordering::SeqCst);
        let me = Arc::clone(self);
        let handle = tokio::spawn(async move {
            me.run_loop().await;
        });
        *self.task_handle.lock().unwrap() = Some(handle);
    }

    pub async fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        if let Some(h) = self.task_handle.lock().unwrap().take() {
            h.abort();
        }
        self.close_cleanup();
    }

    fn close_cleanup(&self) {
        self.is_connected.store(false, Ordering::SeqCst);
        self.piece_manager.remove_peer(&self.peer_key);
        self.pending_requests.lock().unwrap().clear();
        *self.writer_tx.lock().unwrap() = None;
        if let Some(ref cb) = self.on_disconnect {
            cb(&self.peer_key);
        }
    }

    pub fn send_message(&self, msg: &PeerMessage) {
        if let Some(ref tx) = *self.writer_tx.lock().unwrap() {
            let _ = tx.send(msg.encode());
        }
    }

    pub fn download_speed(&self) -> f64 {
        self.speed_state.lock().unwrap().0
    }

    pub fn upload_speed(&self) -> f64 {
        self.speed_state.lock().unwrap().1
    }

    pub fn update_speed(&self) {
        let now = now_unix_secs();
        let mut st = self.speed_state.lock().unwrap();
        let elapsed = now - st.2;
        if elapsed >= 1.0 {
            st.0 = (st.3 as f64) / elapsed;
            st.1 = (st.4 as f64) / elapsed;
            st.2 = now;
            st.3 = 0;
            st.4 = 0;
        }
    }

    async fn run_loop(self: Arc<Self>) {
        let addr = format!("{}:{}", self.peer.ip, self.peer.port);
        let Ok(Ok(mut stream)) = timeout(Duration::from_secs(8), TcpStream::connect(&addr)).await
        else {
            self.close_cleanup();
            return;
        };

        // Send Handshake
        let hs = Handshake::new(self.info_hash, self.peer_id);
        if stream.write_all(&hs.encode()).await.is_err() {
            self.close_cleanup();
            return;
        }

        // Read remote Handshake
        let mut raw_hs = [0u8; HANDSHAKE_LEN];
        if timeout(Duration::from_secs(8), stream.read_exact(&mut raw_hs))
            .await
            .is_err()
        {
            self.close_cleanup();
            return;
        }
        let Ok(remote_hs) = Handshake::decode(&raw_hs) else {
            self.close_cleanup();
            return;
        };
        if remote_hs.info_hash != self.info_hash {
            debug!("Info hash mismatch from {}", self.peer_key);
            self.close_cleanup();
            return;
        }

        self.is_connected.store(true, Ordering::SeqCst);

        let (read_half, mut write_half) = stream.into_split();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Vec<u8>>();
        *self.writer_tx.lock().unwrap() = Some(tx);

        // Spawn writer task
        let writer_handle = tokio::spawn(async move {
            while let Some(bytes) = rx.recv().await {
                if write_half.write_all(&bytes).await.is_err() {
                    break;
                }
            }
        });

        // Send Bitfield if we have any pieces
        let our_bf = self.piece_manager.get_bitfield();
        if our_bf.is_any_set() {
            self.send_message(&PeerMessage::Bitfield(our_bf));
        }

        // Send Interested
        self.send_message(&PeerMessage::Interested);
        self.am_interested.store(true, Ordering::SeqCst);

        // Spawn request pipelining loop
        let req_me = Arc::clone(&self);
        let req_handle = tokio::spawn(async move {
            req_me.request_blocks_loop().await;
        });

        let upload_sem = Arc::new(Semaphore::new(2));
        let mut framer = PeerStreamIterator::new(read_half, stream_buffer_limit());

        loop {
            if !self.running.load(Ordering::SeqCst) || self.piece_manager.is_complete() {
                break;
            }
            match framer.next_message().await {
                Ok(Some(msg)) => {
                    self.handle_message(msg, &upload_sem);
                }
                Ok(None) => break,
                Err(e) => {
                    warn!("Peer stream error with {}: {}", self.peer_key, e);
                    break;
                }
            }
        }

        req_handle.abort();
        writer_handle.abort();
        self.close_cleanup();
    }

    fn handle_message(&self, msg: PeerMessage, upload_sem: &Arc<Semaphore>) {
        match msg {
            PeerMessage::Choke => {
                self.is_choked.store(true, Ordering::SeqCst);
                self.pending_requests.lock().unwrap().clear();
            }
            PeerMessage::Unchoke => {
                self.is_choked.store(false, Ordering::SeqCst);
                if !self.piece_manager.has_peer(&self.peer_key) {
                    self.piece_manager.peer_has_all_pieces(&self.peer_key);
                }
            }
            PeerMessage::Have { piece_index } => {
                self.piece_manager
                    .peer_has_piece(&self.peer_key, piece_index as usize);
            }
            PeerMessage::Bitfield(bf) => {
                self.piece_manager.add_peer(&self.peer_key, bf);
            }
            PeerMessage::Piece {
                index,
                begin,
                block,
            } => {
                let len = block.len() as u64;
                self.bytes_downloaded.fetch_add(len, Ordering::Relaxed);
                self.speed_state.lock().unwrap().3 += len;
                self.pending_requests
                    .lock()
                    .unwrap()
                    .remove(&(index as usize, begin));
                if let Some(ref cb) = self.on_data_received {
                    cb();
                }
                self.piece_manager
                    .on_block_received(index as usize, begin, &block);
            }
            PeerMessage::Request {
                index,
                begin,
                length,
            } => {
                if let Ok(permit) = upload_sem.clone().try_acquire_owned() {
                    if let Some(block_data) =
                        self.piece_manager.read_block(index as usize, begin, length)
                    {
                        let len = block_data.len() as u64;
                        self.bytes_uploaded.fetch_add(len, Ordering::Relaxed);
                        self.speed_state.lock().unwrap().4 += len;
                        self.send_message(&PeerMessage::Piece {
                            index,
                            begin,
                            block: block_data,
                        });
                    }
                    drop(permit);
                }
            }
            _ => {}
        }
    }

    async fn request_blocks_loop(&self) {
        let pipeline = pipeline_per_peer();
        let mut idle_ticks = 0u32;

        while self.running.load(Ordering::SeqCst) && !self.piece_manager.is_complete() {
            if !self.is_connected.load(Ordering::SeqCst) || self.is_choked.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_millis(500)).await;
                continue;
            }

            if let Some(ref throttle_fn) = self.is_throttled {
                if throttle_fn() {
                    tokio::time::sleep(Duration::from_millis(50)).await;
                    continue;
                }
            }

            let now = now_unix_secs();
            let available_slots = {
                let mut pending = self.pending_requests.lock().unwrap();
                pending.retain(|_, &mut ts| now - ts < 10.0);
                pipeline.saturating_sub(pending.len())
            };

            if available_slots == 0 {
                tokio::time::sleep(Duration::from_millis(50)).await;
                continue;
            }

            let blocks = self
                .piece_manager
                .next_requests(&self.peer_key, available_slots);
            if !blocks.is_empty() {
                idle_ticks = 0;
                for block in blocks {
                    self.pending_requests
                        .lock()
                        .unwrap()
                        .insert((block.piece_index, block.begin), now);
                    self.send_message(&PeerMessage::Request {
                        index: block.piece_index as u32,
                        begin: block.begin,
                        length: block.length,
                    });
                }
            } else {
                idle_ticks = (idle_ticks + 1).min(4);
                let delay_ms = (100u64 * (1 << idle_ticks.min(3))).min(1000);
                tokio::time::sleep(Duration::from_millis(delay_ms)).await;
            }
        }
    }
}
