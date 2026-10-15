//! BitTorrent Peer Wire Protocol (BEP 0003) and zero-copy async stream framing.

pub mod protocol;
pub mod stream;

pub use protocol::*;
pub use stream::PeerStreamIterator;
