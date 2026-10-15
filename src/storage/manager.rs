//! `PieceManager` coordinating parallel block requests, rarest-first & sequential piece
//! scheduling, per-file priority filtering, and SHA-1 verification.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use sha1::{Digest, Sha1};
use tracing::{debug, info, warn};

use crate::db::now_unix_secs;
use crate::peer::protocol::Bitfield;
use crate::storage::disk::DiskWriter;
use crate::storage::piece::{Block, Piece};
use crate::torrent::Torrent;

pub const BLOCK_TIMEOUT: f64 = 12.0;
pub const ENDGAME_REMAINING_PIECES: usize = 5;

pub const PRIO_SKIP: u8 = 0;
pub const PRIO_NORMAL: u8 = 1;
pub const PRIO_HIGH: u8 = 2;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileProgress {
    pub path: String,
    pub done_bytes: u64,
    pub total_bytes: u64,
    pub progress: f64,
}

#[derive(Debug)]
struct PieceManagerState {
    pieces: Vec<Piece>,
    missing_pieces: HashSet<usize>,
    ongoing_pieces: HashSet<usize>,
    completed_pieces: HashSet<usize>,
    skipped_pieces: HashSet<usize>,
    finalized_files: HashSet<String>,
    selected_files: Option<HashSet<String>>,
    file_priorities: HashMap<String, u8>,
    piece_priorities: HashMap<usize, u8>,
    selected_bytes: u64,
    /// Compact peer availability: `None` sentinel = seeder has all pieces;
    /// `Some(Bitfield)` = compact 1-bit-per-piece bitfield (12.5 KB for 100k pieces!).
    peers: HashMap<String, Option<Bitfield>>,
    rarity_counts: HashMap<usize, u32>,
    rarity_computed_at: f64,
    bytes_downloaded: u64,
    bytes_uploaded: u64,
    /// New zero-overhead feature: Sequential download mode ("Stream First").
    sequential: bool,
}

#[derive(Clone)]
pub struct PieceManager {
    pub torrent: Torrent,
    pub disk_writer: DiskWriter,
    inner: Arc<Mutex<PieceManagerState>>,
    on_piece_complete: Option<Arc<dyn Fn(usize) + Send + Sync>>,
}

impl PieceManager {
    pub fn new(
        torrent: Torrent,
        download_dir: impl AsRef<Path>,
        on_piece_complete: Option<Arc<dyn Fn(usize) + Send + Sync>>,
        selected_files: Option<Vec<String>>,
        file_priorities: Option<HashMap<String, u8>>,
    ) -> anyhow::Result<Self> {
        let disk_writer = DiskWriter::new(torrent.clone(), download_dir)?;
        let mut pieces = Vec::with_capacity(torrent.piece_count);
        for i in 0..torrent.piece_count {
            let sz = torrent.piece_size(i)? as u32;
            pieces.push(Piece::new(i, sz, torrent.piece_hashes[i]));
        }

        let missing_pieces: HashSet<usize> = (0..torrent.piece_count).collect();
        let state = PieceManagerState {
            pieces,
            missing_pieces,
            ongoing_pieces: HashSet::new(),
            completed_pieces: HashSet::new(),
            skipped_pieces: HashSet::new(),
            finalized_files: HashSet::new(),
            selected_files: None,
            file_priorities: HashMap::new(),
            piece_priorities: HashMap::new(),
            selected_bytes: torrent.total_length,
            peers: HashMap::new(),
            rarity_counts: HashMap::new(),
            rarity_computed_at: 0.0,
            bytes_downloaded: 0,
            bytes_uploaded: 0,
            sequential: false,
        };

        let pm = Self {
            torrent,
            disk_writer,
            inner: Arc::new(Mutex::new(state)),
            on_piece_complete,
        };

        if let Some(sel) = selected_files {
            let _ = pm.set_selected_files(Some(sel));
        }
        if let Some(prio) = file_priorities {
            if !prio.is_empty() {
                let _ = pm.set_file_priorities(prio);
            }
        }

        Ok(pm)
    }

    pub fn is_sequential(&self) -> bool {
        self.inner.lock().unwrap().sequential
    }

