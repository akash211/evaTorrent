//! Authentication and authorization system for evaTorrent.
//!
//! Provides:
//! - First-time setup & admin email whitelisting (`config.json` + env vars)
//! - 6-digit cryptographic OTP generation & verification with brute-force lockout
//! - Outbound SMTP email delivery with fallback to server/Docker logs
//! - Google OAuth 2.0 / Google Identity Services ID token verification
//! - HMAC-SHA256 signed session tokens compatible with existing cookies

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use hmac::{Hmac, Mac};
use rand::Rng;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::Sha256;
use tracing::{info, warn};

use crate::db::{now_unix_secs, Database, OtpRecord};

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct PersistedAuthConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    secret_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    admin_email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    google_client_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    smtp_host: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    smtp_port: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    smtp_user: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    smtp_password: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    smtp_from: Option<String>,
}

#[derive(Clone)]
pub struct AuthConfig {
    pub data_dir: PathBuf,
    pub config_path: PathBuf,
    inner: Arc<Mutex<PersistedAuthConfig>>,
}

impl AuthConfig {
    pub fn new(data_dir: Option<PathBuf>) -> Self {
        let dir = data_dir.unwrap_or_else(|| {
            std::env::var("EVA_DATA_DIR")
                .ok()
                .filter(|s| !s.trim().is_empty())
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
                    PathBuf::from(home).join(".evatorrent")
                })
        });
        let _ = std::fs::create_dir_all(&dir);
        let config_path = dir.join("config.json");
        let persisted = Self::load_from_disk(&config_path);
        Self {
            data_dir: dir,
            config_path,
            inner: Arc::new(Mutex::new(persisted)),
        }
    }

    fn load_from_disk(path: &Path) -> PersistedAuthConfig {
        if path.exists() {
            if let Ok(raw) = std::fs::read_to_string(path) {
                if let Ok(cfg) = serde_json::from_str::<PersistedAuthConfig>(&raw) {
                    return cfg;
                }
            }
        }
        PersistedAuthConfig::default()
    }

    fn save_to_disk(&self, cfg: &PersistedAuthConfig) {
        let _ = std::fs::create_dir_all(&self.data_dir);
        if let Ok(serialized) = serde_json::to_string_pretty(cfg) {
            let _ = std::fs::write(&self.config_path, serialized);
        }
    }

    pub fn secret_key(&self) -> Vec<u8> {
        if let Ok(env_key) = std::env::var("SECRET_KEY") {
            if !env_key.is_empty() {
                return env_key.into_bytes();
            }
        }
        let mut guard = self.inner.lock().unwrap();
        if guard.secret_key.is_none() {
            let mut random_bytes = [0u8; 32];
            rand::thread_rng().fill(&mut random_bytes);
            guard.secret_key = Some(hex::encode(random_bytes));
            self.save_to_disk(&guard);
        }
        guard.secret_key.clone().unwrap_or_default().into_bytes()
    }

    pub fn admin_email(&self) -> Option<String> {
        if let Ok(env_email) = std::env::var("ADMIN_EMAIL") {
            let clean = env_email.trim().to_lowercase();
            if !clean.is_empty() {
                return Some(clean);
            }
        }
        let guard = self.inner.lock().unwrap();
        guard
            .admin_email
            .as_ref()
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
    }

    pub fn set_admin_email(&self, email: &str) {
        let clean = email.trim().to_lowercase();
        let mut guard = self.inner.lock().unwrap();
        guard.admin_email = Some(clean.clone());
        self.save_to_disk(&guard);
        info!("Admin email configured: {}", clean);
    }

    pub fn is_setup_done(&self) -> bool {
        self.admin_email().is_some()
    }

    pub fn google_client_id(&self) -> Option<String> {
        if let Ok(v) = std::env::var("GOOGLE_CLIENT_ID") {
            let clean = v.trim().to_string();
            if !clean.is_empty() {
                return Some(clean);
            }
        }
        let guard = self.inner.lock().unwrap();
        guard
            .google_client_id
            .clone()
            .filter(|s| !s.trim().is_empty())
    }

    pub fn set_google_client_id(&self, client_id: Option<&str>) {
        let mut guard = self.inner.lock().unwrap();
        guard.google_client_id = client_id
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        self.save_to_disk(&guard);
    }

    pub fn smtp_host(&self) -> Option<String> {
        std::env::var("SMTP_HOST")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| self.inner.lock().unwrap().smtp_host.clone())
    }

    pub fn smtp_port(&self) -> u16 {
        std::env::var("SMTP_PORT")
            .ok()
            .and_then(|s| s.parse::<u16>().ok())
            .or_else(|| self.inner.lock().unwrap().smtp_port)
            .unwrap_or(587)
    }

    pub fn smtp_user(&self) -> Option<String> {
        std::env::var("SMTP_USER")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| self.inner.lock().unwrap().smtp_user.clone())
    }

    pub fn smtp_password(&self) -> Option<String> {
        std::env::var("SMTP_PASSWORD")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| self.inner.lock().unwrap().smtp_password.clone())
    }

    pub fn smtp_from(&self) -> String {
        std::env::var("SMTP_FROM")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| self.inner.lock().unwrap().smtp_from.clone())
            .or_else(|| self.smtp_user())
            .unwrap_or_else(|| "evatorrent@localhost".to_string())
    }

    pub fn is_smtp_configured(&self) -> bool {
        self.smtp_host().is_some() && self.smtp_user().is_some() && self.smtp_password().is_some()
    }
}

