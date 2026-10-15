//! Bencoding encoder and decoder according to the BitTorrent specification (BEP 0003).
//!
//! Supports:
//! - Integers: `i<num>e` -> `BencodeValue::Int(i64)`
//! - Byte strings: `<len>:<content>` -> `BencodeValue::Bytes(Vec<u8>)`
//! - Lists: `l<item>...e` -> `BencodeValue::List(Vec<BencodeValue>)`
//! - Dictionaries: `d<key><val>...e` -> `BencodeValue::Dict(BTreeMap<Vec<u8>, BencodeValue>)`

use std::collections::BTreeMap;
use thiserror::Error;

#[derive(Error, Debug, Clone, PartialEq, Eq)]
pub enum BencodeError {
    #[error("Empty bencoded data")]
    EmptyData,
    #[error("Unexpected end of bencoded data")]
    UnexpectedEof,
    #[error("Extra data at end of input: {index} < {length}")]
    TrailingData { index: usize, length: usize },
    #[error("Invalid token '{token}' at index {index}")]
    InvalidToken { token: char, index: usize },
    #[error("Unterminated integer")]
    UnterminatedInteger,
    #[error("Empty integer value")]
    EmptyInteger,
    #[error("Negative zero is invalid in bencoding")]
    NegativeZero,
    #[error("Leading zeroes are invalid in bencoding integers")]
    LeadingZero,
    #[error("Malformed integer: {0}")]
    MalformedInteger(String),
    #[error("Unterminated string length")]
    UnterminatedStringLength,
    #[error("Invalid string length format")]
    InvalidStringLength,
    #[error("String length {length} exceeds remaining data ({remaining} bytes)")]
    StringOutOfBounds { length: usize, remaining: usize },
    #[error("Dictionary key must be a byte string")]
    InvalidDictKey,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BencodeValue {
    Int(i64),
    Bytes(Vec<u8>),
    List(Vec<BencodeValue>),
    Dict(BTreeMap<Vec<u8>, BencodeValue>),
}

impl BencodeValue {
    pub fn as_int(&self) -> Option<i64> {
        match self {
            BencodeValue::Int(v) => Some(*v),
            _ => None,
        }
    }

    pub fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            BencodeValue::Bytes(b) => Some(b.as_slice()),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<String> {
        self.as_bytes()
            .map(|b| String::from_utf8_lossy(b).into_owned())
    }

    pub fn as_list(&self) -> Option<&[BencodeValue]> {
        match self {
            BencodeValue::List(l) => Some(l.as_slice()),
            _ => None,
        }
    }

    pub fn as_dict(&self) -> Option<&BTreeMap<Vec<u8>, BencodeValue>> {
        match self {
            BencodeValue::Dict(d) => Some(d),
            _ => None,
        }
    }

    pub fn dict_get(&self, key: &[u8]) -> Option<&BencodeValue> {
        self.as_dict()?.get(key)
    }
}

pub struct Decoder<'a> {
    data: &'a [u8],
    index: usize,
}

