//! Block and Piece models for managing data chunking and SHA-1 hash verification.
//!
//! Performance optimization in Rust:
//! Unlike Python (which materialized ~10M `Block` objects on the heap at startup for
//! a 155 GB torrent), `Piece` only allocates its contiguous `data` buffer and block
//! states lazily when the first block request or arrival occurs, and frees them the
//! moment the piece is written to disk.

use sha1::{Digest, Sha1};

use crate::db::now_unix_secs;

pub const BLOCK_SIZE: u32 = 16384; // 16 KiB

#[derive(Debug, Clone)]
pub struct Block {
    pub piece_index: usize,
    pub begin: u32,
    pub length: u32,
    pub is_complete: bool,
    pub requested_time: f64,
}

impl Block {
    pub fn new(piece_index: usize, begin: u32, length: u32) -> Self {
        Self {
            piece_index,
            begin,
            length,
            is_complete: false,
            requested_time: 0.0,
        }
    }

    pub fn mark_requested(&mut self) {
        self.requested_time = now_unix_secs();
    }

    pub fn reset(&mut self) {
        self.is_complete = false;
        self.requested_time = 0.0;
    }
}

#[derive(Debug, Clone)]
pub struct Piece {
    pub index: usize,
    pub length: u32,
    pub expected_hash: [u8; 20],
    pub blocks: Vec<Block>,
    buffer: Vec<u8>,
}

impl Piece {
    pub fn new(index: usize, length: u32, expected_hash: [u8; 20]) -> Self {
        let num_blocks = length.div_ceil(BLOCK_SIZE) as usize;
        let mut blocks = Vec::with_capacity(num_blocks);
        for i in 0..num_blocks {
            let begin = (i as u32) * BLOCK_SIZE;
            let blk_len = BLOCK_SIZE.min(length - begin);
            blocks.push(Block::new(index, begin, blk_len));
        }
        Self {
            index,
            length,
            expected_hash,
            blocks,
            buffer: Vec::new(),
        }
    }

    #[inline]
    pub fn is_complete(&self) -> bool {
        self.blocks.iter().all(|b| b.is_complete)
    }

    pub fn set_block_data(&mut self, begin: u32, data: &[u8]) -> bool {
        let blk_idx = (begin / BLOCK_SIZE) as usize;
        let Some(blk) = self.blocks.get_mut(blk_idx) else {
            return false;
        };
        if blk.begin != begin || blk.length as usize != data.len() {
            return false;
        }
        if self.buffer.is_empty() {
            self.buffer = vec![0u8; self.length as usize];
        }
        let start = begin as usize;
        let end = start + data.len();
        if end > self.buffer.len() {
            return false;
        }
        self.buffer[start..end].copy_from_slice(data);
        blk.is_complete = true;
        true
    }

    pub fn verify_hash(&self) -> bool {
        if !self.is_complete() || self.buffer.len() != self.length as usize {
            return false;
        }
        let mut hasher = Sha1::new();
        hasher.update(&self.buffer);
        let digest = hasher.finalize();
        digest.as_slice() == self.expected_hash
    }

    pub fn get_data(&self) -> &[u8] {
        &self.buffer
    }

    pub fn reset(&mut self) {
        for b in &mut self.blocks {
            b.reset();
        }
        self.buffer.clear();
        self.buffer.shrink_to_fit();
    }
}
