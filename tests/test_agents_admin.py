"""
Admin API for agent configuration — /api/agents/*.

This is what the Agent Config tab talks to. The two endpoints that carry weight are
release-device and revoke: they exist because PUT /api/users/{id} applies
exclude_none=True and therefore cannot set a field back to NULL, which is exactly what
releasing a handset is.
"""
from datetime import datetime, timezone

from app.models.agent import Agent, AgentSession
from tests.conftest import login, make_company, make_user

LOGIN = "/agent-api/v1/auth/login"
DEVICE = "9774d56d682e549c"
CUSTOMER = "FM-2291"


def _admin(client, db):
    make_user(db, username="ag_admin", role_name="super_admin")
    tokens = login(client, "ag_admin")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _agent_user(db, username="ravi.k", company=None):
    company = company or make_company(db, code=f"AC{username[:4].upper()}")
    return make_user(db, username=username, role_name="agent", company_id=company.id), company


def _config(agent_id="AG01", customer_id=CUSTOMER, is_active=True):
    return {"agent_id": agent_id, "customer_id": customer_id, "is_active": is_active}


class TestConfigUpsert:
    def test_creates_the_configuration(self, client, db):
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        r = client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == "AG01"
        assert body["device_id"] is None          # binding happens at first login

    def test_updates_an_existing_configuration(self, client, db):
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        r = client.put(f"/api/agents/by-user/{user.id}", headers=hdr,
                       json=_config(agent_id="AG99", customer_id="FM-9999"))
        assert r.status_code == 200
        assert r.json()["agent_id"] == "AG99"
        assert db.query(Agent).count() == 1        # upsert, not a second row

    def test_get_returns_null_when_not_configured(self, client, db):
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        r = client.get(f"/api/agents/by-user/{user.id}", headers=hdr)
        assert r.status_code == 200
        assert r.json() is None

    def test_saving_does_not_disturb_the_bound_device(self, client, db):
        """The config form must never be able to change the binding by accident."""
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        client.post(LOGIN, json={"username": "ravi.k", "password": "testpass123",
                                 "customer_id": CUSTOMER, "device_id": DEVICE})

        r = client.put(f"/api/agents/by-user/{user.id}", headers=hdr,
                       json=_config(customer_id="FM-NEW"))
        assert r.json()["device_id"] == DEVICE

    def test_duplicate_agent_id_in_same_company_returns_409(self, client, db):
        hdr = _admin(client, db)
        company = make_company(db, code="DUP")
        one, _ = _agent_user(db, "a.one", company)
        two, _ = _agent_user(db, "a.two", company)

        assert client.put(f"/api/agents/by-user/{one.id}", headers=hdr,
                          json=_config(agent_id="AG01")).status_code == 200
        r = client.put(f"/api/agents/by-user/{two.id}", headers=hdr,
                       json=_config(agent_id="AG01"))
        assert r.status_code == 409

    def test_same_agent_id_allowed_in_different_companies(self, client, db):
        """The other half — AG01 may exist once per company."""
        hdr = _admin(client, db)
        one, _ = _agent_user(db, "b.one", make_company(db, code="COMP1"))
        two, _ = _agent_user(db, "b.two", make_company(db, code="COMP2"))

        for u in (one, two):
            r = client.put(f"/api/agents/by-user/{u.id}", headers=hdr,
                           json=_config(agent_id="AG01"))
            assert r.status_code == 200

    def test_deactivating_revokes_live_sessions(self, client, db):
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        token = client.post(LOGIN, json={"username": "ravi.k", "password": "testpass123",
                                         "customer_id": CUSTOMER,
                                         "device_id": DEVICE}).json()["token"]

        client.put(f"/api/agents/by-user/{user.id}", headers=hdr,
                   json=_config(is_active=False))

        r = client.get("/agent-api/v1/auth/session",
                       headers={"Authorization": f"Bearer {token}", "X-Device-Id": DEVICE})
        assert r.status_code == 401