    pub fn set_sequential(&self, sequential: bool) {
        self.inner.lock().unwrap().sequential = sequential;
    }

    pub fn is_complete(&self) -> bool {
        let st = self.inner.lock().unwrap();
        st.missing_pieces.is_empty() && st.ongoing_pieces.is_empty()
    }

    pub fn progress_percentage(&self) -> f64 {
        let st = self.inner.lock().unwrap();
        let selected_total = self
            .torrent
            .piece_count
            .saturating_sub(st.skipped_pieces.len());
        if selected_total == 0 {
            return 100.0;
        }
        let done = st
            .completed_pieces
            .iter()
            .filter(|idx| !st.skipped_pieces.contains(idx))
            .count();
        (done as f64 / selected_total as f64) * 100.0
    }

    pub fn selected_total_bytes(&self) -> u64 {
        self.inner.lock().unwrap().selected_bytes
    }

    pub fn bytes_downloaded(&self) -> u64 {
        self.inner.lock().unwrap().bytes_downloaded
    }

    pub fn bytes_uploaded(&self) -> u64 {
        self.inner.lock().unwrap().bytes_uploaded
    }

    pub fn completed_count(&self) -> usize {
        self.inner.lock().unwrap().completed_pieces.len()
    }

    pub fn skipped_count(&self) -> usize {
        self.inner.lock().unwrap().skipped_pieces.len()
    }

    pub fn ongoing_count(&self) -> usize {
        self.inner.lock().unwrap().ongoing_pieces.len()
    }

    pub fn completed_and_ongoing_snapshot(&self) -> (Vec<usize>, Vec<usize>) {
        let st = self.inner.lock().unwrap();
        let mut comp: Vec<usize> = st.completed_pieces.iter().copied().collect();
        comp.sort_unstable();
        let mut ong: Vec<usize> = st.ongoing_pieces.iter().copied().collect();
        ong.sort_unstable();
        (comp, ong)
    }

    pub fn is_file_selected(&self, path: &str) -> bool {
        let st = self.inner.lock().unwrap();
        match &st.selected_files {
            None => true,
            Some(set) => set.contains(path),
        }
    }

    pub fn get_file_priority(&self, path: &str) -> u8 {
        let st = self.inner.lock().unwrap();
        *st.file_priorities.get(path).unwrap_or(&PRIO_NORMAL)
    }

    pub fn file_priorities_snapshot(&self) -> HashMap<String, u8> {
        self.inner.lock().unwrap().file_priorities.clone()
    }

    pub fn selected_files_snapshot(&self) -> Option<Vec<String>> {
        let st = self.inner.lock().unwrap();
        st.selected_files.as_ref().map(|s| {
            let mut v: Vec<String> = s.iter().cloned().collect();
            v.sort();
            v
        })
    }

    fn pieces_overlapping_files(&self, paths: &HashSet<String>) -> HashSet<usize> {
        let mut out = HashSet::new();
        if self.torrent.piece_count == 0 {
            return out;
        }
        for f in &self.torrent.files {
            if !paths.contains(&f.path) {
                continue;
            }
            let first = (f.offset / self.torrent.piece_length) as usize;
            let last = ((f.offset + f.length.max(1) - 1) / self.torrent.piece_length) as usize;
            for idx in first..=last.min(self.torrent.piece_count - 1) {
                out.insert(idx);
            }
        }
        out
    }

    fn maybe_finalize_files_locked(&self, st: &mut PieceManagerState) {
        for f in &self.torrent.files {
            if st.finalized_files.contains(&f.path) {
                continue;
            }
            let is_sel = st
                .selected_files
                .as_ref()
                .map(|s| s.contains(&f.path))
                .unwrap_or(true);
            if !is_sel {
                continue;
            }
            let mut single = HashSet::new();
            single.insert(f.path.clone());
            let overlapping = self.pieces_overlapping_files(&single);
            if overlapping
                .iter()
                .all(|idx| st.completed_pieces.contains(idx))
                && self.disk_writer.finalize_file(&f.path)
            {
                st.finalized_files.insert(f.path.clone());
            }
        }
    }

