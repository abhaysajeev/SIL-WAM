"""
Agent app authentication — /agent-api/v1/auth/*.

Two groups matter most and are easy to get wrong:

  TestLockoutScope proves a wrong DEVICE or LICENCE never counts toward account
  lockout. Counting them would lock out an agent whose phone was replaced — turning a
  support call into a worse one — and nothing else in the suite would catch it.

  TestDeviceBinding proves the one-device rule in both directions: an agent cannot
  bring a second handset, and a handset cannot serve a second agent.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models.agent import Agent, AgentSession
from tests.conftest import login, make_company, make_user

LOGIN = "/agent-api/v1/auth/login"
SESSION = "/agent-api/v1/auth/session"
LOGOUT = "/agent-api/v1/auth/logout"

DEVICE = "9774d56d682e549c"
CUSTOMER = "FM-2291"


def _agent(db, username="ravi.k", agent_id="AG01", customer_id=CUSTOMER,
           device_id=None, is_active=True, company=None, user_active=True):
    """A user with role 'agent' plus the agents row the app login requires."""
    company = company or make_company(db, code=f"AG{username[:4].upper()}")
    user = make_user(db, username=username, role_name="agent", company_id=company.id)
    if not user_active:
        user.is_active = False
    row = Agent(
        user_id=user.id, agent_id=agent_id, customer_id=customer_id,
        device_id=device_id, is_active=is_active,
        device_bound_at=datetime.now(timezone.utc) if device_id else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return user, row, company


def _body(username="ravi.k", password="testpass123", customer_id=CUSTOMER, device_id=DEVICE):
    return {"username": username, "password": password,
            "customer_id": customer_id, "device_id": device_id}


def _hdr(token, device_id=DEVICE):
    return {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}


def _code(response) -> str:
    return response.json()["detail"]["code"]


# ── Credentials ───────────────────────────────────────────────────────────────

class TestCredentials:
    def test_unknown_username_returns_401(self, client, db):
        r = client.post(LOGIN, json=_body(username="nobody"))
        assert r.status_code == 401
        assert _code(r) == "invalid_credentials"

    def test_wrong_password_returns_401(self, client, db):
        _agent(db)
        r = client.post(LOGIN, json=_body(password="wrong"))
        assert r.status_code == 401
        assert _code(r) == "invalid_credentials"

    def test_unknown_username_and_wrong_password_are_indistinguishable(self, client, db):
        """Anything else is username enumeration."""
        _agent(db)
        unknown = client.post(LOGIN, json=_body(username="nobody"))
        wrong   = client.post(LOGIN, json=_body(password="wrong"))
        assert unknown.status_code == wrong.status_code
        assert unknown.json() == wrong.json()

    def test_disabled_user_returns_403(self, client, db):
        _agent(db, user_active=False)
        db.commit()
        r = client.post(LOGIN, json=_body())
        assert r.status_code == 403
        assert _code(r) == "account_disabled"

    def test_locked_account_returns_403(self, client, db):
        user, _a, _c = _agent(db)
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        db.commit()
        r = client.post(LOGIN, json=_body())
        assert r.status_code == 403
        assert _code(r) == "account_locked"

    def test_successful_login_clears_failed_attempts(self, client, db):
        user, _a, _c = _agent(db)
        client.post(LOGIN, json=_body(password="wrong"))
        db.refresh(user)
        assert user.failed_login_attempts == 1

        assert client.post(LOGIN, json=_body()).status_code == 200
        db.refresh(user)
        assert user.failed_login_attempts == 0


# ── The check that protects real agents ───────────────────────────────────────

class TestLockoutScope:
    """
    Only credential failures count toward lockout.

    A wrong device is what happens when someone's phone is replaced. Counting it would
    lock the account after five attempts by an agent who did nothing wrong.
    """

    def test_wrong_password_increments(self, client, db):
        user, _a, _c = _agent(db)
        client.post(LOGIN, json=_body(password="wrong"))
        db.refresh(user)
        assert user.failed_login_attempts == 1

    def test_device_mismatch_does_not_increment(self, client, db):
        user, _a, _c = _agent(db, device_id="old-phone")
        r = client.post(LOGIN, json=_body(device_id="new-phone"))
        assert _code(r) == "device_mismatch"
        db.refresh(user)
        assert user.failed_login_attempts == 0

    def test_licence_mismatch_does_not_increment(self, client, db):
        user, _a, _c = _agent(db)
        r = client.post(LOGIN, json=_body(customer_id="WRONG"))
        assert _code(r) == "licence_mismatch"
        db.refresh(user)
        assert user.failed_login_attempts == 0

    def test_repeated_device_failures_never_lock(self, client, db):
        user, _a, _c = _agent(db, device_id="old-phone")
        for _ in range(settings.MAX_LOGIN_ATTEMPTS + 2):
            client.post(LOGIN, json=_body(device_id="new-phone"))
        db.refresh(user)
        assert user.locked_until is None
        # The real handset still works.
        assert client.post(LOGIN, json=_body(device_id="old-phone")).status_code == 200

    def test_repeated_password_failures_do_lock(self, client, db):
        _agent(db)
        for _ in range(settings.MAX_LOGIN_ATTEMPTS):
            client.post(LOGIN, json=_body(password="wrong"))
        r = client.post(LOGIN, json=_body())          # correct password now
        assert r.status_code == 403
        assert _code(r) == "account_locked"


# ── Agent gate and licence ────────────────────────────────────────────────────

class TestAgentGate:
    def test_user_without_agent_row_returns_403(self, client, db):
        make_user(db, username="plain", role_name="agent")
        r = client.post(LOGIN, json=_body(username="plain"))
        assert r.status_code == 403
        assert _code(r) == "not_an_agent"

    def test_deactivated_agent_returns_403(self, client, db):
        _agent(db, is_active=False)
        r = client.post(LOGIN, json=_body())
        assert r.status_code == 403
        assert _code(r) == "not_an_agent"

    def test_wrong_customer_id_returns_403(self, client, db):
        _agent(db)
        r = client.post(LOGIN, json=_body(customer_id="SOMEONE-ELSE"))
        assert r.status_code == 403
        assert _code(r) == "licence_mismatch"


# ── Device binding ────────────────────────────────────────────────────────────

class TestDeviceBinding:
    def test_first_login_binds_the_device(self, client, db):
        _user, agent, _c = _agent(db)                 # device_id starts NULL
        r = client.post(LOGIN, json=_body())
        assert r.status_code == 200
        body = r.json()
        assert body["device"]["device_id"] == DEVICE
        assert body["device"]["newly_bound"] is True
        assert body["device"]["bound_at"] is not None

        db.refresh(agent)
        assert agent.device_id == DEVICE

    def test_second_login_from_same_device_is_not_newly_bound(self, client, db):
        _agent(db)
        client.post(LOGIN, json=_body())
        r = client.post(LOGIN, json=_body())
        assert r.status_code == 200
        assert r.json()["device"]["newly_bound"] is False

    def test_different_device_returns_403(self, client, db):
        _agent(db, device_id="bound-phone")
        r = client.post(LOGIN, json=_body(device_id="another-phone"))
        assert r.status_code == 403
        assert _code(r) == "device_mismatch"

    def test_device_belonging_to_another_agent_returns_403(self, client, db):
        """The duplicate check — one handset cannot serve two agents."""
        company = make_company(db, code="SHARED")
        _agent(db, username="agent.one", agent_id="AG01",
               device_id=DEVICE, company=company)
        _agent(db, username="agent.two", agent_id="AG02", company=company)

        r = client.post(LOGIN, json=_body(username="agent.two"))
        assert r.status_code == 403
        assert _code(r) == "device_already_registered"

    def test_a_free_device_is_allowed(self, client, db):
        """The other half of the pair — only a taken device is refused."""
        company = make_company(db, code="SHARED2")
        _agent(db, username="agent.one", agent_id="AG01",
               device_id="phone-one", company=company)
        _agent(db, username="agent.two", agent_id="AG02", company=company)

        r = client.post(LOGIN, json=_body(username="agent.two", device_id="phone-two"))
        assert r.status_code == 200
        assert r.json()["device"]["newly_bound"] is True

    def test_the_same_device_is_refused_across_companies(self, client, db):
        """ANDROID_ID identifies one physical handset — the rule is global."""
        _agent(db, username="a.one", agent_id="AG01", device_id=DEVICE,
               company=make_company(db, code="COMPA"))
        _agent(db, username="a.two", agent_id="AG01",
               company=make_company(db, code="COMPB"))

        r = client.post(LOGIN, json=_body(username="a.two"))
        assert _code(r) == "device_already_registered"


# ── The login response ────────────────────────────────────────────────────────

class TestLoginResponse:
    def test_returns_identity_so_the_app_needs_no_second_call(self, client, db):
        _agent(db)
        body = client.post(LOGIN, json=_body()).json()
        assert body["agent"]["username"] == "ravi.k"
        assert body["agent"]["agent_id"] == "AG01"
        assert body["agent"]["company_id"] is not None
        assert body["agent"]["company_name"]

    def test_expires_at_is_null_and_server_time_is_present(self, client, db):
        """No absolute expiry; server_time is the clock-offset baseline."""
        _agent(db)
        body = client.post(LOGIN, json=_body()).json()
        assert body["expires_at"] is None
        assert body["server_time"]
        assert body["token"]

    def test_token_is_stored_hashed_never_raw(self, client, db):
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        rows = db.query(AgentSession).all()
        assert len(rows) == 1
        assert rows[0].token_hash != token
        assert len(rows[0].token_hash) == 64


# ── Sessions ──────────────────────────────────────────────────────────────────

class TestSessions:
    def test_session_endpoint_returns_identity(self, client, db):
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        r = client.get(SESSION, headers=_hdr(token))
        assert r.status_code == 200
        assert r.json()["agent"]["agent_id"] == "AG01"
        assert r.json()["token"] is None       # the caller already holds one

    def test_login_supersedes_the_previous_session(self, client, db):
        _agent(db)
        first = client.post(LOGIN, json=_body()).json()["token"]
        second = client.post(LOGIN, json=_body()).json()["token"]
        assert first != second
        assert client.get(SESSION, headers=_hdr(first)).status_code == 401
        assert client.get(SESSION, headers=_hdr(second)).status_code == 200

    def test_logout_revokes_only_this_session(self, client, db):
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        assert client.post(LOGOUT, headers=_hdr(token)).status_code == 204
        assert client.get(SESSION, headers=_hdr(token)).status_code == 401

    def test_unknown_token_returns_401(self, client, db):
        assert client.get(SESSION, headers=_hdr("not-a-real-token")).status_code == 401

    def test_missing_authorization_returns_401(self, client, db):
        assert client.get(SESSION, headers={"X-Device-Id": DEVICE}).status_code == 401

    def test_deactivating_the_agent_kills_the_session_immediately(self, client, db):
        """Why no absolute expiry is needed: revocation already takes one request."""
        _user, agent, _c = _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        assert client.get(SESSION, headers=_hdr(token)).status_code == 200

        agent.is_active = False
        db.commit()
        assert client.get(SESSION, headers=_hdr(token)).status_code == 401

    def test_disabling_the_user_kills_the_session_immediately(self, client, db):
        user, _a, _c = _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        user.is_active = False
        db.commit()
        assert client.get(SESSION, headers=_hdr(token)).status_code == 401


# ── The device header, on every request ───────────────────────────────────────

class TestDeviceHeader:
    def test_missing_device_header_returns_401(self, client, db):
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        r = client.get(SESSION, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    def test_wrong_device_header_returns_401(self, client, db):
        """A token lifted off the handset must not work from anywhere else."""
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        assert client.get(SESSION, headers=_hdr(token, "someone-elses-phone")).status_code == 401


# ── Idle expiry ───────────────────────────────────────────────────────────────

class TestIdleExpiry:
    def test_a_session_idle_past_the_cutoff_returns_401(self, client, db):
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        session = db.query(AgentSession).first()
        session.last_used_at = (
            datetime.now(timezone.utc)
            - timedelta(days=settings.AGENT_SESSION_IDLE_DAYS + 1)
        )
        db.commit()
        assert client.get(SESSION, headers=_hdr(token)).status_code == 401

    def test_a_recently_used_session_still_works(self, client, db):
        _agent(db)
        token = client.post(LOGIN, json=_body()).json()["token"]
        session = db.query(AgentSession).first()
        session.last_used_at = datetime.now(timezone.utc) - timedelta(days=5)
        db.commit()
        assert client.get(SESSION, headers=_hdr(token)).status_code == 200


# ── The web panel refuses agents ──────────────────────────────────────────────

class TestWebLoginRefusesAgents:
    def test_agent_cannot_log_into_the_web_panel(self, client, db):
        _agent(db)
        r = client.post("/api/auth/login",
                        json={"username": "ravi.k", "password": "testpass123"})
        assert r.status_code == 403
        assert "agent app" in r.json()["detail"].lower()

    def test_non_agent_web_login_is_unaffected(self, client, db):
        make_user(db, username="webuser", role_name="admin")
        assert login(client, "webuser")["access_token"]
