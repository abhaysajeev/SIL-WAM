"""
Phase 5 — waking the device: push tokens, the rev-driven notifier, live events.

  * a customer message pushes every participant's device at HIGH priority;
  * a status-only change pushes at normal priority, collapsed per thread;
  * a burst of changes in one thread is ONE push;
  * non-participants are never pushed; a revoked session is never pushed;
  * an UNREGISTERED token is cleared;
  * the payload carries no message text.
"""
import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.agent import AgentSession
from app.services import agent_timeline, live_events, push, sync_notifier
from tests.test_agent_participants import (
    CUST, _broadcast, _reply, _thread, no_immediate_dispatch, team,  # noqa: F401
)

TOKEN_URL = "/agent-api/v1/devices/push-token"


class FakeFcm:
    def __init__(self, outcome=push.OK):
        self.sent = []
        self.outcome = outcome

    def send(self, token, data, *, high, collapse_key):
        self.sent.append({"token": token, "data": data, "high": high, "collapse": collapse_key})
        return self.outcome


@pytest.fixture()
def fcm():
    fake = FakeFcm()
    push.set_client(fake)
    yield fake
    push.set_client(None)


def _register(client, h, token):
    r = client.put(TOKEN_URL, headers=h, json={"token": token, "platform": "android"})
    assert r.status_code == 200, r.text


def _drain(db):
    """Mark everything so far as notified, so a test sees only its own changes."""
    while sync_notifier.run_once(db):
        pass


def test_register_and_move_token(client, db, team):
    comp, tpl, pb, hdr, h = team
    _register(client, h["a"], "tok-" + "a" * 30)
    assert db.query(AgentSession).filter(AgentSession.push_token.isnot(None)).count() == 1
    # The same physical token registered again by another session moves, not copies.
    _register(client, h["b"], "tok-" + "a" * 30)
    db.expire_all()
    assert db.query(AgentSession).filter(AgentSession.push_token.isnot(None)).count() == 1


def test_inbound_pushes_participants_high_without_text(client, db, team, fcm):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _broadcast(client, db, h["b"], pb, tpl)
    _register(client, h["a"], "tok-a" + "x" * 30)
    _register(client, h["b"], "tok-b" + "x" * 30)
    _register(client, h["c"], "tok-c" + "x" * 30)
    _drain(db)
    fcm.sent.clear()

    _reply(db, comp, body="secret customer words")
    assert sync_notifier.run_once(db) >= 1
    tokens = {s["token"][:5] for s in fcm.sent}
    assert tokens == {"tok-a", "tok-b"}                 # C never contacted the customer
    assert all(s["high"] for s in fcm.sent)
    assert all("secret" not in str(s["data"]) for s in fcm.sent)
    tid = str(_thread(db, comp).id)
    assert all(s["data"]["thread_id"] == tid for s in fcm.sent)


def test_status_only_change_is_normal_priority_and_collapsed(client, db, team, fcm):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _register(client, h["a"], "tok-a" + "y" * 30)
    _drain(db)
    fcm.sent.clear()
    wamid = db.execute(text("""SELECT m.wamid FROM agent_messages m
                                JOIN agent_threads t ON t.id = m.thread_id
                               WHERE t.mobile_no = :m LIMIT 1"""), {"m": CUST}).scalar()
    agent_timeline.apply_status(db, wamid, "delivered")
    agent_timeline.apply_status(db, wamid, "read")
    db.commit()
    sync_notifier.run_once(db)
    mine = [s for s in fcm.sent if s["data"]["thread_id"] == str(_thread(db, comp).id)]
    assert len(mine) == 1                           # two changes, one push
    assert mine[0]["high"] is False and mine[0]["collapse"]


def test_nothing_new_means_no_push(client, db, team, fcm):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _register(client, h["a"], "tok-a" + "z" * 30)
    _drain(db)
    fcm.sent.clear()
    assert sync_notifier.run_once(db) == 0
    assert fcm.sent == []


def test_unregistered_token_is_cleared(client, db, team):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _register(client, h["a"], "tok-dead" + "q" * 30)
    push.set_client(FakeFcm(outcome=push.UNREGISTERED))
    try:
        _reply(db, comp)
        while sync_notifier.run_once(db):
            pass
        db.expire_all()
        assert db.query(AgentSession).filter(AgentSession.push_token.isnot(None)).count() == 0
    finally:
        push.set_client(None)


def test_revoked_session_is_not_pushed(client, db, team, fcm):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _register(client, h["a"], "tok-a" + "r" * 30)
    client.post("/agent-api/v1/auth/logout", headers=h["a"])
    _drain(db)
    fcm.sent.clear()
    _reply(db, comp)
    sync_notifier.run_once(db)
    assert fcm.sent == []


def test_push_off_still_publishes_live_events(client, db, team):
    comp, tpl, pb, hdr, h = team
    _broadcast(client, db, h["a"], pb, tpl)
    _drain(db)
    push.set_client(None)
    with patch.object(live_events, "publish") as pub:
        _reply(db, comp)
        sync_notifier.run_once(db)
    assert pub.called
    event = pub.call_args[0][1]
    assert event["type"] == "sync" and event["new_message"] is True


def test_live_events_fan_out():
    async def scenario():
        entry = live_events.subscribe("agent-1")
        other = live_events.subscribe("agent-2")
        await asyncio.sleep(0)
        assert live_events.publish("agent-1", {"rev": 7}) == 1
        got = await asyncio.wait_for(entry[1].get(), timeout=1)
        assert got == {"rev": 7}
        assert other[1].empty()
        live_events.unsubscribe("agent-1", entry)
        live_events.unsubscribe("agent-2", other)
        assert live_events.connected("agent-1") == 0
    asyncio.run(scenario())


def test_events_endpoint_requires_a_session(client):
    r = client.get("/agent-api/v1/events")
    assert r.status_code == 401