    pub fn set_selected_files(
        &self,
        selected_paths: Option<Vec<String>>,
    ) -> Result<serde_json::Value, String> {
        let all_paths: Vec<String> = self.torrent.files.iter().map(|f| f.path.clone()).collect();
        match selected_paths {
            None => self.set_file_priorities(HashMap::new()),
            Some(sel) => {
                let sel_set: HashSet<String> = sel.into_iter().collect();
                let all_set: HashSet<String> = all_paths.iter().cloned().collect();
                if sel_set == all_set {
                    return self.set_file_priorities(HashMap::new());
                }
                let mut map = HashMap::with_capacity(all_paths.len());
                for p in all_paths {
                    let prio = if sel_set.contains(&p) {
                        PRIO_NORMAL
                    } else {
                        PRIO_SKIP
                    };
                    map.insert(p, prio);
                }
                self.set_file_priorities(map)
            }
        }
    }

    pub fn set_file_priorities(
        &self,
        priorities: HashMap<String, u8>,
    ) -> Result<serde_json::Value, String> {
        let all_paths: HashSet<String> =
            self.torrent.files.iter().map(|f| f.path.clone()).collect();
        let mut st = self.inner.lock().unwrap();

        if priorities.is_empty() {
            st.selected_files = None;
            st.file_priorities.clear();
            st.piece_priorities.clear();
            st.skipped_pieces.clear();
            for idx in 0..self.torrent.piece_count {
                if !st.completed_pieces.contains(&idx) && !st.ongoing_pieces.contains(&idx) {
                    st.missing_pieces.insert(idx);
                }
            }
            st.selected_bytes = self.torrent.total_length;
            st.rarity_computed_at = 0.0;
            return Ok(serde_json::json!({
                "selected_count": all_paths.len(),
                "total_count": all_paths.len(),
                "high_priority_count": 0,
                "skipped_pieces": 0,
            }));
        }

        for (k, &v) in &priorities {
            if !all_paths.contains(k) {
                return Err(format!("Unknown file in selection: {}", k));
            }
            if v > PRIO_HIGH {
                return Err(format!(
                    "Invalid priority {} for '{}': use 0=skip, 1=normal, 2=high",
                    v, k
                ));
            }
        }

        let mut full: HashMap<String, u8> =
            all_paths.iter().map(|p| (p.clone(), PRIO_NORMAL)).collect();
        for (p, v) in priorities {
            full.insert(p, v);
        }

        let selected: HashSet<String> = full
            .iter()
            .filter(|(_, &p)| p > PRIO_SKIP)
            .map(|(k, _)| k.clone())
            .collect();
        if selected.is_empty() {
            return Err("At least one file must stay selected (priority > 0).".to_string());
        }

        let keep_pieces = self.pieces_overlapping_files(&selected);
        let new_skipped: HashSet<usize> = (0..self.torrent.piece_count)
            .filter(|i| !keep_pieces.contains(i))
            .collect();

        let mut piece_prio = HashMap::new();
        if self.torrent.piece_count > 0 {
            for f in &self.torrent.files {
                let p = *full.get(&f.path).unwrap_or(&PRIO_NORMAL);
                if p == PRIO_SKIP {
                    continue;
                }
                let first = (f.offset / self.torrent.piece_length) as usize;
                let last = ((f.offset + f.length.max(1) - 1) / self.torrent.piece_length) as usize;
                for idx in first..=last.min(self.torrent.piece_count - 1) {
                    let cur = *piece_prio.get(&idx).unwrap_or(&PRIO_SKIP);
                    if p > cur {
                        piece_prio.insert(idx, p);
                    }
                }
            }
        }

        let before = st.skipped_pieces.len() as i64;

        let ongoing_list: Vec<usize> = st.ongoing_pieces.iter().copied().collect();
        for piece_idx in ongoing_list {
            if new_skipped.contains(&piece_idx) {
                let refunded: u64 = st.pieces[piece_idx]
                    .blocks
                    .iter()
                    .filter(|b| b.is_complete)
                    .map(|b| b.length as u64)
                    .sum();
                st.bytes_downloaded = st.bytes_downloaded.saturating_sub(refunded);
                st.pieces[piece_idx].reset();
                st.ongoing_pieces.remove(&piece_idx);
            }
        }

        for idx in 0..self.torrent.piece_count {
            if new_skipped.contains(&idx) {
                st.missing_pieces.remove(&idx);
                if st.completed_pieces.contains(&idx) {
                    st.pieces[idx].reset();
                }
            } else if !st.completed_pieces.contains(&idx) && !st.ongoing_pieces.contains(&idx) {
                st.missing_pieces.insert(idx);
            }
        }

        st.skipped_pieces = new_skipped;
        st.file_priorities = full
            .iter()
            .filter(|(_, &p)| p != PRIO_NORMAL)
            .map(|(k, &v)| (k.clone(), v))
            .collect();
        st.selected_files = if selected.len() == all_paths.len() {
            None
        } else {
            Some(selected.clone())
        };
        st.piece_priorities = piece_prio;

        if st.selected_files.is_none() {
            st.selected_bytes = self.torrent.total_length;
        } else {
            let mut total = 0u64;
            for i in 0..self.torrent.piece_count {
                if !st.skipped_pieces.contains(&i) {
                    total += self.torrent.piece_size(i).unwrap_or(0);
                }
            }
            st.selected_bytes = total;
        }
        st.rarity_computed_at = 0.0;
        self.maybe_finalize_files_locked(&mut st);

        let high_count = full.values().filter(|&&p| p == PRIO_HIGH).count();
        let skipped_now = st.skipped_pieces.len();

        Ok(serde_json::json!({
            "selected_count": selected.len(),
            "total_count": all_paths.len(),
            "high_priority_count": high_count,
            "skipped_pieces": skipped_now,
            "skipped_delta": (skipped_now as i64) - before,
        }))
    }

