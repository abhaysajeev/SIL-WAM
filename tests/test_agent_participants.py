"""
Phase 3 — a customer's replies reach the agents who contacted them, and nobody else.

  * an agent's broadcast makes them a participant of every recipient's chat;
  * the reply reaches every participant (teammates who both messaged the customer);
  * an agent who never contacted the customer cannot list, open, look up, write to,
    or download from that chat — every path answers 404;
  * the participation lapses 30 days after the agent's last contact;
  * a deactivated agent stops seeing chats; contacting the customer again restores it;
  * a web-panel (admin) broadcast makes nobody a participant.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.agent import Agent
from app.models.agent_timeline import AgentThread, AgentThreadParticipant
from app.services import agent_timeline, broadcast_scheduler
from app.services.wa_sender import SendResult
from tests.conftest import make_user
from tests.test_agent_send import (
    BROADCAST, CUSTOMER, MESSAGES, THREADS, _agent_headers, _setup,
)

CUST = "916666000000"       # the first contact _setup puts in the phonebook

_SENT = SendResult(ok=True, meta_message_id="wamid.bc", error=None)


@pytest.fixture(autouse=True)
def no_immediate_dispatch():
    """POST /broadcasts kicks a dispatch on the app's own (dev) database; the tests
    dispatch explicitly on the test database instead."""
    with patch("app.api.campaigns_api._kick_dispatch"):
        yield


def _add_agent(client, db, hdr, comp, pb, tpl, username, agent_code):
    user = make_user(db, username=username, role_name="agent", company_id=comp.id)
    r = client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json={
        "agent_id": agent_code, "customer_id": CUSTOMER, "is_active": True,
        "phonebook_ids": [str(pb.id)], "template_ids": [str(tpl.id)]})
    assert r.status_code == 200, r.text
    return user


def _broadcast(client, db, h, pb, tpl):
    r = client.post(BROADCAST, headers=h, json={
        "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id), "params": ["a", "b"]})
    assert r.status_code == 201, r.text
    counter = iter(range(10_000))
    with patch("app.services.wa_sender.send_template",
               side_effect=lambda *a, **k: SendResult(
                   ok=True, meta_message_id=f"wamid.bc.{next(counter)}", error=None)):
        broadcast_scheduler.dispatch_pending(db)
    return r.json()["campaign_id"]


def _reply(db, comp, mobile=CUST, body="Interested!", wamid="wamid.reply.1"):
    agent_timeline.record_inbound(db, comp.id, mobile, body_rendered=body, wamid=wamid)
    db.commit()


def _thread(db, comp, mobile=CUST):
    db.expire_all()
    return db.query(AgentThread).filter_by(company_id=comp.id, mobile_no=mobile).one()


def _listed(client, h):
    return [t["mobile_no"] for t in client.get(THREADS, headers=h).json()["threads"]]


@pytest.fixture()
def team(client, db):
    """One company, one phonebook, three agents: A and B broadcast, C does not."""
    comp, _acc, tpl, pb, user_a, hdr = _setup(client, db, "PART", contacts=2,
                                              username="agent.a")
    _add_agent(client, db, hdr, comp, pb, tpl, "agent.b", "AG02")
    _add_agent(client, db, hdr, comp, pb, tpl, "agent.c", "AG03")
    h = {
        "a": _agent_headers(client, "agent.a", device_id="dev-a"),
        "b": _agent_headers(client, "agent.b", device_id="dev-b"),
        "c": _agent_headers(client, "agent.c", device_id="dev-c"),
    }
    return comp, tpl, pb, hdr, h


def test_agent_broadcast_makes_the_agent_a_participant(client, db, team):
    comp, tpl, pb, _hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    t = _thread(db, comp)
    user_a = db.execute(text("SELECT id FROM users WHERE username='agent.a'")).scalar()
    agent_a = db.query(Agent).filter_by(user_id=user_a).one()
    parts = db.query(AgentThreadParticipant).filter_by(thread_id=t.id).all()
    assert len(parts) == 1 and parts[0].reason == "broadcast"
    assert parts[0].agent_id == agent_a.id


def test_reply_reaches_only_the_agents_who_messaged(client, db, team):
    comp, tpl, pb, _hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _broadcast(client, db, h["b"], pb, tpl)
    _reply(db, comp)

    assert CUST in _listed(client, h["a"])
    assert CUST in _listed(client, h["b"])
    assert CUST not in _listed(client, h["c"])

    tid = str(_thread(db, comp).id)
    assert agent_timeline.active_participants(db, tid).__len__() == 2


def test_a_non_participant_gets_404_everywhere(client, db, team):
    comp, tpl, pb, _hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _reply(db, comp)
    tid = str(_thread(db, comp).id)

    assert client.get(f"{THREADS}/{tid}/messages", headers=h["c"]).status_code == 404
    assert client.get(f"{THREADS}/by-mobile/{CUST}", headers=h["c"]).status_code == 404
    # The short spelling of the number does not get round it either.
    assert client.get(f"{THREADS}/by-mobile/6666000000", headers=h["c"]).status_code == 404
    r = client.post(MESSAGES, headers=h["c"], json={"mobile_no": CUST, "text": "hi"})
    assert r.status_code == 404

    # …while the participant sees all of it.
    assert client.get(f"{THREADS}/{tid}/messages", headers=h["a"]).status_code == 200
    assert client.get(f"{THREADS}/by-mobile/{CUST}", headers=h["a"]).status_code == 200


def test_a_reply_by_one_agent_shows_in_the_teammates_chat(client, db, team):
    comp, tpl, pb, _hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _broadcast(client, db, h["b"], pb, tpl)
    _reply(db, comp)
    with patch("app.services.wa_sender.send_text", return_value=_SENT):
        r = client.post(MESSAGES, headers=h["a"], json={"mobile_no": CUST, "text": "Sure!"})
    assert r.status_code == 201, r.text
    tid = str(_thread(db, comp).id)
    msgs = client.get(f"{THREADS}/{tid}/messages", headers=h["b"]).json()["messages"]
    assert any(m["body"] == "Sure!" for m in msgs)


def test_participation_lapses_after_30_days(client, db, team):
    comp, tpl, pb, _hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _reply(db, comp)
    db.execute(text("UPDATE agent_thread_participants SET last_contact_at = now() - interval '31 days'"))
    db.commit()
    assert CUST not in _listed(client, h["a"])

    # Contacting the customer again renews it.
    _broadcast(client, db, h["a"], pb, tpl)
    assert CUST in _listed(client, h["a"])


def test_deactivated_agent_stops_seeing_chats(client, db, team):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _broadcast(client, db, h["b"], pb, tpl)
    _reply(db, comp)
    user_b = db.execute(text("SELECT id FROM users WHERE username='agent.b'")).scalar()
    r = client.put(f"/api/agents/by-user/{user_b}", headers=hdr, json={
        "agent_id": "AG02", "customer_id": CUSTOMER, "is_active": False})
    assert r.status_code == 200, r.text

    tid = _thread(db, comp).id
    active = agent_timeline.active_participants(db, tid)
    assert len(active) == 1                       # A keeps it; B is out
    assert CUST in _listed(client, h["a"])


def test_web_panel_broadcast_makes_no_participants(client, db):
    from tests.test_agent_timeline import _send_campaign
    from tests.test_agent_send import _admin
    hdr = _admin(client, db, "web_admin_part")
    comp, *_ = _send_campaign(client, hdr, db, "WEBPART")
    assert db.query(AgentThreadParticipant).count() == 0
    assert db.query(AgentThread).filter_by(company_id=comp.id).count() > 0


def test_media_in_a_chat_follows_the_same_rule(client, db, team, tmp_path):
    from app.services import media_store
    from app.models.media_asset import MediaAsset
    media_store.set_store(media_store.local_store(str(tmp_path)))
    try:
        comp, tpl, pb, _hdr, h = team
        _broadcast(client, db, h["a"], pb, tpl)
        store = media_store.get_store()
        store.put("k/x.jpg", b"\xff\xd8\xff" + b"0" * 10, "image/jpeg")
        asset = MediaAsset(company_id=comp.id, format="IMAGE", mime="image/jpeg",
                           size_bytes=13, sha256="0" * 64, storage_key="k/x.jpg",
                           expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        db.add(asset)
        db.commit()
        agent_timeline.record_inbound(db, comp.id, CUST, wamid="wamid.photo",
                                      media={"format": "image", "asset_id": str(asset.id)})
        db.commit()
        assert client.get(f"/agent-api/v1/media/{asset.id}", headers=h["a"]).status_code == 200
        assert client.get(f"/agent-api/v1/media/{asset.id}", headers=h["c"]).status_code == 404
    finally:
        media_store.set_store(None)
