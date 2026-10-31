"""
The per-contact timeline the agent chat window reads.

The property under test throughout: **one thread holds a customer's whole history**,
whichever store originally produced each message. Before this existed, a broadcast lived
only in `broadcast_recipients` and the reply to it only in `messages`, and nothing could
put the two in one list.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.broadcast_campaign import BroadcastRecipient
from app.models.conversation import Message
from app.models.phonebook import Phonebook, PhonebookContact
from app.services import agent_timeline, broadcast_scheduler, conversation_engine
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_user, make_wa_account, make_wa_template, login,
)

_OK = SendResult(ok=True, meta_message_id="wamid.tl.1", error=None)


def _admin(client, db, username="tl_admin"):
    make_user(db, username=username, role_name="super_admin")
    tokens = login(client, username)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _setup(db, code="TLINE", contacts=2):
    comp = make_company(db, code=code)
    make_api_key(db, comp.id, key=f"key-{code}")
    account = make_wa_account(db, comp.id)
    tpl = make_wa_template(db, comp.id, name=f"tl_{code.lower()}")
    tpl.components = [{"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]
    db.commit()
    pb = Phonebook(company_id=comp.id, name=f"List {code}")
    db.add(pb); db.flush()
    for i in range(contacts):
        db.add(PhonebookContact(
            phonebook_id=pb.id, mobile_no=f"9177770{i:05d}",
            customer_name=f"Cust {i}", agent_id=f"AG{i}"))
    db.commit()
    return comp, account, tpl, pb


def _send_campaign(client, hdr, db, code, contacts=2, result=_OK):
    comp, account, tpl, pb = _setup(db, code=code, contacts=contacts)
    cid = client.post("/api/campaigns/", headers=hdr, json={
        "name": f"C {code}", "company_id": str(comp.id),
        "template_id": str(tpl.id)}).json()["id"]
    client.post(f"/api/campaigns/{cid}/recipients", headers=hdr,
                json={"phonebook_ids": [str(pb.id)], "params": ["Ravi", "Diwali"]})
    client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
    client.post(f"/api/campaigns/{cid}/send", headers=hdr)
    with patch("app.services.wa_sender.send_template", return_value=result):
        broadcast_scheduler.dispatch_pending(db)
    return comp, account, tpl, pb, cid


def _inbound(mobile, text_body="Yes please", wamid="wamid.in.1", context=None):
    msg = {"id": wamid, "from": mobile, "type": "text", "text": {"body": text_body}}
    if context:
        msg["context"] = {"id": context}
    return msg


# ── Thread identity ───────────────────────────────────────────────────────────

class TestThreads:
    def test_one_thread_per_customer_per_company(self, db):
        comp = make_company(db, code="TH1")
        a = agent_timeline.get_or_create_thread(db, comp.id, "917025985366")
        b = agent_timeline.get_or_create_thread(db, comp.id, "917025985366")
        db.commit()
        assert a.id == b.id
        assert db.query(AgentThread).filter(
            AgentThread.company_id == comp.id).count() == 1

    def test_the_same_number_is_a_separate_thread_per_company(self, db):
        """
        Two businesses messaging one person are two conversations. Sharing a thread
        would leak one company's history into another's chat window.
        """
        c1 = make_company(db, code="TH2A")
        c2 = make_company(db, code="TH2B")
        t1 = agent_timeline.get_or_create_thread(db, c1.id, "917025985366")
        t2 = agent_timeline.get_or_create_thread(db, c2.id, "917025985366")
        db.commit()
        assert t1.id != t2.id

    def test_a_known_name_is_never_overwritten_with_nothing(self, db):
        comp = make_company(db, code="TH3")
        agent_timeline.get_or_create_thread(db, comp.id, "917025985366", "Abhay")
        t = agent_timeline.get_or_create_thread(db, comp.id, "917025985366", None)
        db.commit()
        assert t.contact_name == "Abhay"

    def test_a_missing_name_is_filled_in_later(self, db):
        comp = make_company(db, code="TH4")
        agent_timeline.get_or_create_thread(db, comp.id, "917025985366", None)
        t = agent_timeline.get_or_create_thread(db, comp.id, "917025985366", "Abhay")
        db.commit()
        assert t.contact_name == "Abhay"


# ── Sequence allocation ───────────────────────────────────────────────────────

class TestSequence:
    def test_seq_starts_at_one_and_increments(self, db):
        comp = make_company(db, code="SEQ1")
        for i in range(4):
            agent_timeline.record_outbound(
                db, comp.id, "917025985366", kind="broadcast",
                body_rendered=f"msg {i}", wamid=f"wamid.seq.{i}")
        db.commit()
        seqs = [m.seq for m in db.query(AgentMessage).order_by(AgentMessage.seq).all()]
        assert seqs == [1, 2, 3, 4]

    def test_the_sequence_is_gap_free_across_directions(self, db):
        """
        A gap is indistinguishable from a message the client failed to fetch, which is
        what makes "everything after seq N" a safe sync and a timestamp not.
        """
        comp = make_company(db, code="SEQ2")
        agent_timeline.record_outbound(db, comp.id, "91702", kind="broadcast",
                                       wamid="w1")
        agent_timeline.record_inbound(db, comp.id, "91702", body_rendered="hi")
        agent_timeline.record_outbound(db, comp.id, "91702", kind="agent_reply",
                                       wamid="w2")
        db.commit()
        rows = db.query(AgentMessage).order_by(AgentMessage.seq).all()
        assert [r.seq for r in rows] == [1, 2, 3]
        assert [r.direction for r in rows] == ["outbound", "inbound", "outbound"]

    def test_sequences_are_independent_per_thread(self, db):
        comp = make_company(db, code="SEQ3")
        agent_timeline.record_outbound(db, comp.id, "91701", kind="broadcast", wamid="a")
        agent_timeline.record_outbound(db, comp.id, "91702", kind="broadcast", wamid="b")
        db.commit()
        assert {m.seq for m in db.query(AgentMessage).all()} == {1}

    def test_the_allocator_never_repeats_a_value(self, db):
        """
        Sequential, so this proves monotonicity rather than concurrency — the real
        concurrent case is the test below.
        """
        comp = make_company(db, code="SEQ4")
        t = agent_timeline.get_or_create_thread(db, comp.id, "917025985366")
        db.commit()
        seen = {agent_timeline._next_seq(db, t.id) for _ in range(50)}
        db.commit()
        assert seen == set(range(1, 51))

    def test_parallel_sessions_cannot_take_the_same_seq(self, db):
        """
        The claim that matters, tested the only way it can be: real threads on real
        sessions, contending for one thread row.

        UPDATE ... RETURNING takes the row lock, so the increments serialise and every
        caller gets a distinct value. Read-then-write would let two sessions read the
        same counter and return the same seq — and a duplicate seq means the app's
        "everything after N" sync silently skips a message.
        """
        import threading
        # The TEST database's session factory, not the application's. Using
        # app.core.database.SessionLocal here opens connections to the dev database,
        # where the thread row does not exist — the UPDATE then matches nothing and
        # RETURNING yields None, which looks like a locking failure and is not one.
        from tests.conftest import TestingSessionLocal

        comp = make_company(db, code="SEQ5")
        t = agent_timeline.get_or_create_thread(db, comp.id, "917025985366")
        db.commit()
        tid = t.id

        got, errors = [], []
        lock = threading.Lock()
        start = threading.Barrier(8, timeout=15)

        def allocate():
            s = TestingSessionLocal()
            try:
                start.wait()                 # maximise the overlap
                v = agent_timeline._next_seq(s, tid)
                s.commit()
                with lock:
                    got.append(v)
            except Exception as exc:         # noqa: BLE001 — reported, not swallowed
                with lock:
                    errors.append(exc)
            finally:
                s.close()

        threads = [threading.Thread(target=allocate) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)

        assert not errors, errors
        # A None here means the UPDATE matched no row — a different failure entirely
        # from a duplicate, and worth separating so the diagnosis is not guesswork.
        assert None not in got, "the thread row was not visible to the worker session"
        # A fresh thread starts at 0, so eight allocations must yield exactly 1..8.
        assert sorted(got) == list(range(1, 9))
        assert len(set(got)) == 8                   # the point: no duplicates


# ── Broadcast writes into the timeline ────────────────────────────────────────

class TestBroadcastWritesTimeline:
    def test_a_broadcast_send_appears_in_the_customers_thread(self, client, db):
        hdr = _admin(client, db, "tl_bc")
        comp, _a, _t, _pb, cid = _send_campaign(client, hdr, db, "BCTL", contacts=2)

        msgs = db.query(AgentMessage).join(AgentThread).filter(
            AgentThread.company_id == comp.id).all()
        assert len(msgs) == 2
        assert all(m.direction == "outbound" and m.kind == "broadcast" for m in msgs)
        assert all(str(m.campaign_id) == cid for m in msgs)

    def test_the_bubble_holds_the_rendered_text_not_the_placeholders(self, client, db):
        """
        The whole point of rendering server-side: what is stored is what the customer
        was shown, not "Hi {{1}}, offer on {{2}}."
        """
        hdr = _admin(client, db, "tl_render")
        comp, *_ = _send_campaign(client, hdr, db, "RENDTL", contacts=1)
        m = db.query(AgentMessage).join(AgentThread).filter(
            AgentThread.company_id == comp.id).first()
        assert m.body_rendered == "Hi Ravi, offer on Diwali."
        assert "{{" not in m.body_rendered

    def test_the_structured_template_is_kept_beside_the_prose(self, client, db):
        hdr = _admin(client, db, "tl_meta")
        comp, _a, tpl, *_ = _send_campaign(client, hdr, db, "METATL", contacts=1)
        m = db.query(AgentMessage).join(AgentThread).filter(
            AgentThread.company_id == comp.id).first()
        assert m.template_meta["name"] == tpl.name
        assert m.template_meta["params"] == ["Ravi", "Diwali"]

    def test_a_failed_send_writes_no_bubble(self, client, db):
        """A message that never reached Meta was never sent to anyone."""
        hdr = _admin(client, db, "tl_fail")
        fail = SendResult(ok=False, meta_message_id=None,
                          error="(#131026) not a WhatsApp user")
        comp, *_ = _send_campaign(client, hdr, db, "FAILTL", contacts=2, result=fail)
        assert db.query(AgentMessage).join(AgentThread).filter(
            AgentThread.company_id == comp.id).count() == 0

    def test_sending_does_not_open_the_24_hour_window(self, client, db):
        """
        The rule the whole window design rests on. A template going out is not a
        conversation the customer started, and treating it as one produces free-text
        sends Meta rejects with 131047.
        """
        hdr = _admin(client, db, "tl_window")
        comp, *_ = _send_campaign(client, hdr, db, "WINTL", contacts=1)
        t = db.query(AgentThread).filter(AgentThread.company_id == comp.id).first()
        assert t.last_inbound_at is None
        assert t.last_message_at is not None
        assert agent_timeline.window_state(t)["state"] == "closed"


# ── Inbound writes into the timeline ──────────────────────────────────────────

class TestInboundWritesTimeline:
    def test_a_reply_lands_in_the_same_thread_as_the_broadcast(self, client, db):
        """
        The reason this feature exists. Before it, the send was in
        broadcast_recipients and the reply in messages, and nothing joined them.
        """
        hdr = _admin(client, db, "tl_reply")
        comp, account, _t, pb, cid = _send_campaign(client, hdr, db, "REPLY", contacts=1)
        mobile = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first().mobile_no

        conversation_engine.handle_inbound(db, account, _inbound(mobile, "Yes please"))
        db.commit()

        threads = db.query(AgentThread).filter(AgentThread.company_id == comp.id).all()
        assert len(threads) == 1
        rows = db.query(AgentMessage).filter(
            AgentMessage.thread_id == threads[0].id).order_by(AgentMessage.seq).all()
        assert [r.direction for r in rows] == ["outbound", "inbound"]
        assert rows[1].body_rendered == "Yes please"

    def test_an_inbound_opens_the_window(self, client, db):
        hdr = _admin(client, db, "tl_open")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "OPEN", contacts=1)
        mobile = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first().mobile_no

        conversation_engine.handle_inbound(db, account, _inbound(mobile))
        db.commit()

        t = db.query(AgentThread).filter(AgentThread.company_id == comp.id).first()
        assert t.last_inbound_at is not None
        assert agent_timeline.window_state(t)["state"] == "open"

    def test_a_reply_carrying_context_records_what_it_answers(self, client, db):
        """
        Meta's context.id is the wamid being replied to, so a reply can be attached to
        the exact broadcast that prompted it. Most customers just type, which is why
        this must never be the only way a reply reaches a thread.
        """
        hdr = _admin(client, db, "tl_ctx")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "CTX", contacts=1)
        sent = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()

        conversation_engine.handle_inbound(
            db, account, _inbound(sent.mobile_no, "Yes", context=sent.wamid))
        db.commit()

        reply = db.query(AgentMessage).filter(
            AgentMessage.direction == "inbound").first()
        assert reply.replies_to_wamid == sent.wamid

    def test_a_reply_without_context_still_reaches_the_thread(self, client, db):
        hdr = _admin(client, db, "tl_noctx")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "NOCTX", contacts=1)
        mobile = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first().mobile_no

        conversation_engine.handle_inbound(db, account, _inbound(mobile, "hello"))
        db.commit()

        reply = db.query(AgentMessage).filter(
            AgentMessage.direction == "inbound").first()
        assert reply is not None and reply.replies_to_wamid is None

    def test_the_message_row_is_still_written_as_before(self, client, db):
        """The timeline is additive. Nothing about the transactional path changes."""
        hdr = _admin(client, db, "tl_msgrow")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "MSGROW", contacts=1)
        mobile = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first().mobile_no

        conversation_engine.handle_inbound(
            db, account, _inbound(mobile, "hi", wamid="wamid.both.1"))
        db.commit()

        assert db.query(Message).filter(Message.wamid == "wamid.both.1").first() is not None
        assert db.query(AgentMessage).filter(
            AgentMessage.wamid == "wamid.both.1").first() is not None

    def test_a_duplicate_inbound_is_not_recorded_twice(self, client, db):
        hdr = _admin(client, db, "tl_dup")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "DUPIN", contacts=1)
        mobile = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first().mobile_no

        for _ in range(2):
            conversation_engine.handle_inbound(
                db, account, _inbound(mobile, "hi", wamid="wamid.dup.1"))
            db.commit()

        assert db.query(AgentMessage).filter(
            AgentMessage.direction == "inbound").count() == 1


# ── Receipts ──────────────────────────────────────────────────────────────────

class TestReceipts:
    def test_a_delivery_receipt_updates_both_stores(self, client, db):
        """
        The accepted cost of the design: wamid lives in two tables. The campaign's
        counters and the customer's chat bubble must not disagree.
        """
        hdr = _admin(client, db, "tl_recv")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "RECV", contacts=1)
        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()

        conversation_engine.handle_status(
            db, {"id": r.wamid, "status": "delivered"}, account)
        db.commit()

        db.refresh(r)
        m = db.query(AgentMessage).filter(AgentMessage.wamid == r.wamid).first()
        db.refresh(m)
        assert r.status == "delivered"
        assert m.status == "delivered" and m.delivered_at is not None

    def test_a_receipt_never_walks_a_bubble_backwards(self, client, db):
        hdr = _admin(client, db, "tl_rank")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "RANK", contacts=1)
        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()

        for state in ("read", "delivered"):
            conversation_engine.handle_status(
                db, {"id": r.wamid, "status": state}, account)
            db.commit()

        m = db.query(AgentMessage).filter(AgentMessage.wamid == r.wamid).first()
        db.refresh(m)
        assert m.status == "read"

    def test_a_failure_receipt_applies_even_after_read(self, client, db):
        """
        Meta contradicting itself is news. The ranking guard stops progress running
        backwards; it must not suppress a failure.
        """
        hdr = _admin(client, db, "tl_failrecv")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "FAILRV", contacts=1)
        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()

        conversation_engine.handle_status(db, {"id": r.wamid, "status": "read"}, account)
        db.commit()
        conversation_engine.handle_status(
            db, {"id": r.wamid, "status": "failed",
                 "errors": [{"code": 131026, "title": "not a WhatsApp user"}]}, account)
        db.commit()

        m = db.query(AgentMessage).filter(AgentMessage.wamid == r.wamid).first()
        db.refresh(m)
        assert m.status == "failed"

    def test_an_unknown_wamid_is_harmless(self, client, db):
        hdr = _admin(client, db, "tl_unknown")
        comp, account, *_ = _send_campaign(client, hdr, db, "UNK", contacts=1)
        conversation_engine.handle_status(
            db, {"id": "wamid.nobody.knows", "status": "delivered"}, account)
        db.commit()      # must not raise


# ── The 24-hour window ────────────────────────────────────────────────────────

class TestWindow:
    def _thread(self, db, code, last_inbound):
        comp = make_company(db, code=code)
        t = agent_timeline.get_or_create_thread(db, comp.id, "917025985366")
        t.last_inbound_at = last_inbound
        db.commit()
        return t

    def test_never_written_means_closed(self, db):
        t = self._thread(db, "WIN1", None)
        w = agent_timeline.window_state(t)
        assert w["state"] == "closed" and w["window_expires_at"] is None
        assert not agent_timeline.is_open(t)

    def test_a_recent_inbound_is_open(self, db):
        t = self._thread(db, "WIN2", datetime.now(timezone.utc) - timedelta(hours=1))
        assert agent_timeline.window_state(t)["state"] == "open"

    def test_the_last_hour_is_flagged_closing_soon(self, db):
        """So the app can warn before the composer disappears mid-sentence."""
        t = self._thread(db, "WIN3",
                         datetime.now(timezone.utc) - timedelta(hours=23, minutes=30))
        assert agent_timeline.window_state(t)["state"] == "closing_soon"

    def test_past_24_hours_is_closed(self, db):
        t = self._thread(db, "WIN4", datetime.now(timezone.utc) - timedelta(hours=25))
        assert agent_timeline.window_state(t)["state"] == "closed"

    def test_the_expiry_is_exactly_24_hours_after_the_last_inbound(self, db):
        opened = datetime.now(timezone.utc) - timedelta(hours=2)
        t = self._thread(db, "WIN5", opened)
        w = agent_timeline.window_state(t)
        assert abs((w["window_expires_at"] - opened).total_seconds() - 24 * 3600) < 1


# ── Failure isolation ─────────────────────────────────────────────────────────

class TestTimelineNeverBreaksTheCaller:
    def test_a_timeline_failure_does_not_lose_the_inbound_message(self, client, db):
        """
        The timeline is a derived view. Losing a row from it is recoverable; losing a
        customer's actual message is not.
        """
        hdr = _admin(client, db, "tl_isolate")
        comp, account, _t, _pb, cid = _send_campaign(client, hdr, db, "ISOL", contacts=1)
        mobile = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first().mobile_no

        with patch("app.services.agent_timeline.get_or_create_thread",
                   side_effect=RuntimeError("timeline is down")):
            conversation_engine.handle_inbound(
                db, account, _inbound(mobile, "important", wamid="wamid.isol.1"))
            db.commit()

        assert db.query(Message).filter(
            Message.wamid == "wamid.isol.1").first() is not None
