//! BitTorrent Peer Wire Protocol (BEP 0003) message packing and unpacking.

use thiserror::Error;

pub const PSTR: &[u8; 19] = b"BitTorrent protocol";
pub const PSTRLEN: u8 = 19;
pub const HANDSHAKE_LEN: usize = 68; // 1 + 19 + 8 + 20 + 20

pub const ID_CHOKE: u8 = 0;
pub const ID_UNCHOKE: u8 = 1;
pub const ID_INTERESTED: u8 = 2;
pub const ID_NOT_INTERESTED: u8 = 3;
pub const ID_HAVE: u8 = 4;
pub const ID_BITFIELD: u8 = 5;
pub const ID_REQUEST: u8 = 6;
pub const ID_PIECE: u8 = 7;
pub const ID_CANCEL: u8 = 8;
pub const ID_PORT: u8 = 9;

#[derive(Error, Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    #[error("Handshake must be at least 68 bytes, got {0}")]
    ShortHandshake(usize),
    #[error("Invalid protocol string in handshake")]
    InvalidProtocol,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handshake {
    pub info_hash: [u8; 20],
    pub peer_id: [u8; 20],
    pub reserved: [u8; 8],
}

impl Handshake {
    pub fn new(info_hash: [u8; 20], peer_id: [u8; 20]) -> Self {
        Self {
            info_hash,
            peer_id,
            reserved: [0u8; 8],
        }
    }

    pub fn encode(&self) -> [u8; HANDSHAKE_LEN] {
        let mut buf = [0u8; HANDSHAKE_LEN];
        buf[0] = PSTRLEN;
        buf[1..20].copy_from_slice(PSTR);
        buf[20..28].copy_from_slice(&self.reserved);
        buf[28..48].copy_from_slice(&self.info_hash);
        buf[48..68].copy_from_slice(&self.peer_id);
        buf
    }

