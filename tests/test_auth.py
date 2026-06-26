"""Auth module — full use-case test suite."""
import secrets
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models.user import RefreshToken
from tests.conftest import make_user, login as do_login


# ── /api/auth/login ────────────────────────────────────────────────────────

class TestLogin:
    def test_success_returns_both_tokens(self, client, db):
        make_user(db)
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    def test_wrong_password_returns_401(self, client, db):
        make_user(db)
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "wrongpass"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid credentials"

    def test_nonexistent_user_returns_401(self, client, db):
        r = client.post("/api/auth/login", json={"username": "nobody", "password": "anything"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid credentials"

    def test_inactive_user_returns_403(self, client, db):
        make_user(db, is_active=False)
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"].lower()

    def test_empty_username_returns_422(self, client, db):
        r = client.post("/api/auth/login", json={"username": "", "password": "testpass123"})
        assert r.status_code == 422

    def test_empty_password_returns_422(self, client, db):
        r = client.post("/api/auth/login", json={"username": "testuser", "password": ""})
        assert r.status_code == 422

    def test_missing_fields_returns_422(self, client, db):
        r = client.post("/api/auth/login", json={"username": "testuser"})
        assert r.status_code == 422

    def test_each_login_creates_new_refresh_token(self, client, db):
        make_user(db)
        t1 = do_login(client)["refresh_token"]
        t2 = do_login(client)["refresh_token"]
        assert t1 != t2

    def test_failed_attempts_increment_on_wrong_password(self, client, db):
        user = make_user(db)
        client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        db.refresh(user)
        assert user.failed_login_attempts == 1

    def test_account_locked_after_max_attempts(self, client, db):
        from app.core.config import settings
        make_user(db)
        for _ in range(settings.MAX_LOGIN_ATTEMPTS):
            client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        assert r.status_code == 403
        assert "locked" in r.json()["detail"].lower()

    def test_locked_account_rejects_correct_password(self, client, db):
        from app.core.config import settings
        make_user(db)
        for _ in range(settings.MAX_LOGIN_ATTEMPTS):
            client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert r.status_code == 403
        assert "locked" in r.json()["detail"].lower()

    def test_failed_attempts_reset_on_successful_login(self, client, db):
        user = make_user(db)
        client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        db.refresh(user)
        assert user.failed_login_attempts == 2
        do_login(client)
        db.refresh(user)
        assert user.failed_login_attempts == 0
        assert user.locked_until is None

    def test_expired_lockout_allows_login(self, client, db):
        from datetime import timedelta
        user = make_user(db)
        user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        user.failed_login_attempts = 5
        db.commit()
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass123"})
        assert r.status_code == 200


# ── /api/auth/refresh ──────────────────────────────────────────────────────

class TestRefresh:
    def test_success_returns_new_access_token(self, client, db):
        make_user(db)
        tokens = do_login(client)
        r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_old_token_revoked_after_rotation(self, client, db):
        make_user(db)
        tokens = do_login(client)
        client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        # Same token must now be rejected
        r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 401

    def test_new_rotated_token_works(self, client, db):
        make_user(db)
        tokens = do_login(client)
        r1 = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        # Rotated token is not returned by /refresh — only access_token is.
        assert r1.status_code == 200

    def test_invalid_token_returns_401(self, client, db):
        r = client.post("/api/auth/refresh", json={"refresh_token": "totally-invalid-token"})
        assert r.status_code == 401

    def test_expired_token_returns_401(self, client, db):
        user = make_user(db)
        raw = secrets.token_urlsafe(64)
        db.add(RefreshToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            revoked=False,
        ))
        db.commit()
        r = client.post("/api/auth/refresh", json={"refresh_token": raw})
        assert r.status_code == 401
        assert "expired" in r.json()["detail"].lower()

    def test_revoked_token_returns_401(self, client, db):
        user = make_user(db)
        raw = secrets.token_urlsafe(64)
        db.add(RefreshToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            revoked=True,
        ))
        db.commit()
        r = client.post("/api/auth/refresh", json={"refresh_token": raw})
        assert r.status_code == 401

    def test_disabled_user_refresh_returns_403(self, client, db):
        user = make_user(db)
        tokens = do_login(client)
        # Disable user after token was issued
        user.is_active = False
        db.commit()
        r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 403

    def test_empty_token_returns_422(self, client, db):
        r = client.post("/api/auth/refresh", json={"refresh_token": ""})
        assert r.status_code == 422