    pub fn file_progress(&self) -> Vec<FileProgress> {
        let st = self.inner.lock().unwrap();
        let mut out = Vec::with_capacity(self.torrent.files.len());
        for f in &self.torrent.files {
            if f.length == 0 {
                out.push(FileProgress {
                    path: f.path.clone(),
                    done_bytes: 0,
                    total_bytes: 0,
                    progress: 100.0,
                });
                continue;
            }
            let first = (f.offset / self.torrent.piece_length) as usize;
            let last = ((f.offset + f.length - 1) / self.torrent.piece_length) as usize;
            let mut done = 0u64;
            if self.torrent.piece_count > 0 {
                for idx in first..=last.min(self.torrent.piece_count - 1) {
                    if st.completed_pieces.contains(&idx) {
                        let p_start = (idx as u64) * self.torrent.piece_length;
                        let p_end = p_start + self.torrent.piece_size(idx).unwrap_or(0);
                        let ov_start = p_start.max(f.offset);
                        let ov_end = p_end.min(f.offset + f.length);
                        if ov_end > ov_start {
                            done += ov_end - ov_start;
                        }
                    }
                }
            }
            let progress = ((done as f64 / f.length as f64) * 1000.0).round() / 10.0;
            out.push(FileProgress {
                path: f.path.clone(),
                done_bytes: done,
                total_bytes: f.length,
                progress,
            });
        }
        out
    }

    pub fn add_peer(&self, peer_key: &str, bitfield: Bitfield) {
        let mut st = self.inner.lock().unwrap();
        st.peers.insert(peer_key.to_string(), Some(bitfield));
        st.rarity_computed_at = 0.0;
    }

    pub fn peer_has_piece(&self, peer_key: &str, piece_index: usize) {
        let mut st = self.inner.lock().unwrap();
        match st.peers.get_mut(peer_key) {
            Some(None) => return, // Seeder already has all pieces
            Some(Some(bf)) => {
                bf.set_piece(piece_index);
            }
            None => {
                let mut bf = Bitfield::with_piece_count(self.torrent.piece_count);
                bf.set_piece(piece_index);
                st.peers.insert(peer_key.to_string(), Some(bf));
            }
        }
        *st.rarity_counts.entry(piece_index).or_insert(0) += 1;
    }

    pub fn peer_has_all_pieces(&self, peer_key: &str) {
        let mut st = self.inner.lock().unwrap();
        st.peers.insert(peer_key.to_string(), None);
        st.rarity_computed_at = 0.0;
    }