impl<'a> Decoder<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Self { data, index: 0 }
    }

    pub fn decode(&mut self) -> Result<BencodeValue, BencodeError> {
        if self.index >= self.data.len() {
            return Err(BencodeError::EmptyData);
        }
        let val = self.decode_next()?;
        if self.index != self.data.len() {
            return Err(BencodeError::TrailingData {
                index: self.index,
                length: self.data.len(),
            });
        }
        Ok(val)
    }

    fn peek(&self) -> Result<u8, BencodeError> {
        if self.index >= self.data.len() {
            Err(BencodeError::UnexpectedEof)
        } else {
            Ok(self.data[self.index])
        }
    }

    fn decode_next(&mut self) -> Result<BencodeValue, BencodeError> {
        let token = self.peek()?;
        match token {
            b'i' => self.decode_int(),
            b'l' => self.decode_list(),
            b'd' => self.decode_dict(),
            b'0'..=b'9' => self.decode_bytes().map(BencodeValue::Bytes),
            _ => Err(BencodeError::InvalidToken {
                token: token as char,
                index: self.index,
            }),
        }
    }

    fn decode_int(&mut self) -> Result<BencodeValue, BencodeError> {
        self.index += 1; // Skip 'i'
        let rel_end = self.data[self.index..]
            .iter()
            .position(|&b| b == b'e')
            .ok_or(BencodeError::UnterminatedInteger)?;
        let end_idx = self.index + rel_end;
        let raw_num = &self.data[self.index..end_idx];

        if raw_num.is_empty() {
            return Err(BencodeError::EmptyInteger);
        }
        if raw_num == b"-0" {
            return Err(BencodeError::NegativeZero);
        }
        if raw_num.len() > 1 && raw_num[0] == b'0' {
            return Err(BencodeError::LeadingZero);
        }
        if raw_num.len() > 2 && raw_num.starts_with(b"-0") {
            return Err(BencodeError::LeadingZero);
        }

        let s = std::str::from_utf8(raw_num)
            .map_err(|_| BencodeError::MalformedInteger(format!("{:?}", raw_num)))?;
        let val: i64 = s
            .parse()
            .map_err(|_| BencodeError::MalformedInteger(s.to_string()))?;

        self.index = end_idx + 1; // Skip 'e'
        Ok(BencodeValue::Int(val))
    }

    fn decode_bytes(&mut self) -> Result<Vec<u8>, BencodeError> {
        let rel_colon = self.data[self.index..]
            .iter()
            .position(|&b| b == b':')
            .ok_or(BencodeError::UnterminatedStringLength)?;
        let colon_idx = self.index + rel_colon;
        let raw_len = &self.data[self.index..colon_idx];

        if raw_len.is_empty() || (raw_len.len() > 1 && raw_len[0] == b'0') {
            return Err(BencodeError::InvalidStringLength);
        }
        if raw_len[0] == b'-' {
            return Err(BencodeError::InvalidStringLength);
        }

        let len_str =
            std::str::from_utf8(raw_len).map_err(|_| BencodeError::InvalidStringLength)?;
        let length: usize = len_str
            .parse()
            .map_err(|_| BencodeError::InvalidStringLength)?;

        let start = colon_idx + 1;
        let end = start
            .checked_add(length)
            .ok_or(BencodeError::InvalidStringLength)?;
        if end > self.data.len() {
            return Err(BencodeError::StringOutOfBounds {
                length,
                remaining: self.data.len().saturating_sub(start),
            });
        }

        self.index = end;
        Ok(self.data[start..end].to_vec())
    }

    fn decode_list(&mut self) -> Result<BencodeValue, BencodeError> {
        self.index += 1; // Skip 'l'
        let mut items = Vec::new();
        while self.peek()? != b'e' {
            items.push(self.decode_next()?);
        }
        self.index += 1; // Skip 'e'
        Ok(BencodeValue::List(items))
    }

    fn decode_dict(&mut self) -> Result<BencodeValue, BencodeError> {
        self.index += 1; // Skip 'd'
        let mut map = BTreeMap::new();
        while self.peek()? != b'e' {
            let next_tok = self.peek()?;
            if !next_tok.is_ascii_digit() {
                return Err(BencodeError::InvalidDictKey);
            }
            let key = self.decode_bytes()?;
            let val = self.decode_next()?;
            map.insert(key, val);
        }
        self.index += 1; // Skip 'e'
        Ok(BencodeValue::Dict(map))
    }
}

pub fn bdecode(data: &[u8]) -> Result<BencodeValue, BencodeError> {
    Decoder::new(data).decode()
}

pub fn bencode(value: &BencodeValue) -> Vec<u8> {
    let mut out = Vec::with_capacity(128);
    encode_into(value, &mut out);
    out
}

fn encode_into(value: &BencodeValue, out: &mut Vec<u8>) {
    match value {
        BencodeValue::Int(n) => {
            out.push(b'i');
            out.extend_from_slice(n.to_string().as_bytes());
            out.push(b'e');
        }
        BencodeValue::Bytes(bytes) => {
            out.extend_from_slice(bytes.len().to_string().as_bytes());
            out.push(b':');
            out.extend_from_slice(bytes);
        }
        BencodeValue::List(items) => {
            out.push(b'l');
            for item in items {
                encode_into(item, out);
            }
            out.push(b'e');
        }
        BencodeValue::Dict(map) => {
            out.push(b'd');
            // BTreeMap<Vec<u8>, BencodeValue> iterates in lexicographical byte order per BEP 0003
            for (k, v) in map {
                out.extend_from_slice(k.len().to_string().as_bytes());
                out.push(b':');
                out.extend_from_slice(k);
                encode_into(v, out);
            }
            out.push(b'e');
        }
    }
}
