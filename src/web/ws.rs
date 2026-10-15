//! WebSocket broadcast manager using `tokio::sync::broadcast` for zero-lock fan-out.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::broadcast;

#[derive(Clone)]
pub struct WebSocketManager {
    tx: broadcast::Sender<String>,
    active_count: Arc<AtomicUsize>,
}

impl Default for WebSocketManager {
    fn default() -> Self {
        Self::new()
    }
}

impl WebSocketManager {
    pub fn new() -> Self {
        let (tx, _) = broadcast::channel(64);
        Self {
            tx,
            active_count: Arc::new(AtomicUsize::new(0)),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<String> {
        self.active_count.fetch_add(1, Ordering::Relaxed);
        self.tx.subscribe()
    }

    pub fn on_disconnect(&self) {
        self.active_count.fetch_sub(1, Ordering::Relaxed);
    }

    pub fn has_connections(&self) -> bool {
        self.active_count.load(Ordering::Relaxed) > 0
    }

    pub fn broadcast(&self, payload: &Value) {
        if !self.has_connections() {
            return;
        }
        if let Ok(text) = serde_json::to_string(payload) {
            let _ = self.tx.send(text);
        }
    }
}
