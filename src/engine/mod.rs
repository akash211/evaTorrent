//! Core BitTorrent download engine: resource limits, speed scheduler, cgroup/disk monitor,
//! peer connection workers, `TorrentSession`, and `EngineManager`.

pub mod connection;
pub mod limits;
pub mod manager;
pub mod monitor;
pub mod session;
pub mod speed;
