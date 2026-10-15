//! evaTorrent ⚡ — High-performance asynchronous BitTorrent engine & Web UI in Rust.

// Intentional API shapes (tracker announce needs 8 args; torrent/search types
// are complex by nature; `from_str`-style helpers predate FromStr adoption).
#![allow(clippy::too_many_arguments)]
#![allow(clippy::type_complexity)]
#![allow(clippy::should_implement_trait)]

pub mod auth;
pub mod bencoding;
pub mod db;
pub mod engine;
pub mod metadata;
pub mod peer;
pub mod search;
pub mod storage;
pub mod subtitles;
pub mod torrent;
pub mod tracker;
pub mod web;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
