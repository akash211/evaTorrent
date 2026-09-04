import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from evatorrent.auth import AuthConfig, OTPManager, SessionManager
from evatorrent.web.app import app, auth_config, session_manager, otp_manager


def test_auth_config_and_session():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = AuthConfig(data_dir=Path(tmp))
        assert not cfg.is_setup_done
        assert cfg.admin_email is None

        cfg.set_admin_email("test@example.com")
        assert cfg.is_setup_done
        assert cfg.admin_email == "test@example.com"

        sm = SessionManager(cfg)
        token = sm.create_token("test@example.com")
        assert sm.verify_token(token) == "test@example.com"

        # Invalid or forged token
        assert sm.verify_token("invalid.token") is None
        assert sm.verify_token(token + "extra") is None


def test_otp_manager():
    om = OTPManager(expiry_seconds=60)
    success, msg, otp = om.generate_otp("user@example.com")
    assert success is True
    assert len(otp) == 6
    assert otp.isdigit()

    # Rate limiting
    success2, msg2, _ = om.generate_otp("user@example.com")
    assert success2 is False
    assert "wait" in msg2

    # Verify invalid OTP
    assert om.verify_otp("user@example.com", "000000") is False

    # Verify correct OTP
    assert om.verify_otp("user@example.com", otp) is True

    # Once consumed, cannot reuse
    assert om.verify_otp("user@example.com", otp) is False


def test_auth_endpoints_and_route_protection():
    with tempfile.TemporaryDirectory() as tmp:
        # Re-point auth_config for isolated test
        test_cfg = AuthConfig(data_dir=Path(tmp))
        auth_config.config_path = test_cfg.config_path
        auth_config.data_dir = test_cfg.data_dir
        auth_config._persisted = {}

        client = TestClient(app)

        # 1. Check status before setup
        resp = client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["setup_required"] is True

        # 2. Protected endpoint without auth -> 401 SETUP_REQUIRED
        assert client.get("/api/torrents").status_code == 401

        # 3. Setup with invalid email
        resp_setup_err = client.post("/api/auth/setup", json={"admin_email": "invalid"})
        assert resp_setup_err.status_code == 400

        # 4. Valid setup
        resp_setup = client.post("/api/auth/setup", json={"admin_email": "admin@example.com"})
        assert resp_setup.status_code == 200
        token = resp_setup.json()["token"]
        assert token is not None

        # 5. Access with valid token header
        resp_torrents = client.get(
            "/api/torrents",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp_torrents.status_code == 200
        assert isinstance(resp_torrents.json(), list)

        # 6. Request OTP for unauthorized email -> 403
        resp_otp_bad = client.post("/api/auth/otp/request", json={"email": "hacker@evil.com"})
        assert resp_otp_bad.status_code == 403

        # 7. Request OTP for admin email -> 200
        resp_otp_ok = client.post("/api/auth/otp/request", json={"email": "admin@example.com"})
        assert resp_otp_ok.status_code == 200

        # Retrieve generated OTP from manager for test
        record = otp_manager.db.get_otp_record("admin@example.com") if otp_manager.db else otp_manager._otps.get("admin@example.com")
        assert record is not None
        code = record["otp"]

        # 8. Verify OTP -> gets session cookie and token
        resp_verify = client.post(
            "/api/auth/otp/verify",
            json={"email": "admin@example.com", "otp": code},
        )
        assert resp_verify.status_code == 200
        assert "evatorrent_session" in resp_verify.cookies