    pub fn remove_peer(&self, peer_key: &str) {
        let mut st = self.inner.lock().unwrap();
        st.peers.remove(peer_key);
        st.rarity_computed_at = 0.0;
    }

    pub fn has_peer(&self, peer_key: &str) -> bool {
        self.inner.lock().unwrap().peers.contains_key(peer_key)
    }

    pub fn get_bitfield(&self) -> Bitfield {
        let st = self.inner.lock().unwrap();
        let mut bf = Bitfield::with_piece_count(self.torrent.piece_count);
        for &idx in &st.completed_pieces {
            bf.set_piece(idx);
        }
        bf
    }

    pub fn check_existing_files(&self, verify_mode: Option<&str>) -> usize {
        let mode = verify_mode
            .map(|s| s.to_string())
            .or_else(|| std::env::var("EVA_VERIFY_ON_STARTUP").ok())
            .unwrap_or_else(|| "quick".to_string())
            .to_lowercase();

        if mode == "off" {
            warn!(
                "[VERIFY] '{}': EVA_VERIFY_ON_STARTUP=off, skipping disk verification ({} pieces marked missing)",
                self.torrent.name, self.torrent.piece_count
            );
            let mut st = self.inner.lock().unwrap();
            st.completed_pieces.clear();
            st.ongoing_pieces.clear();
            st.missing_pieces = (0..self.torrent.piece_count)
                .filter(|i| !st.skipped_pieces.contains(i))
                .collect();
            for p in &mut st.pieces {
                p.reset();
            }
            st.bytes_downloaded = 0;
            return 0;
        }

        let any_file_exists = self.torrent.files.iter().any(|f| {
            self.disk_writer.output_dir.join(&f.path).exists()
                || self
                    .disk_writer
                    .output_dir
                    .join(format!("{}.part", f.path))
                    .exists()
        });
        if !any_file_exists {
            let mut st = self.inner.lock().unwrap();
            st.completed_pieces.clear();
            st.ongoing_pieces.clear();
            st.missing_pieces = (0..self.torrent.piece_count)
                .filter(|i| !st.skipped_pieces.contains(i))
                .collect();
            for p in &mut st.pieces {
                p.reset();
            }
            st.bytes_downloaded = 0;
            return 0;
        }

        let skipped_snapshot = self.inner.lock().unwrap().skipped_pieces.clone();
        let mut verified_indices = Vec::new();
        for idx in 0..self.torrent.piece_count {
            if skipped_snapshot.contains(&idx) {
                continue;
            }
            let expected_len = self.torrent.piece_size(idx).unwrap_or(0) as usize;
            if let Ok(data) = self.disk_writer.read_piece(idx) {
                if data.len() != expected_len {
                    continue;
                }
                if mode != "full" && !data.is_empty() && data.iter().all(|&b| b == 0) {
                    continue;
                }
                let mut hasher = Sha1::new();
                hasher.update(&data);
                if hasher.finalize().as_slice() == self.torrent.piece_hashes[idx] {
                    verified_indices.push(idx);
                }
            }
        }

        let mut st = self.inner.lock().unwrap();
        st.completed_pieces.clear();
        st.ongoing_pieces.clear();
        st.missing_pieces = (0..self.torrent.piece_count)
            .filter(|i| !st.skipped_pieces.contains(i))
            .collect();
        for p in &mut st.pieces {
            p.reset();
        }
        for &idx in &verified_indices {
            st.completed_pieces.insert(idx);
            st.missing_pieces.remove(&idx);
        }
        st.bytes_downloaded = st
            .completed_pieces
            .iter()
            .map(|&i| self.torrent.piece_size(i).unwrap_or(0))
            .sum();
        self.maybe_finalize_files_locked(&mut st);
        verified_indices.len()
    }

    pub fn read_block(&self, piece_index: usize, begin: u32, length: u32) -> Option<Vec<u8>> {
        {
            let st = self.inner.lock().unwrap();
            if !st.completed_pieces.contains(&piece_index) || piece_index >= st.pieces.len() {
                return None;
            }
            let piece_len = st.pieces[piece_index].length;
            if begin.saturating_add(length) > piece_len {
                return None;
            }
        }
        let full_piece = self.disk_writer.read_piece(piece_index).ok()?;
        let start = begin as usize;
        let end = start + (length as usize);
        if end > full_piece.len() {
            return None;
        }
        let slice = full_piece[start..end].to_vec();
        self.inner.lock().unwrap().bytes_uploaded += slice.len() as u64;
        Some(slice)
    }

