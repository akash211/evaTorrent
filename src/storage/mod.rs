//! Storage subsystem: piece/block tracking, `.part` disk writer, and `PieceManager`.

pub mod disk;
pub mod manager;
pub mod piece;

pub use disk::DiskWriter;
pub use manager::{PieceManager, PRIO_HIGH, PRIO_NORMAL, PRIO_SKIP};
pub use piece::{Block, Piece, BLOCK_SIZE};
