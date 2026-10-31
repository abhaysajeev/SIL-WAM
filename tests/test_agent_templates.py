"""
Agent template mapping — /api/agents/by-user/*/templates and /agent-api/v1/templates.

The test that matters most is TestLiveStructure: the mapping stores an id, never a copy
of the template, so an edit at Meta reaches the app on the next request. A snapshot
would have looked identical in every other test here and only failed in production, as
a rejected send with a parameter-count error.
"""
from datetime import datetime, timezone

from app.models.agent import Agent, AgentTemplate
from app.models.whatsapp import WhatsAppTemplate
from tests.conftest import login, make_company, make_user, make_wa_template

LOGIN = "/agent-api/v1/auth/login"
AGENT_TEMPLATES = "/agent-api/v1/templates"
DEVICE = "9774d56d682e549c"
CUSTOMER = "FM-2291"

BODY = [
    {"type": "HEADER", "format": "TEXT", "text": "Order update"},
    {"type": "BODY",
     "text": "Hello {{1}}, your order {{2}} ships on {{3}}.",
     "example": {"body_text": [["Ravi", "SO-1201", "5 Sep"]]}},
    {"type": "FOOTER", "text": "Softland India"},
    {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Track"}]},
]


def _admin(client, db):
    make_user(db, username="tpl_admin", role_name="super_admin")
    t = login(client, "tpl_admin")
    return {"Authorization": f"Bearer {t['access_token']}"}


def _tpl(db, company_id, name, status="APPROVED", components=None):
    t = make_wa_template(db, company_id, name=name, status=status)
    t.components = components if components is not None else BODY
    db.commit(); db.refresh(t)
    return t


def _agent_user(db, company, username="ravi.k"):
    return make_user(db, username=username, role_name="agent", company_id=company.id)


def _configure(client, hdr, user_id, template_ids=None, agent_id="AG01"):
    body = {"agent_id": agent_id, "customer_id": CUSTOMER, "is_active": True}
    if template_ids is not None:
        body["template_ids"] = [str(t) for t in template_ids]
    return client.put(f"/api/agents/by-user/{user_id}", headers=hdr, json=body)


def _agent_token(client, username="ravi.k", device_id=DEVICE):
    r = client.post(LOGIN, json={"username": username, "password": "testpass123",
                                 "customer_id": CUSTOMER, "device_id": device_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Device-Id": device_id}


# ── Admin mapping ─────────────────────────────────────────────────────────────

class TestMapping:
    def test_maps_selected_templates(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="MAP1")
        user = _agent_user(db, comp)
        a, b = _tpl(db, comp.id, "order_update"), _tpl(db, comp.id, "invoice_ready")

        assert _configure(client, hdr, user.id, [a.id, b.id]).status_code == 200
        r = client.get(f"/api/agents/by-user/{user.id}/templates", headers=hdr)
        assert {t["name"] for t in r.json()} == {"order_update", "invoice_ready"}

    def test_saving_replaces_the_whole_set(self, client, db):
        """A multi-select means 'what is on screen is what should be stored'."""
        hdr = _admin(client, db)
        comp = make_company(db, code="MAP2")
        user = _agent_user(db, comp)
        a, b = _tpl(db, comp.id, "aaa"), _tpl(db, comp.id, "bbb")

        _configure(client, hdr, user.id, [a.id, b.id])
        _configure(client, hdr, user.id, [b.id])          # a deselected
        names = {t["name"] for t in
                 client.get(f"/api/agents/by-user/{user.id}/templates", headers=hdr).json()}
        assert names == {"bbb"}

    def test_omitting_template_ids_leaves_the_mapping_alone(self, client, db):
        """A caller that does not manage templates must not wipe them by accident."""
        hdr = _admin(client, db)
        comp = make_company(db, code="MAP3")
        user = _agent_user(db, comp)
        a = _tpl(db, comp.id, "keepme")

        _configure(client, hdr, user.id, [a.id])
        _configure(client, hdr, user.id, None)            # no template_ids key at all
        assert len(client.get(f"/api/agents/by-user/{user.id}/templates",
                              headers=hdr).json()) == 1

    def test_empty_list_clears_the_mapping(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="MAP4")
        user = _agent_user(db, comp)
        a = _tpl(db, comp.id, "dropme")
        _configure(client, hdr, user.id, [a.id])
        _configure(client, hdr, user.id, [])
        assert client.get(f"/api/agents/by-user/{user.id}/templates", headers=hdr).json() == []

    def test_another_companys_template_is_refused(self, client, db):
        hdr = _admin(client, db)
        mine   = make_company(db, code="MINE")
        theirs = make_company(db, code="THEIRS")
        user = _agent_user(db, mine)
        foreign = _tpl(db, theirs.id, "not_yours")

        r = _configure(client, hdr, user.id, [foreign.id])
        assert r.status_code == 400
        assert "another company" in r.json()["detail"]

    def test_mapping_survives_repeated_saves(self, client, db):
        """The unique constraint must not turn an unchanged save into a 500."""
        hdr = _admin(client, db)
        comp = make_company(db, code="MAP5")
        user = _agent_user(db, comp)
        a = _tpl(db, comp.id, "steady")
        for _ in range(3):
            assert _configure(client, hdr, user.id, [a.id]).status_code == 200
        assert db.query(AgentTemplate).count() == 1


class TestAvailableTemplates:
    def test_lists_only_this_users_company(self, client, db):
        hdr = _admin(client, db)
        mine, theirs = make_company(db, code="AV1"), make_company(db, code="AV2")
        user = _agent_user(db, mine)
        _tpl(db, mine.id, "ours")
        _tpl(db, theirs.id, "theirs")

        names = {t["name"] for t in client.get(
            f"/api/agents/by-user/{user.id}/available-templates", headers=hdr).json()}
        assert names == {"ours"}

    def test_excludes_unapproved(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="AV3")
        user = _agent_user(db, comp)
        _tpl(db, comp.id, "good")
        _tpl(db, comp.id, "pending", status="PENDING")

        names = {t["name"] for t in client.get(
            f"/api/agents/by-user/{user.id}/available-templates", headers=hdr).json()}
        assert names == {"good"}


# ── The agent-facing endpoint ─────────────────────────────────────────────────

class TestAgentEndpoint:
    def _ready(self, client, db, code="AT1", statuses=("APPROVED",)):
        hdr = _admin(client, db)
        comp = make_company(db, code=code)
        user = _agent_user(db, comp)
        tpls = [_tpl(db, comp.id, f"tpl_{i}", status=s) for i, s in enumerate(statuses)]
        _configure(client, hdr, user.id, [t.id for t in tpls])
        return hdr, comp, user, tpls

    def test_returns_mapped_templates(self, client, db):
        _hdr, _c, _u, _t = self._ready(client, db)
        r = client.get(AGENT_TEMPLATES, headers=_agent_token(client))
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_unmapped_agent_gets_an_empty_list(self, client, db):
        """Explicit mapping is the point — and a template send is billable."""
        hdr = _admin(client, db)
        comp = make_company(db, code="AT2")
        user = _agent_user(db, comp)
        _tpl(db, comp.id, "exists_but_unmapped")
        _configure(client, hdr, user.id)                  # no template_ids

        r = client.get(AGENT_TEMPLATES, headers=_agent_token(client))
        assert r.status_code == 200
        assert r.json() == []

    def test_unapproved_templates_are_hidden(self, client, db):
        """Paused at Meta → gone from the picker, so nobody fills in a doomed form."""
        _hdr, _c, _u, _t = self._ready(client, db, "AT3", ("APPROVED", "PAUSED"))
        names = {t["name"] for t in
                 client.get(AGENT_TEMPLATES, headers=_agent_token(client)).json()}
        assert names == {"tpl_0"}

    def test_admin_still_sees_the_unapproved_one_flagged(self, client, db):
        """Otherwise nobody can explain why an agent lost a template."""
        hdr, _c, user, _t = self._ready(client, db, "AT4", ("APPROVED", "PAUSED"))
        rows = client.get(f"/api/agents/by-user/{user.id}/templates", headers=hdr).json()
        assert {r["name"]: r["status"] for r in rows} == {
            "tpl_0": "APPROVED", "tpl_1": "PAUSED"}

    def test_full_structure_is_returned(self, client, db):
        """Everything the app needs to render a picker and send."""
        _hdr, _c, _u, _t = self._ready(client, db, "AT5")
        t = client.get(AGENT_TEMPLATES, headers=_agent_token(client)).json()[0]

        assert t["body_text"] == "Hello {{1}}, your order {{2}} ships on {{3}}."
        assert t["param_count"] == 3
        assert [p["index"] for p in t["parameters"]] == [1, 2, 3]
        assert t["parameters"][0]["example"] == "Ravi"
        assert t["header"] == {"format": "TEXT", "text": "Order update"}
        assert t["footer_text"] == "Softland India"
        assert t["buttons"] == [{"type": "QUICK_REPLY", "text": "Track"}]
        # name + language are the send address; Meta does not know our id.
        assert t["name"] and t["language"]

    def test_media_header_is_reported(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="AT6")
        user = _agent_user(db, comp)
        t = _tpl(db, comp.id, "with_image", components=[
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "Hi {{1}}"},
        ])
        _configure(client, hdr, user.id, [t.id])

        got = client.get(AGENT_TEMPLATES, headers=_agent_token(client)).json()[0]
        assert got["header"]["format"] == "IMAGE"
        assert got["header"]["text"] is None      # a media header carries no text

    def test_requires_a_session(self, client, db):
        assert client.get(AGENT_TEMPLATES).status_code == 401

    def test_requires_the_device_header(self, client, db):
        _hdr, _c, _u, _t = self._ready(client, db, "AT7")
        tok = _agent_token(client)["Authorization"]
        assert client.get(AGENT_TEMPLATES, headers={"Authorization": tok}).status_code == 401


# ── The reason the mapping stores an id and not a copy ────────────────────────

class TestLiveStructure:
    def test_template_edits_reach_the_app_immediately(self, client, db):
        """
        A snapshot taken at mapping time would still report the OLD parameter count
        here — and the only production symptom would be Meta rejecting the send.
        """
        hdr = _admin(client, db)
        comp = make_company(db, code="LIVE1")
        user = _agent_user(db, comp)
        t = _tpl(db, comp.id, "evolving")
        _configure(client, hdr, user.id, [t.id])

        before = client.get(AGENT_TEMPLATES, headers=_agent_token(client)).json()[0]
        assert before["param_count"] == 3

        # Meta re-approves the template with a fourth parameter; sync_templates
        # rewrites components wholesale, exactly like this.
        t.components = [{"type": "BODY", "text": "Hi {{1}} {{2}} {{3}} {{4}}"}]
        db.commit()

        after = client.get(AGENT_TEMPLATES, headers=_agent_token(client)).json()[0]
        assert after["param_count"] == 4
        assert after["body_text"] == "Hi {{1}} {{2}} {{3}} {{4}}"

    def test_deleting_a_template_removes_the_mapping(self, client, db):
        """CASCADE, so no dead id survives to be resolved later."""
        hdr = _admin(client, db)
        comp = make_company(db, code="LIVE2")
        user = _agent_user(db, comp)
        t = _tpl(db, comp.id, "doomed")
        _configure(client, hdr, user.id, [t.id])
        assert db.query(AgentTemplate).count() == 1

        db.delete(t)
        db.commit()
        assert db.query(AgentTemplate).count() == 0
        assert client.get(AGENT_TEMPLATES, headers=_agent_token(client)).json() == []


class TestAccessControl:
    def test_mapping_requires_authentication(self, client, db):
        comp = make_company(db, code="AC1")
        user = _agent_user(db, comp)
        assert client.get(f"/api/agents/by-user/{user.id}/templates").status_code == 403

    def test_company_scoped_admin_cannot_reach_another_company(self, client, db):
        theirs = make_company(db, code="ACT")
        mine   = make_company(db, code="ACM")
        target = _agent_user(db, theirs, username="their.agent")

        make_user(db, username="scoped_admin", role_name="admin", company_id=mine.id)
        tok = login(client, "scoped_admin")["access_token"]
        hdr = {"Authorization": f"Bearer {tok}"}

        assert client.get(f"/api/agents/by-user/{target.id}/templates",
                          headers=hdr).status_code == 403
