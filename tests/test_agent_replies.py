"""
Phase 4 — the agent's reply endpoint and inbound content.

POST /agent-api/v1/threads/{id}/messages:
  * text inside the 24-hour window; a template at any time; an uploaded image/PDF;
  * the same client_msg_id never sends twice; reused for another message → 409;
  * a teammate's newer reply holds the send back (409 teammate_replied) unless force;
  * opted out / template paused / bad template values are refused before anything is sent;
  * a Meta failure is a failed bubble with a category, not a lost message;
  * crash recovery: never-attempted rows are sent, in-flight ones become unconfirmed.
Inbound: reactions update the target message; unknown types get a placeholder; customer
attachments are copied to our store.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.messaging import MessagingOptOut
from app.services import agent_send, agent_timeline, conversation_engine, media_assets, media_store
from app.services.wa_sender import SendResult
from tests.test_agent_participants import (
    CUST, _broadcast, _reply, _thread, no_immediate_dispatch, team,  # noqa: F401 (fixtures)
)
from tests.test_agent_send import THREADS

OK = SendResult(ok=True, meta_message_id="wamid.out.1", error=None)


def _url(tid):
    return f"{THREADS}/{tid}/messages"


def _ready(client, db, team, *, reply=True):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    if reply:
        _reply(db, comp)
    return comp, tpl, pb, h, str(_thread(db, comp).id)


def _send(client, h, tid, **body):
    body.setdefault("client_msg_id", str(uuid.uuid4()))
    return client.post(_url(tid), headers=h, json=body)


# ── Text ─────────────────────────────────────────────────────────────────────

def test_text_reply_inside_window(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    with patch("app.services.wa_sender.send_text", return_value=OK) as snd:
        r = _send(client, h["a"], tid, text="Yes, available")
    assert r.status_code == 202, r.text
    m = r.json()["message"]
    assert m["status"] == "sent" and m["wamid"] == "wamid.out.1"
    assert m["sent_by_agent_id"] is not None
    snd.assert_called_once()
    assert snd.call_args[0][1] == CUST


def test_text_outside_window_refused_template_allowed(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team, reply=False)
    r = _send(client, h["a"], tid, text="hello")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "window_closed"
    with patch("app.services.wa_sender.send_template", return_value=OK) as snd:
        r = _send(client, h["a"], tid, template={"template_id": str(tpl.id),
                                                 "params": ["Ravi", "Diwali"]})
    assert r.status_code == 202, r.text
    assert r.json()["message"]["kind"] == "template"
    snd.assert_called_once()
    # Sending a template never opens the window.
    assert _thread(db, comp).last_inbound_at is None


def test_non_participant_cannot_reply(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    assert _send(client, h["c"], tid, text="hi").status_code == 404


def test_exactly_one_kind_and_uuid_key(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    assert _send(client, h["a"], tid, text="a", media={"media_id": "x"}).status_code == 422
    assert client.post(_url(tid), headers=h["a"],
                       json={"client_msg_id": "not-a-uuid", "text": "a"}).status_code == 422


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_same_key_sends_once(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    key = str(uuid.uuid4())
    with patch("app.services.wa_sender.send_text", return_value=OK) as snd:
        r1 = _send(client, h["a"], tid, client_msg_id=key, text="Once")
        r2 = _send(client, h["a"], tid, client_msg_id=key, text="Once")
    assert r1.status_code == 202 and r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r1.json()["message"]["id"] == r2.json()["message"]["id"]
    assert snd.call_count == 1


def test_same_key_different_message_refused(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    key = str(uuid.uuid4())
    with patch("app.services.wa_sender.send_text", return_value=OK):
        _send(client, h["a"], tid, client_msg_id=key, text="First")
        r = _send(client, h["a"], tid, client_msg_id=key, text="Different")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_mismatch"


# ── Teammate collision ───────────────────────────────────────────────────────

def test_teammate_replied_holds_back_until_forced(client, db, team):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _broadcast(client, db, h["b"], pb, tpl)
    _reply(db, comp)
    tid = str(_thread(db, comp).id)
    seen = client.get(_url(tid), headers=h["a"]).json()["thread"]["rev"]

    with patch("app.services.wa_sender.send_text", return_value=OK):
        assert _send(client, h["b"], tid, text="B here").status_code == 202
        r = _send(client, h["a"], tid, text="A here", last_seen_rev=seen)
        assert r.status_code == 409
        d = r.json()["detail"]
        assert d["code"] == "teammate_replied"
        assert [m["body"] for m in d["messages"]] == ["B here"]
        r = _send(client, h["a"], tid, text="A here", last_seen_rev=seen, force=True)
        assert r.status_code == 202


def test_own_earlier_reply_is_not_a_collision(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    seen = client.get(_url(tid), headers=h["a"]).json()["thread"]["rev"]
    with patch("app.services.wa_sender.send_text", return_value=OK):
        _send(client, h["a"], tid, text="one")
        assert _send(client, h["a"], tid, text="two", last_seen_rev=seen).status_code == 202


# ── Refusals ─────────────────────────────────────────────────────────────────

def test_opted_out(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    db.add(MessagingOptOut(company_id=comp.id, mobile_no=CUST, source="stop_keyword"))
    db.commit()
    with patch("app.services.wa_sender.send_text") as snd:
        r = _send(client, h["a"], tid, text="hi")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "recipient_opted_out"
    snd.assert_not_called()


def test_paused_template_refused_at_send_time(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    tpl.status = "PAUSED"
    db.commit()
    r = _send(client, h["a"], tid, template={"template_id": str(tpl.id), "params": ["a", "b"]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "template_unavailable"


@pytest.mark.parametrize("bad", ["two\nlines", "tab\there", "five     spaces", "  "])
def test_template_values_meta_would_reject(client, db, team, bad):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    r = _send(client, h["a"], tid, template={"template_id": str(tpl.id), "params": [bad, "b"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_param"


def test_daily_limit_counts_replies_not_broadcasts(client, db, team):
    from tests.test_agent_send import _settings
    comp, tpl, pb, h, tid = _ready(client, db, team)
    _settings(db, comp.id, agent_daily_message_limit=1)
    with patch("app.services.wa_sender.send_text", return_value=OK):
        # The broadcast above did not use up the allowance…
        assert _send(client, h["a"], tid, text="one").status_code == 202
        # …but the reply did.
        r = _send(client, h["a"], tid, text="two")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "daily_limit_reached"


# ── Failure is a bubble, not an exception ────────────────────────────────────

def test_meta_failure_recorded_with_category(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    fail = SendResult(ok=False, meta_message_id=None, error="(#131026) Message undeliverable",
                      error_code="131026")
    with patch("app.services.wa_sender.send_text", return_value=fail):
        r = _send(client, h["a"], tid, text="hi")
    assert r.status_code == 202
    m = r.json()["message"]
    assert m["status"] == "failed" and m["failure_category"] == "blocked"


def test_failed_receipt_sets_category(db):
    from tests.conftest import make_company
    comp = make_company(db, code="FCAT")
    agent_timeline.record_outbound(db, comp.id, CUST, kind="agent_reply", wamid="wf1")
    db.commit()
    agent_timeline.apply_status(db, "wf1", "failed",
                                errors=[{"code": 131042, "title": "Business eligibility payment issue"}])
    db.commit()
    m = db.query(AgentMessage).filter_by(wamid="wf1").one()
    assert (m.failure_category, m.error_code) == ("billing", "131042")


# ── Media reply ──────────────────────────────────────────────────────────────

def test_media_reply(client, db, team, tmp_path):
    media_store.set_store(media_store.local_store(str(tmp_path)))
    try:
        comp, tpl, pb, h, tid = _ready(client, db, team)
        with patch("app.services.media_assets.meta_media.upload_media", return_value="meta-x"):
            mid = client.post("/agent-api/v1/media", headers=h["a"], files={
                "file": ("p.pdf", b"%PDF-1.4\n" + b"0" * 100, "application/pdf")}).json()["media_id"]
        with patch("app.services.wa_sender.send_media", return_value=OK) as snd:
            r = _send(client, h["a"], tid, media={"media_id": mid, "caption": "Price list"})
        assert r.status_code == 202, r.text
        assert snd.call_args[0][2:4] == ("document", "meta-x")
        assert r.json()["message"]["media"]["asset_id"] == mid
    finally:
        media_store.set_store(None)


# ── Crash recovery ───────────────────────────────────────────────────────────

def test_recovery_sends_never_attempted_and_parks_in_flight(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    agent_row = db.execute(text(
        "SELECT a.id FROM agents a JOIN users u ON u.id=a.user_id WHERE u.username='agent.a'")).scalar()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    q = agent_timeline.record_outbound(db, comp.id, CUST, thread_id=tid, kind="agent_reply",
                                       body_rendered="never sent", status="queued",
                                       sent_by_agent_id=agent_row, client_msg_id=str(uuid.uuid4()))
    s = agent_timeline.record_outbound(db, comp.id, CUST, thread_id=tid, kind="agent_reply",
                                       body_rendered="in flight", status="sending",
                                       sent_by_agent_id=agent_row, client_msg_id=str(uuid.uuid4()))
    db.execute(text("UPDATE agent_messages SET created_at=:o WHERE id=:i"), {"o": old, "i": str(q)})
    db.execute(text("UPDATE agent_messages SET attempted_at=:o WHERE id=:i"), {"o": old, "i": str(s)})
    db.commit()
    with patch("app.services.wa_sender.send_text", return_value=OK) as snd:
        out = agent_send.recover_stuck(db)
    assert out == {"unconfirmed": 1, "resent": 1}
    snd.assert_called_once()
    db.expire_all()
    assert db.get(AgentMessage, q).status == "sent"
    assert db.get(AgentMessage, s).status == "unconfirmed"


def test_deliver_twice_sends_once(client, db, team):
    comp, tpl, pb, h, tid = _ready(client, db, team)
    mid = agent_timeline.record_outbound(db, comp.id, CUST, thread_id=tid, kind="agent_reply",
                                         body_rendered="x", status="queued",
                                         client_msg_id=str(uuid.uuid4()))
    db.commit()
    with patch("app.services.wa_sender.send_text", return_value=OK) as snd:
        agent_send.deliver(db, mid)
        agent_send.deliver(db, mid)
    assert snd.call_count == 1


# ── Inbound content ──────────────────────────────────────────────────────────

def test_reaction_updates_the_message(db):
    from tests.conftest import make_company
    comp = make_company(db, code="REACT")
    agent_timeline.record_outbound(db, comp.id, CUST, kind="broadcast", wamid="wr1")
    db.commit()
    before = db.query(AgentMessage).filter_by(wamid="wr1").one().rev
    assert agent_timeline.apply_reaction(db, comp.id, "wr1", "👍")
    db.commit()
    m = db.query(AgentMessage).filter_by(wamid="wr1").one()
    assert m.reactions == {"👍": ["customer"]} and m.rev > before
    assert db.query(AgentMessage).count() == 1          # no new bubble
    agent_timeline.apply_reaction(db, comp.id, "wr1", "")   # un-react
    db.commit()
    db.expire_all()
    assert db.query(AgentMessage).filter_by(wamid="wr1").one().reactions is None


@pytest.mark.parametrize("msg,expected", [
    ({"type": "location", "location": {"name": "Lulu Mall"}}, "📍 Location: Lulu Mall"),
    ({"type": "unsupported", "unsupported": {"type": "poll_update"}},
     "Unsupported message (poll_update) — view it on WhatsApp"),
    ({"type": "order"}, "Unsupported message (order) — view it on WhatsApp"),
])
def test_placeholders(msg, expected):
    assert conversation_engine._inbound_text(msg) == expected


def test_inbound_media_is_copied_and_rev_bumped(db, tmp_path):
    from tests.conftest import make_company, make_wa_account
    media_store.set_store(media_store.local_store(str(tmp_path)))
    try:
        comp = make_company(db, code="INMED")
        make_wa_account(db, comp.id, phone_number_id="PN-INMED")
        agent_timeline.record_inbound(db, comp.id, CUST, wamid="wim",
                                      media={"type": "image", "media_id": "meta-in-1"})
        db.commit()
        rev0 = db.query(AgentMessage).filter_by(wamid="wim").one().rev
        with patch("app.services.media_assets.meta_media.fetch_media",
                   return_value=(b"\xff\xd8\xff" + b"1" * 50, "image/jpeg")):
            assert media_assets.fetch_inbound(db) == 1
        db.expire_all()
        m = db.query(AgentMessage).filter_by(wamid="wim").one()
        assert m.media["asset_id"] and m.rev > rev0
        assert media_assets.fetch_inbound(db) == 0        # done, not fetched again
    finally:
        media_store.set_store(None)


def test_inbound_media_failure_retries_then_gives_up(db):
    from tests.conftest import make_company, make_wa_account
    comp = make_company(db, code="INMED2")
    make_wa_account(db, comp.id, phone_number_id="PN-INMED2")
    agent_timeline.record_inbound(db, comp.id, CUST, wamid="wim2",
                                  media={"type": "image", "media_id": "meta-gone"})
    db.commit()
    with patch("app.services.media_assets.meta_media.fetch_media",
               side_effect=RuntimeError("expired")):
        for _ in range(media_assets.INBOUND_MAX_ATTEMPTS + 2):
            media_assets.fetch_inbound(db)
    db.expire_all()
    m = db.query(AgentMessage).filter_by(wamid="wim2").one()
    assert m.media["fetch_attempts"] == media_assets.INBOUND_MAX_ATTEMPTS
    assert "expired" in m.media["fetch_error"]