    fn piece_priority_locked(st: &PieceManagerState, piece_idx: usize) -> u8 {
        if st.skipped_pieces.contains(&piece_idx) {
            PRIO_SKIP
        } else {
            *st.piece_priorities.get(&piece_idx).unwrap_or(&PRIO_NORMAL)
        }
    }

    fn peer_has_locked(peer_bf: &Option<Bitfield>, piece_idx: usize) -> bool {
        match peer_bf {
            None => true,
            Some(bf) => bf.has_piece(piece_idx),
        }
    }

    fn refresh_rarity_locked(&self, st: &mut PieceManagerState) {
        let now = now_unix_secs();
        if now - st.rarity_computed_at < 10.0 && !st.rarity_counts.is_empty() {
            return;
        }
        let mut counts = HashMap::new();
        for bf in st.peers.values().flatten() {
            for idx in 0..self.torrent.piece_count {
                if bf.has_piece(idx) {
                    *counts.entry(idx).or_insert(0) += 1;
                }
            }
        }
        st.rarity_counts = counts;
        st.rarity_computed_at = now;
    }

    pub fn next_requests(&self, peer_key: &str, max_count: usize) -> Vec<Block> {
        let mut st = self.inner.lock().unwrap();
        let Some(peer_bf_clone) = st.peers.get(peer_key).cloned() else {
            return Vec::new();
        };

        let now = now_unix_secs();
        let mut blocks_to_request = Vec::with_capacity(max_count);

        let mut ongoing_sorted: Vec<usize> = st.ongoing_pieces.iter().copied().collect();
        ongoing_sorted
            .sort_by_key(|&i| (std::cmp::Reverse(Self::piece_priority_locked(&st, i)), i));

        // 1. Timed-out blocks in ongoing pieces
        for &piece_idx in &ongoing_sorted {
            if Self::peer_has_locked(&peer_bf_clone, piece_idx) {
                for block in &mut st.pieces[piece_idx].blocks {
                    if !block.is_complete
                        && block.requested_time > 0.0
                        && (now - block.requested_time > BLOCK_TIMEOUT)
                    {
                        block.mark_requested();
                        blocks_to_request.push(block.clone());
                        if blocks_to_request.len() >= max_count {
                            return blocks_to_request;
                        }
                    }
                }
            }
        }

        // 2. Unrequested blocks in ongoing pieces
        for &piece_idx in &ongoing_sorted {
            if Self::peer_has_locked(&peer_bf_clone, piece_idx) {
                for block in &mut st.pieces[piece_idx].blocks {
                    if !block.is_complete && block.requested_time == 0.0 {
                        block.mark_requested();
                        blocks_to_request.push(block.clone());
                        if blocks_to_request.len() >= max_count {
                            return blocks_to_request;
                        }
                    }
                }
            }
        }

        // 3. Start new missing pieces (Sequential vs Rarest-First)
        let max_ongoing = crate::engine::limits::max_ongoing_pieces();
        if st.ongoing_pieces.len() < max_ongoing.max(1) {
            if !st.sequential {
                self.refresh_rarity_locked(&mut st);
            }
            let mut candidates: Vec<usize> = st
                .missing_pieces
                .iter()
                .copied()
                .filter(|&i| {
                    !st.ongoing_pieces.contains(&i) && Self::peer_has_locked(&peer_bf_clone, i)
                })
                .collect();

            let sort_cap = crate::engine::limits::rarest_sort_cap();
            if candidates.len() > sort_cap {
                candidates.sort_by_key(|&idx| {
                    (
                        std::cmp::Reverse(Self::piece_priority_locked(&st, idx)),
                        idx,
                    )
                });
                candidates.truncate(sort_cap);
            }

            if st.sequential {
                // Sequential streaming mode: high priority first, then lowest piece index first
                candidates.sort_by_key(|&idx| {
                    (
                        std::cmp::Reverse(Self::piece_priority_locked(&st, idx)),
                        idx,
                    )
                });
            } else {
                // Rarest-first within priority tier
                candidates.sort_by_key(|&idx| {
                    (
                        std::cmp::Reverse(Self::piece_priority_locked(&st, idx)),
                        *st.rarity_counts.get(&idx).unwrap_or(&0),
                        idx,
                    )
                });
            }

            for piece_idx in candidates {
                st.missing_pieces.remove(&piece_idx);
                st.ongoing_pieces.insert(piece_idx);
                for block in &mut st.pieces[piece_idx].blocks {
                    if !block.is_complete && block.requested_time == 0.0 {
                        block.mark_requested();
                        blocks_to_request.push(block.clone());
                        if blocks_to_request.len() >= max_count {
                            return blocks_to_request;
                        }
                    }
                }
                if blocks_to_request.len() >= max_count {
                    break;
                }
            }
        }

        // 4. Endgame mode: duplicate requests across peers when only a few pieces remain
        if blocks_to_request.is_empty()
            && st.missing_pieces.len() + st.ongoing_pieces.len() <= ENDGAME_REMAINING_PIECES
        {
            for &piece_idx in &ongoing_sorted {
                if !Self::peer_has_locked(&peer_bf_clone, piece_idx) {
                    continue;
                }
                for block in &mut st.pieces[piece_idx].blocks {
                    if !block.is_complete {
                        block.mark_requested();
                        blocks_to_request.push(block.clone());
                        if blocks_to_request.len() >= max_count {
                            return blocks_to_request;
                        }
                    }
                }
            }
        }

        blocks_to_request
    }