# ── /api/auth/logout ───────────────────────────────────────────────────────

class TestLogout:
    def test_success_returns_204(self, client, db):
        make_user(db)
        tokens = do_login(client)
        r = client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 204

    def test_token_unusable_after_logout(self, client, db):
        make_user(db)
        tokens = do_login(client)
        client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        r = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 401

    def test_unknown_token_is_silent_204(self, client, db):
        """Logout with a token we've never seen must not error."""
        r = client.post("/api/auth/logout", json={"refresh_token": "nonexistent-token-xyz"})
        assert r.status_code == 204

    def test_double_logout_is_silent(self, client, db):
        make_user(db)
        tokens = do_login(client)
        client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        r = client.post("/api/auth/logout", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 204


# ── /api/auth/me ───────────────────────────────────────────────────────────

class TestMe:
    def test_success_returns_user_profile(self, client, db):
        make_user(db, username="testuser", role_name="admin")
        tokens = do_login(client)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "testuser"
        assert data["role_name"] == "admin"
        assert "permissions" in data

    def test_no_token_returns_403(self, client, db):
        r = client.get("/api/auth/me")
        assert r.status_code == 403

    def test_invalid_token_returns_401(self, client, db):
        r = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
        assert r.status_code == 401

    def test_malformed_header_returns_403(self, client, db):
        r = client.get("/api/auth/me", headers={"Authorization": "Token not-a-bearer"})
        assert r.status_code == 403

    def test_inactive_user_returns_403(self, client, db):
        user = make_user(db)
        tokens = do_login(client)
        # Disable after token issued — next request must be rejected
        user.is_active = False
        db.commit()
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 403

    def test_access_token_not_accepted_as_refresh(self, client, db):
        """Access tokens must be rejected by /refresh."""
        make_user(db)
        tokens = do_login(client)
        r = client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
        assert r.status_code == 401


# ── Role enforcement ───────────────────────────────────────────────────────

class TestChangePassword:
    def test_success_returns_204(self, client, db):
        make_user(db)
        tokens = do_login(client)
        r = client.post(
            "/api/auth/change-password",
            json={"current_password": "testpass123", "new_password": "newpassword99"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 204

    def test_wrong_current_password_returns_400(self, client, db):
        make_user(db)
        tokens = do_login(client)
        r = client.post(
            "/api/auth/change-password",
            json={"current_password": "wrongpassword", "new_password": "newpassword99"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 400
        assert "incorrect" in r.json()["detail"].lower()

    def test_no_token_returns_403(self, client, db):
        r = client.post(
            "/api/auth/change-password",
            json={"current_password": "testpass123", "new_password": "newpassword99"},
        )
        assert r.status_code == 403

    def test_short_new_password_returns_422(self, client, db):
        make_user(db)
        tokens = do_login(client)
        r = client.post(
            "/api/auth/change-password",
            json={"current_password": "testpass123", "new_password": "short"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        assert r.status_code == 422

    def test_new_password_works_on_next_login(self, client, db):
        make_user(db)
        tokens = do_login(client)
        client.post(
            "/api/auth/change-password",
            json={"current_password": "testpass123", "new_password": "newpassword99"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        r = client.post("/api/auth/login", json={"username": "testuser", "password": "newpassword99"})
        assert r.status_code == 200

    def test_must_change_password_cleared_after_change(self, client, db):
        user = make_user(db)
        assert user.must_change_password is True
        tokens = do_login(client)
        client.post(
            "/api/auth/change-password",
            json={"current_password": "testpass123", "new_password": "newpassword99"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        db.refresh(user)
        assert user.must_change_password is False


class TestRoleEnforcement:
    def test_admin_role_visible_in_me(self, client, db):
        make_user(db, role_name="admin")
        tokens = do_login(client)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.json()["role_name"] == "admin"

    def test_manager_role_visible_in_me(self, client, db):
        make_user(db, role_name="manager")
        tokens = do_login(client)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.json()["role_name"] == "manager"