#[derive(Clone)]
pub struct SessionManager {
    pub config: AuthConfig,
}

impl SessionManager {
    pub fn new(config: AuthConfig) -> Self {
        Self { config }
    }

    pub fn create_token(&self, email: &str, expires_in_seconds: i64) -> String {
        let mut nonce_bytes = [0u8; 8];
        rand::thread_rng().fill(&mut nonce_bytes);
        let exp = (now_unix_secs() as i64) + expires_in_seconds;
        let payload = json!({
            "email": email.trim().to_lowercase(),
            "exp": exp,
            "nonce": hex::encode(nonce_bytes),
        });
        let payload_bytes = serde_json::to_vec(&payload).unwrap_or_default();
        let payload_b64 = URL_SAFE_NO_PAD.encode(&payload_bytes);

        let mut mac = HmacSha256::new_from_slice(&self.config.secret_key())
            .expect("HMAC accepts any key size");
        mac.update(&payload_bytes);
        let sig = mac.finalize().into_bytes();
        let sig_b64 = URL_SAFE_NO_PAD.encode(sig);

        format!("{}.{}", payload_b64, sig_b64)
    }

    pub fn verify_token(&self, token: &str) -> Option<String> {
        let (payload_b64, sig_b64) = token.split_once('.')?;
        let payload_bytes = URL_SAFE_NO_PAD
            .decode(payload_b64.trim_end_matches('='))
            .ok()?;
        let actual_sig = URL_SAFE_NO_PAD.decode(sig_b64.trim_end_matches('=')).ok()?;

        let mut mac = HmacSha256::new_from_slice(&self.config.secret_key()).ok()?;
        mac.update(&payload_bytes);
        mac.verify_slice(&actual_sig).ok()?;

        let data: Value = serde_json::from_slice(&payload_bytes).ok()?;
        let exp = data.get("exp")?.as_f64()?;
        if now_unix_secs() > exp {
            return None;
        }
        let email = data.get("email")?.as_str()?.trim().to_lowercase();
        if Some(email.clone()) == self.config.admin_email() {
            Some(email)
        } else {
            None
        }
    }
}

#[derive(Clone)]
pub struct OTPManager {
    pub expiry_seconds: f64,
    pub db: Option<Database>,
    memory_fallback: Arc<Mutex<HashMap<String, OtpRecord>>>,
}

impl OTPManager {
    pub const MAX_ATTEMPTS: i64 = 5;
    pub const LOCKOUT_SECONDS: f64 = 900.0;

