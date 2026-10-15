//! Disk storage manager for single-file and multi-file torrents with `.part` safety
//! and random-access byte range streaming for HTTP media playback.

use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use tracing::{info, warn};

use crate::torrent::{FileInfo, Torrent};

#[derive(Debug, Clone)]
pub struct DiskWriter {
    pub torrent: Torrent,
    pub output_dir: PathBuf,
    pub files: Vec<FileInfo>,
}

impl DiskWriter {
    pub fn new(torrent: Torrent, output_dir: impl AsRef<Path>) -> std::io::Result<Self> {
        let output_dir = output_dir.as_ref().to_path_buf();
        let files = torrent.files.clone();
        let writer = Self {
            torrent,
            output_dir,
            files,
        };
        writer.init_paths()?;
        Ok(writer)
    }

    fn init_paths(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.output_dir)?;
        for f in &self.files {
            let final_path = self.output_dir.join(&f.path);
            if let Some(parent) = final_path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let part_path = self.output_dir.join(format!("{}.part", f.path));
            if !final_path.exists() && !part_path.exists() {
                let _ = File::create(&part_path)?;
            }
        }
        Ok(())
    }

    pub fn active_file_path(&self, file_info: &FileInfo) -> PathBuf {
        let final_path = self.output_dir.join(&file_info.path);
        if final_path.exists() {
            final_path
        } else {
            self.output_dir.join(format!("{}.part", file_info.path))
        }
    }

    pub fn write_piece(&self, piece_index: usize, data: &[u8]) -> std::io::Result<()> {
        let piece_offset = (piece_index as u64) * self.torrent.piece_length;
        let piece_end = piece_offset + (data.len() as u64);
        let mut bytes_written = 0usize;

        for f in &self.files {
            let file_start = f.offset;
            let file_end = f.offset + f.length;

            if piece_offset < file_end && piece_end > file_start {
                let overlap_start = piece_offset.max(file_start);
                let overlap_end = piece_end.min(file_end);

                let slice_start = (overlap_start - piece_offset) as usize;
                let slice_end = (overlap_end - piece_offset) as usize;
                let slice_to_write = &data[slice_start..slice_end];

                let file_seek_pos = overlap_start - file_start;
                let target_path = self.active_file_path(f);
                if let Some(parent) = target_path.parent() {
                    std::fs::create_dir_all(parent)?;
                }

                let mut fp = OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create(true)
                    .truncate(false)
                    .open(&target_path)?;
                fp.seek(SeekFrom::Start(file_seek_pos))?;
                fp.write_all(slice_to_write)?;
                fp.flush()?;

                bytes_written += slice_to_write.len();
            }
        }

        if bytes_written != data.len() {
            warn!(
                "Piece {}: Expected to write {} bytes, wrote {}",
                piece_index,
                data.len(),
                bytes_written
            );
        }
        Ok(())
    }

    pub fn finalize_file(&self, path: &str) -> bool {
        for f in &self.files {
            if f.path != path {
                continue;
            }
            let part_path = self.output_dir.join(format!("{}.part", f.path));
            let final_path = self.output_dir.join(&f.path);
            if part_path.exists() && !final_path.exists() {
                if let Some(parent) = final_path.parent() {
                    let _ = std::fs::create_dir_all(parent);
                }
                if std::fs::rename(&part_path, &final_path).is_ok() {
                    info!("Finalized '{}' (removed .part extension)", f.path);
                    return true;
                }
                return false;
            }
            return final_path.exists();
        }
        false
    }

    pub fn finalize(&self, selected_paths: Option<&HashSet<String>>) {
        for f in &self.files {
            if let Some(sel) = selected_paths {
                if !sel.contains(&f.path) {
                    continue;
                }
            }
            let part_path = self.output_dir.join(format!("{}.part", f.path));
            let final_path = self.output_dir.join(&f.path);
            if part_path.exists() {
                if let Some(parent) = final_path.parent() {
                    let _ = std::fs::create_dir_all(parent);
                }
                if std::fs::rename(&part_path, &final_path).is_ok() {
                    info!("Finalized '{}' (removed .part extension)", f.path);
                }
            }
        }
    }

    pub fn read_piece(&self, piece_index: usize) -> std::io::Result<Vec<u8>> {
        let piece_size = self
            .torrent
            .piece_size(piece_index)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e.to_string()))?
            as usize;
        let piece_offset = (piece_index as u64) * self.torrent.piece_length;
        let piece_end = piece_offset + (piece_size as u64);

        let mut buffer = Vec::with_capacity(piece_size);
        for f in &self.files {
            let file_start = f.offset;
            let file_end = f.offset + f.length;

            if piece_offset < file_end && piece_end > file_start {
                let overlap_start = piece_offset.max(file_start);
                let overlap_end = piece_end.min(file_end);
                let len_to_read = (overlap_end - overlap_start) as usize;
                let file_seek_pos = overlap_start - file_start;
                let target_path = self.active_file_path(f);

                if target_path.exists() {
                    let mut fp = File::open(&target_path)?;
                    fp.seek(SeekFrom::Start(file_seek_pos))?;
                    let mut chunk = vec![0u8; len_to_read];
                    let n = fp.read(&mut chunk)?;
                    buffer.extend_from_slice(&chunk[..n]);
                    if n < len_to_read {
                        buffer.resize(buffer.len() + (len_to_read - n), 0);
                    }
                } else {
                    buffer.resize(buffer.len() + len_to_read, 0);
                }
            }
        }
        Ok(buffer)
    }

    /// Reads an arbitrary byte slice from a specific file within the torrent (used for HTTP Range streaming).
    pub fn read_file_range(
        &self,
        file_index: usize,
        start: u64,
        length: usize,
    ) -> std::io::Result<Vec<u8>> {
        let f = self.files.get(file_index).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::NotFound, "File index out of bounds")
        })?;
        let target_path = self.active_file_path(f);
        let mut fp = File::open(&target_path)?;
        fp.seek(SeekFrom::Start(start))?;
        let mut buf = vec![0u8; length];
        let n = fp.read(&mut buf)?;
        buf.truncate(n);
        Ok(buf)
    }
}