    pub fn decode(data: &[u8]) -> Result<Self, ProtocolError> {
        if data.len() < HANDSHAKE_LEN {
            return Err(ProtocolError::ShortHandshake(data.len()));
        }
        if data[0] != PSTRLEN || &data[1..20] != PSTR {
            return Err(ProtocolError::InvalidProtocol);
        }
        let mut reserved = [0u8; 8];
        reserved.copy_from_slice(&data[20..28]);
        let mut info_hash = [0u8; 20];
        info_hash.copy_from_slice(&data[28..48]);
        let mut peer_id = [0u8; 20];
        peer_id.copy_from_slice(&data[48..68]);

        Ok(Self {
            info_hash,
            peer_id,
            reserved,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bitfield {
    pub data: Vec<u8>,
}

impl Bitfield {
    pub fn new(data: Vec<u8>) -> Self {
        Self { data }
    }

    pub fn with_piece_count(piece_count: usize) -> Self {
        let num_bytes = piece_count.div_ceil(8);
        Self {
            data: vec![0u8; num_bytes],
        }
    }

    #[inline]
    pub fn has_piece(&self, piece_index: usize) -> bool {
        let byte_idx = piece_index / 8;
        if byte_idx >= self.data.len() {
            return false;
        }
        let bit_idx = 7 - (piece_index % 8);
        ((self.data[byte_idx] >> bit_idx) & 1) != 0
    }

    #[inline]
    pub fn set_piece(&mut self, piece_index: usize) {
        let byte_idx = piece_index / 8;
        if byte_idx < self.data.len() {
            let bit_idx = 7 - (piece_index % 8);
            self.data[byte_idx] |= 1 << bit_idx;
        }
    }

    pub fn is_any_set(&self) -> bool {
        self.data.iter().any(|&b| b != 0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PeerMessage {
    KeepAlive,
    Choke,
    Unchoke,
    Interested,
    NotInterested,
    Have {
        piece_index: u32,
    },
    Bitfield(Bitfield),
    Request {
        index: u32,
        begin: u32,
        length: u32,
    },
    Piece {
        index: u32,
        begin: u32,
        block: Vec<u8>,
    },
    Cancel {
        index: u32,
        begin: u32,
        length: u32,
    },
    Port {
        port: u16,
    },
}

impl PeerMessage {
    pub fn encode(&self) -> Vec<u8> {
        match self {
            PeerMessage::KeepAlive => vec![0, 0, 0, 0],
            PeerMessage::Choke => vec![0, 0, 0, 1, ID_CHOKE],
            PeerMessage::Unchoke => vec![0, 0, 0, 1, ID_UNCHOKE],
            PeerMessage::Interested => vec![0, 0, 0, 1, ID_INTERESTED],
            PeerMessage::NotInterested => vec![0, 0, 0, 1, ID_NOT_INTERESTED],
            PeerMessage::Have { piece_index } => {
                let mut buf = Vec::with_capacity(9);
                buf.extend_from_slice(&5u32.to_be_bytes());
                buf.push(ID_HAVE);
                buf.extend_from_slice(&piece_index.to_be_bytes());
                buf
            }
            PeerMessage::Bitfield(bf) => {
                let len = (1 + bf.data.len()) as u32;
                let mut buf = Vec::with_capacity(4 + len as usize);
                buf.extend_from_slice(&len.to_be_bytes());
                buf.push(ID_BITFIELD);
                buf.extend_from_slice(&bf.data);
                buf
            }
            PeerMessage::Request {
                index,
                begin,
                length,
            } => {
                let mut buf = Vec::with_capacity(17);
                buf.extend_from_slice(&13u32.to_be_bytes());
                buf.push(ID_REQUEST);
                buf.extend_from_slice(&index.to_be_bytes());
                buf.extend_from_slice(&begin.to_be_bytes());
                buf.extend_from_slice(&length.to_be_bytes());
                buf
            }
            PeerMessage::Piece {
                index,
                begin,
                block,
            } => {
                let len = (9 + block.len()) as u32;
                let mut buf = Vec::with_capacity(4 + len as usize);
                buf.extend_from_slice(&len.to_be_bytes());
                buf.push(ID_PIECE);
                buf.extend_from_slice(&index.to_be_bytes());
                buf.extend_from_slice(&begin.to_be_bytes());
                buf.extend_from_slice(block);
                buf
            }
            PeerMessage::Cancel {
                index,
                begin,
                length,
            } => {
                let mut buf = Vec::with_capacity(17);
                buf.extend_from_slice(&13u32.to_be_bytes());
                buf.push(ID_CANCEL);
                buf.extend_from_slice(&index.to_be_bytes());
                buf.extend_from_slice(&begin.to_be_bytes());
                buf.extend_from_slice(&length.to_be_bytes());
                buf
            }
            PeerMessage::Port { port } => {
                let mut buf = Vec::with_capacity(7);
                buf.extend_from_slice(&3u32.to_be_bytes());
                buf.push(ID_PORT);
                buf.extend_from_slice(&port.to_be_bytes());
                buf
            }
        }
    }
}

pub fn decode_message(_payload_length: usize, msg_id: u8, payload: &[u8]) -> Option<PeerMessage> {
    match msg_id {
        ID_CHOKE => Some(PeerMessage::Choke),
        ID_UNCHOKE => Some(PeerMessage::Unchoke),
        ID_INTERESTED => Some(PeerMessage::Interested),
        ID_NOT_INTERESTED => Some(PeerMessage::NotInterested),
        ID_HAVE => {
            if payload.len() != 4 {
                return None;
            }
            let idx = u32::from_be_bytes([payload[0], payload[1], payload[2], payload[3]]);
            Some(PeerMessage::Have { piece_index: idx })
        }
        ID_BITFIELD => Some(PeerMessage::Bitfield(Bitfield::new(payload.to_vec()))),
        ID_REQUEST => {
            if payload.len() != 12 {
                return None;
            }
            let index = u32::from_be_bytes([payload[0], payload[1], payload[2], payload[3]]);
            let begin = u32::from_be_bytes([payload[4], payload[5], payload[6], payload[7]]);
            let length = u32::from_be_bytes([payload[8], payload[9], payload[10], payload[11]]);
            Some(PeerMessage::Request {
                index,
                begin,
                length,
            })
        }
        ID_PIECE => {
            if payload.len() < 8 {
                return None;
            }
            let index = u32::from_be_bytes([payload[0], payload[1], payload[2], payload[3]]);
            let begin = u32::from_be_bytes([payload[4], payload[5], payload[6], payload[7]]);
            let block = payload[8..].to_vec();
            Some(PeerMessage::Piece {
                index,
                begin,
                block,
            })
        }
        ID_CANCEL => {
            if payload.len() != 12 {
                return None;
            }
            let index = u32::from_be_bytes([payload[0], payload[1], payload[2], payload[3]]);
            let begin = u32::from_be_bytes([payload[4], payload[5], payload[6], payload[7]]);
            let length = u32::from_be_bytes([payload[8], payload[9], payload[10], payload[11]]);
            Some(PeerMessage::Cancel {
                index,
                begin,
                length,
            })
        }
        ID_PORT => {
            if payload.len() != 2 {
                return None;
            }
            let port = u16::from_be_bytes([payload[0], payload[1]]);
            Some(PeerMessage::Port { port })
        }
        _ => None,
    }
}