    pub fn on_block_received(&self, index: usize, begin: u32, data: &[u8]) -> bool {
        let mut completed_data: Option<Vec<u8>> = None;
        {
            let mut st = self.inner.lock().unwrap();
            if index >= st.pieces.len() || st.completed_pieces.contains(&index) {
                return false;
            }
            // Ignore duplicate block arrivals (endgame / timeout reassignments)
            if st.pieces[index]
                .blocks
                .iter()
                .any(|b| b.begin == begin && b.is_complete)
            {
                return false;
            }
            if !st.pieces[index].set_block_data(begin, data) {
                return false;
            }
            st.bytes_downloaded += data.len() as u64;

            if st.pieces[index].is_complete() {
                if st.pieces[index].verify_hash() {
                    completed_data = Some(st.pieces[index].get_data().to_vec());
                    // Release piece buffer immediately after extraction to prevent RAM growth
                    st.pieces[index].reset();
                    st.ongoing_pieces.remove(&index);
                    st.missing_pieces.remove(&index);
                    st.completed_pieces.insert(index);
                } else {
                    warn!("Hash mismatch on piece {}! Discarding and retrying.", index);
                    st.pieces[index].reset();
                    st.ongoing_pieces.remove(&index);
                    st.missing_pieces.insert(index);
                    return false;
                }
            }
        }

        if let Some(piece_bytes) = completed_data {
            if let Err(e) = self.disk_writer.write_piece(index, &piece_bytes) {
                debug!("Failed writing piece {} to disk: {}", index, e);
            }
            let is_all_done = self.is_complete();
            {
                let mut st = self.inner.lock().unwrap();
                if is_all_done {
                    let sel = st.selected_files.clone();
                    self.disk_writer.finalize(sel.as_ref());
                    for f in &self.torrent.files {
                        if sel.as_ref().map(|s| s.contains(&f.path)).unwrap_or(true) {
                            st.finalized_files.insert(f.path.clone());
                        }
                    }
                } else {
                    self.maybe_finalize_files_locked(&mut st);
                }
            }
            if self.torrent.piece_count < 2000 || index.is_multiple_of(50) || is_all_done {
                info!(
                    "Piece {}/{} verified & saved. Progress: {:.1}%",
                    index,
                    self.torrent.piece_count,
                    self.progress_percentage()
                );
            }
            if let Some(ref cb) = self.on_piece_complete {
                cb(index);
            }
            return true;
        }
        false
    }
}
