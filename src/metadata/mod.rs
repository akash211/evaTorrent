//! Discover media lookup service & persistent cache (Movies, TV, Books, Games).

pub mod cache;
pub mod service;

pub use service::{keys_configured, lookup_media};