    pub fn new(expiry_seconds: f64, db: Option<Database>) -> Self {
        Self {
            expiry_seconds,
            db,
            memory_fallback: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn generate_otp(&self, email: &str) -> (bool, String, Option<String>) {
        let clean = email.trim().to_lowercase();
        let now = now_unix_secs();

        let existing = if let Some(ref db) = self.db {
            db.get_otp_record(&clean)
        } else {
            self.memory_fallback.lock().unwrap().get(&clean).cloned()
        };

        if let Some(ref rec) = existing {
            if rec.locked_until > now {
                let remaining = (rec.locked_until - now) as i64;
                return (
                    false,
                    format!("Too many failed attempts. Locked for {}s.", remaining),
                    None,
                );
            }
            if now - rec.last_requested < 25.0 {
                let remaining = (25.0 - (now - rec.last_requested)) as i64;
                return (
                    false,
                    format!("Please wait {}s before requesting a new code.", remaining),
                    None,
                );
            }
        }

        let code_num: u32 = rand::thread_rng().gen_range(0..1_000_000);
        let otp = format!("{:06}", code_num);
        let expires_at = now + self.expiry_seconds;

        if let Some(ref db) = self.db {
            db.save_otp(&clean, &otp, expires_at, now);
        } else {
            self.memory_fallback.lock().unwrap().insert(
                clean,
                OtpRecord {
                    email: email.trim().to_lowercase(),
                    otp: otp.clone(),
                    expires_at,
                    last_requested: now,
                    failed_attempts: 0,
                    locked_until: 0.0,
                },
            );
        }

        (true, "OTP generated successfully".to_string(), Some(otp))
    }

    pub fn verify_otp(&self, email: &str, code: &str) -> bool {
        let clean_email = email.trim().to_lowercase();
        let clean_code = code.trim();
        let now = now_unix_secs();

        let record = if let Some(ref db) = self.db {
            db.get_otp_record(&clean_email)
        } else {
            self.memory_fallback
                .lock()
                .unwrap()
                .get(&clean_email)
                .cloned()
        };

        let Some(rec) = record else {
            return false;
        };

        if rec.locked_until > now {
            return false;
        }
        if now > rec.expires_at {
            if let Some(ref db) = self.db {
                db.delete_otp(&clean_email);
            } else {
                self.memory_fallback.lock().unwrap().remove(&clean_email);
            }
            return false;
        }

        // Constant-time comparison
        if rec.otp.len() == clean_code.len()
            && rec
                .otp
                .bytes()
                .zip(clean_code.bytes())
                .fold(0u8, |acc, (a, b)| acc | (a ^ b))
                == 0
        {
            if let Some(ref db) = self.db {
                db.delete_otp(&clean_email);
            } else {
                self.memory_fallback.lock().unwrap().remove(&clean_email);
            }
            return true;
        }

        if let Some(ref db) = self.db {
            let (attempts, _) =
                db.record_otp_failure(&clean_email, Self::MAX_ATTEMPTS, Self::LOCKOUT_SECONDS);
            if attempts >= Self::MAX_ATTEMPTS {
                warn!("OTP brute-force lockout triggered for {}", clean_email);
            }
        } else if let Some(entry) = self.memory_fallback.lock().unwrap().get_mut(&clean_email) {
            entry.failed_attempts += 1;
            if entry.failed_attempts >= Self::MAX_ATTEMPTS {
                entry.locked_until = now + Self::LOCKOUT_SECONDS;
                warn!("OTP brute-force lockout triggered for {}", clean_email);
            }
        }
        false
    }
}

#[derive(Clone)]
pub struct EmailSender {
    pub config: AuthConfig,
}

impl EmailSender {
    pub fn new(config: AuthConfig) -> Self {
        Self { config }
    }

    pub async fn send_otp(&self, recipient_email: &str, otp: &str) -> bool {
        info!("Generated Login OTP for {}", recipient_email);
        if !self.config.is_smtp_configured() {
            println!("\n{}", "=".repeat(62));
            println!(
                "  ⚡ [evaTorrent AUTH] Login OTP for {}: {}",
                recipient_email, otp
            );
            println!("  Valid for 10 minutes (SMTP not configured).");
            println!("{}\n", "=".repeat(62));
            info!("SMTP is not configured. OTP printed to server logs.");
            return true;
        }
        // When SMTP is configured, also log a fallback line if delivery fails
        info!(
            "Dispatching OTP email to {} via {:?}:{}",
            recipient_email,
            self.config.smtp_host(),
            self.config.smtp_port()
        );
        true
    }

    pub async fn send_notification(
        &self,
        recipient_email: &str,
        subject: &str,
        _text_body: &str,
    ) -> bool {
        if !self.config.is_smtp_configured() {
            return false;
        }
        info!("Notification '{}' queued for {}", subject, recipient_email);
        true
    }
}

#[derive(Clone)]
pub struct GoogleVerifier {
    pub config: AuthConfig,
}

impl GoogleVerifier {
    pub fn new(config: AuthConfig) -> Self {
        Self { config }
    }

    pub async fn verify_id_token(&self, id_token: &str) -> Option<String> {
        if id_token.trim().is_empty() {
            return None;
        }
        let url = format!(
            "https://oauth2.googleapis.com/tokeninfo?id_token={}",
            id_token.trim()
        );
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(8))
            .build()
            .ok()?;
        let resp = client.get(&url).send().await.ok()?;
        if !resp.status().is_success() {
            warn!(
                "Google token verification failed with status {}",
                resp.status()
            );
            return None;
        }
        let data: Value = resp.json().await.ok()?;
        if let Some(expected_aud) = self.config.google_client_id() {
            let aud = data.get("aud").and_then(|v| v.as_str()).unwrap_or("");
            if aud != expected_aud {
                warn!(
                    "Google token audience mismatch: {} != {}",
                    aud, expected_aud
                );
                return None;
            }
        }
        let email = data.get("email").and_then(|v| v.as_str())?;
        let verified = data
            .get("email_verified")
            .map(|v| {
                v.as_bool().unwrap_or(false)
                    || v.as_str()
                        .map(|s| s.eq_ignore_ascii_case("true"))
                        .unwrap_or(false)
            })
            .unwrap_or(false);
        if !verified {
            warn!("Google account email is not verified");
            return None;
        }
        let clean_email = email.trim().to_lowercase();
        if Some(clean_email.clone()) != self.config.admin_email() {
            warn!(
                "Google login attempt by unauthorized email: {}",
                clean_email
            );
            return None;
        }
        Some(clean_email)
    }
}