class TestReleaseDevice:
    def _bound(self, client, db, hdr, username="ravi.k", device_id=DEVICE):
        user, _c = _agent_user(db, username)
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        token = client.post(LOGIN, json={"username": username, "password": "testpass123",
                                         "customer_id": CUSTOMER,
                                         "device_id": device_id}).json()["token"]
        agent = db.query(Agent).filter(Agent.user_id == user.id).first()
        return user, agent, token

    def test_clears_the_binding_and_revokes_sessions(self, client, db):
        hdr = _admin(client, db)
        _user, agent, token = self._bound(client, db, hdr)

        r = client.post(f"/api/agents/{agent.id}/release-device", headers=hdr)
        assert r.status_code == 200
        assert r.json()["released_device_id"] == DEVICE
        assert r.json()["sessions_revoked"] == 1

        db.refresh(agent)
        assert agent.device_id is None
        assert agent.device_bound_at is None

        assert client.get("/agent-api/v1/auth/session",
                          headers={"Authorization": f"Bearer {token}",
                                   "X-Device-Id": DEVICE}).status_code == 401

    def test_a_new_device_can_bind_afterwards(self, client, db):
        """The factory-reset recovery path, end to end."""
        hdr = _admin(client, db)
        _user, agent, _token = self._bound(client, db, hdr)
        client.post(f"/api/agents/{agent.id}/release-device", headers=hdr)

        r = client.post(LOGIN, json={"username": "ravi.k", "password": "testpass123",
                                     "customer_id": CUSTOMER, "device_id": "brand-new-phone"})
        assert r.status_code == 200
        assert r.json()["device"]["newly_bound"] is True

    def test_releases_the_device_for_another_agent_to_claim(self, client, db):
        hdr = _admin(client, db)
        company = make_company(db, code="HANDOVER")
        _u1, agent1, _t = self._bound(client, db, hdr, username="a.one")
        two, _ = _agent_user(db, "a.two", company)
        client.put(f"/api/agents/by-user/{two.id}", headers=hdr, json=_config(agent_id="AG02"))

        # Blocked while agent one holds it.
        blocked = client.post(LOGIN, json={"username": "a.two", "password": "testpass123",
                                           "customer_id": CUSTOMER, "device_id": DEVICE})
        assert blocked.json()["detail"]["code"] == "device_already_registered"

        client.post(f"/api/agents/{agent1.id}/release-device", headers=hdr)

        allowed = client.post(LOGIN, json={"username": "a.two", "password": "testpass123",
                                           "customer_id": CUSTOMER, "device_id": DEVICE})
        assert allowed.status_code == 200


class TestSessions:
    def test_lists_only_live_sessions(self, client, db):
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        body = {"username": "ravi.k", "password": "testpass123",
                "customer_id": CUSTOMER, "device_id": DEVICE}
        client.post(LOGIN, json=body)
        client.post(LOGIN, json=body)          # supersedes the first

        agent = db.query(Agent).filter(Agent.user_id == user.id).first()
        r = client.get(f"/api/agents/{agent.id}/sessions", headers=hdr)
        assert r.status_code == 200
        assert len(r.json()) == 1              # the superseded one is history

    def test_revoke_ends_one_session_without_unbinding(self, client, db):
        hdr = _admin(client, db)
        user, _c = _agent_user(db)
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json=_config())
        token = client.post(LOGIN, json={"username": "ravi.k", "password": "testpass123",
                                         "customer_id": CUSTOMER,
                                         "device_id": DEVICE}).json()["token"]
        session = db.query(AgentSession).filter(AgentSession.revoked.is_(False)).first()

        assert client.post(f"/api/agents/sessions/{session.id}/revoke",
                           headers=hdr).status_code == 204
        assert client.get("/agent-api/v1/auth/session",
                          headers={"Authorization": f"Bearer {token}",
                                   "X-Device-Id": DEVICE}).status_code == 401

        agent = db.query(Agent).filter(Agent.user_id == user.id).first()
        db.refresh(agent)
        assert agent.device_id == DEVICE       # a sign-out, not a release


class TestAccessControl:
    def test_requires_authentication(self, client, db):
        user, _c = _agent_user(db)
        assert client.get(f"/api/agents/by-user/{user.id}").status_code == 403

    def test_company_scoped_user_cannot_reach_another_company(self, client, db):
        theirs = make_company(db, code="THEIRS")
        mine   = make_company(db, code="MINE")
        target, _ = _agent_user(db, "their.agent", theirs)

        make_user(db, username="scoped", role_name="admin", company_id=mine.id)
        tokens = login(client, "scoped")
        hdr = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = client.get(f"/api/agents/by-user/{target.id}", headers=hdr)
        assert r.status_code == 403

    def test_unknown_user_returns_404(self, client, db):
        hdr = _admin(client, db)
        stub = "00000000-0000-0000-0000-000000000999"
        assert client.get(f"/api/agents/by-user/{stub}", headers=hdr).status_code == 404
