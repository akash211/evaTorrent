"""Authentication and authorization system for evaTorrent.

Provides:
- First-time setup & admin email whitelisting
- 6-digit cryptographic OTP generation & verification
- Outbound SMTP email delivery with fallback to server/docker logs
- Google OAuth 2.0 / Google Identity Services ID token verification
- HMAC-signed secure session tokens
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("evatorrent.auth")

DEFAULT_DATA_DIR = Path(os.environ.get("EVA_DATA_DIR", Path.home() / ".evatorrent"))
CONFIG_FILE = DEFAULT_DATA_DIR / "config.json"


class AuthConfig:
    """Manages application auth configuration from environment variables or persistent storage."""

    def __init__(self, data_dir: Path = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "config.json"
        self._persisted: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            try:
                with self.config_path.open("r", encoding="utf-8") as f:
                    self._persisted = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read auth config from {self.config_path}: {e}")
                self._persisted = {}
        else:
            self._persisted = {}

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.config_path.open("w", encoding="utf-8") as f:
                json.dump(self._persisted, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save auth config to {self.config_path}: {e}")

    @property
    def secret_key(self) -> bytes:
        env_key = os.environ.get("SECRET_KEY")
        if env_key:
            return env_key.encode("utf-8")
        if "secret_key" not in self._persisted:
            self._persisted["secret_key"] = secrets.token_hex(32)
            self._save()
        return self._persisted["secret_key"].encode("utf-8")

    @property
    def admin_email(self) -> Optional[str]:
        env_email = os.environ.get("ADMIN_EMAIL")
        if env_email and env_email.strip():
            return env_email.strip().lower()
        persisted_email = self._persisted.get("admin_email")
        if persisted_email and str(persisted_email).strip():
            return str(persisted_email).strip().lower()
        return None

    def set_admin_email(self, email: str) -> None:
        clean_email = email.strip().lower()
        self._persisted["admin_email"] = clean_email
        self._save()
        logger.info(f"Admin email configured: {clean_email}")

    @property
    def is_setup_done(self) -> bool:
        return self.admin_email is not None

    @property
    def google_client_id(self) -> Optional[str]:
        return os.environ.get("GOOGLE_CLIENT_ID") or self._persisted.get("google_client_id")

    def set_google_client_id(self, client_id: Optional[str]) -> None:
        if client_id:
            self._persisted["google_client_id"] = client_id.strip()
        else:
            self._persisted.pop("google_client_id", None)
        self._save()

    @property
    def smtp_host(self) -> Optional[str]:
        return os.environ.get("SMTP_HOST") or self._persisted.get("smtp_host")

    @property
    def smtp_port(self) -> int:
        return int(os.environ.get("SMTP_PORT") or self._persisted.get("smtp_port") or 587)

    @property
    def smtp_user(self) -> Optional[str]:
        return os.environ.get("SMTP_USER") or self._persisted.get("smtp_user")

    @property
    def smtp_password(self) -> Optional[str]:
        return os.environ.get("SMTP_PASSWORD") or self._persisted.get("smtp_password")

    @property
    def smtp_from(self) -> str:
        return (
            os.environ.get("SMTP_FROM")
            or self._persisted.get("smtp_from")
            or (self.smtp_user if self.smtp_user else "evatorrent@localhost")
        )

    @property
    def is_smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)


class SessionManager:
    """Creates and verifies secure HMAC-SHA256 signed session tokens."""

    def __init__(self, config: AuthConfig):
        self.config = config

    def create_token(self, email: str, expires_in_seconds: int = 86400 * 30) -> str:
        payload = {
            "email": email.strip().lower(),
            "exp": int(time.time()) + expires_in_seconds,
            "nonce": secrets.token_hex(8),
        }
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")

        sig = hmac.new(self.config.secret_key, payload_bytes, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")

        return f"{payload_b64}.{sig_b64}"

    def verify_token(self, token: str) -> Optional[str]:
        if not token or "." not in token:
            return None
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts

        # Add padding back
        pad = len(payload_b64) % 4
        if pad:
            payload_b64 += "=" * (4 - pad)
        pad_sig = len(sig_b64) % 4
        if pad_sig:
            sig_b64 += "=" * (4 - pad_sig)

        try:
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            expected_sig = hmac.new(self.config.secret_key, payload_bytes, hashlib.sha256).digest()
            actual_sig = base64.urlsafe_b64decode(sig_b64)
            if not hmac.compare_digest(expected_sig, actual_sig):
                return None

            data = json.loads(payload_bytes.decode("utf-8"))
            if time.time() > data.get("exp", 0):
                return None

            email = data.get("email")
            # Verify user is still authorized
            if email and email == self.config.admin_email:
                return email
            return None
        except Exception:
            return None


class OTPManager:
    """Generates and verifies one-time login passwords."""

    def __init__(self, expiry_seconds: int = 600):
        self.expiry_seconds = expiry_seconds
        # email -> {"otp": "123456", "expires_at": float, "last_requested": float}
        self._otps: Dict[str, Dict[str, Any]] = {}

    def generate_otp(self, email: str) -> Tuple[bool, str, Optional[str]]:
        clean_email = email.strip().lower()
        now = time.time()

        existing = self._otps.get(clean_email)
        if existing and (now - existing["last_requested"]) < 25.0:
            remaining = int(25.0 - (now - existing["last_requested"]))
            return False, f"Please wait {remaining}s before requesting a new code.", None

        # 6-digit random numeric code
        otp = f"{secrets.randbelow(1_000_000):06d}"
        self._otps[clean_email] = {
            "otp": otp,
            "expires_at": now + self.expiry_seconds,
            "last_requested": now,
        }
        return True, "OTP generated successfully", otp

    def verify_otp(self, email: str, code: str) -> bool:
        clean_email = email.strip().lower()
        clean_code = code.strip()

        record = self._otps.get(clean_email)
        if not record:
            return False

        if time.time() > record["expires_at"]:
            self._otps.pop(clean_email, None)
            return False

        if hmac.compare_digest(record["otp"], clean_code):
            # Consume OTP
            self._otps.pop(clean_email, None)
            return True

        return False


class EmailSender:
    """Sends OTP via SMTP, with automatic fallback to logging to Docker/server console."""

    def __init__(self, config: AuthConfig):
        self.config = config

    async def send_otp(self, recipient_email: str, otp: str) -> bool:
        """Sends OTP via SMTP if configured, otherwise prints to console."""
        # Print to console/docker log always for easy dev & recovery access
        print("\n" + "=" * 62, flush=True)
        print(f"  ⚡ [evaTorrent AUTH] Login OTP for {recipient_email}: {otp}", flush=True)
        print(f"  Valid for 10 minutes.", flush=True)
        print("=" * 62 + "\n", flush=True)

        logger.info(f"Generated Login OTP for {recipient_email}")

        if not self.config.is_smtp_configured:
            logger.info("SMTP is not configured. OTP printed to server logs.")
            return True

        return await asyncio.to_thread(self._send_smtp_sync, recipient_email, otp)

    def _send_smtp_sync(self, recipient_email: str, otp: str) -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"evaTorrent Login Code: {otp}"
            msg["From"] = self.config.smtp_from
            msg["To"] = recipient_email

            text_content = (
                f"Hello,\n\n"
                f"Your one-time login code for evaTorrent is: {otp}\n\n"
                f"This code will expire in 10 minutes.\n"
                f"If you did not request this code, you can ignore this email."
            )
            html_content = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 480px; margin: 0 auto; background: #0f1117; color: #e2e8f0; padding: 32px; border-radius: 12px; border: 1px solid #1e293b;">
              <h2 style="color: #38bdf8; margin-top: 0;">evaTorrent ⚡</h2>
              <p style="font-size: 15px; color: #94a3b8;">Use the verification code below to sign in to your evaTorrent dashboard:</p>
              <div style="background: #1e293b; padding: 18px; text-align: center; border-radius: 8px; font-size: 32px; font-weight: 700; letter-spacing: 8px; color: #38bdf8; margin: 24px 0;">
                {otp}
              </div>
              <p style="font-size: 13px; color: #64748b; margin-bottom: 0;">This code is valid for 10 minutes. If you did not request this email, please ignore it.</p>
            </div>
            """

            msg.attach(MIMEText(text_content, "plain"))
            msg.attach(MIMEText(html_content, "html"))

            host = self.config.smtp_host
            port = self.config.smtp_port

            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=10.0) as server:
                    if self.config.smtp_user and self.config.smtp_password:
                        server.login(self.config.smtp_user, self.config.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=10.0) as server:
                    server.starttls()
                    if self.config.smtp_user and self.config.smtp_password:
                        server.login(self.config.smtp_user, self.config.smtp_password)
                    server.send_message(msg)

            logger.info(f"Successfully sent OTP email to {recipient_email} via {host}:{port}")
            return True
        except Exception as e:
            logger.error(f"Failed to send SMTP email to {recipient_email}: {e}")
            return False


class GoogleVerifier:
    """Verifies Google ID Tokens using Google's tokeninfo API."""

    def __init__(self, config: AuthConfig):
        self.config = config

    async def verify_id_token(self, id_token: str) -> Optional[str]:
        """Verifies Google ID token and returns the verified email address."""
        if not id_token:
            return None

        token_info_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(token_info_url)
                if resp.status_code != 200:
                    logger.warning(f"Google token verification failed with status {resp.status_code}")
                    return None

                data = resp.json()
                # If Google Client ID is configured, check audience
                if self.config.google_client_id:
                    aud = data.get("aud")
                    if aud != self.config.google_client_id:
                        logger.warning(f"Google token audience mismatch: {aud} != {self.config.google_client_id}")
                        return None

                email = data.get("email")
                email_verified = data.get("email_verified")
                if not email or str(email_verified).lower() != "true":
                    logger.warning("Google account email is not verified")
                    return None

                clean_email = email.strip().lower()
                # Verify that email matches authorized admin email
                if clean_email != self.config.admin_email:
                    logger.warning(f"Google login attempt by unauthorized email: {clean_email}")
                    return None

                return clean_email
        except Exception as e:
            logger.error(f"Error during Google token verification: {e}")
            return None
