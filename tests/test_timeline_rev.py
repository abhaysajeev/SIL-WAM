"""
Phase 1c — the `rev` change counter.

The app syncs with GET /threads/{id}/messages?changed_after_rev=N. That call must return
new messages AND existing messages whose status changed — a delivery tick is an UPDATE of
an existing row, allocates no new seq, and was invisible to the old after_seq sync.
"""
import threading

from app.models.agent_timeline import AgentMessage, AgentThread
from app.services import agent_timeline
from tests.conftest import make_company


def _msg(db, wamid):
    db.expire_all()
    return db.query(AgentMessage).filter_by(wamid=wamid).one()


def test_new_messages_get_increasing_rev(db):
    comp = make_company(db, code="REV1")
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast", wamid="w1")
    agent_timeline.record_inbound(db, comp.id, "918075571893", wamid="w2")
    db.commit()
    assert (_msg(db, "w1").rev, _msg(db, "w2").rev) == (1, 2)
    t = db.query(AgentThread).one()
    assert (t.message_seq, t.rev) == (2, 2)


def test_status_change_bumps_rev_but_not_seq(db):
    comp = make_company(db, code="REV2")
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast", wamid="w3")
    agent_timeline.record_inbound(db, comp.id, "918075571893", wamid="w4")
    db.commit()

    assert agent_timeline.apply_status(db, "w3", "delivered")
    db.commit()
    m = _msg(db, "w3")
    assert (m.seq, m.rev, m.status) == (1, 3, "delivered")
    t = db.query(AgentThread).one()
    assert (t.message_seq, t.rev) == (2, 3)


def test_stale_status_does_not_bump(db):
    comp = make_company(db, code="REV3")
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast", wamid="w5")
    agent_timeline.apply_status(db, "w5", "read")
    db.commit()
    rev_after_read = _msg(db, "w5").rev
    agent_timeline.apply_status(db, "w5", "delivered")     # late, out of order
    db.commit()
    assert _msg(db, "w5").rev == rev_after_read
    assert _msg(db, "w5").status == "read"


def test_repeated_failed_receipt_bumps_once(db):
    comp = make_company(db, code="REV4")
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast", wamid="w6")
    agent_timeline.apply_status(db, "w6", "failed")
    db.commit()
    r1 = _msg(db, "w6").rev
    agent_timeline.apply_status(db, "w6", "failed")
    db.commit()
    assert _msg(db, "w6").rev == r1


def test_concurrent_status_updates_never_share_a_rev(db):
    """Same guarantee as seq: UPDATE … RETURNING under the thread's row lock."""
    from tests.conftest import TestingSessionLocal

    comp = make_company(db, code="REV5")
    for i in range(8):
        agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast",
                                       wamid=f"wc{i}")
    db.commit()

    errors = []
    start = threading.Barrier(8, timeout=15)

    def deliver(i):
        s = TestingSessionLocal()
        try:
            start.wait()
            agent_timeline.apply_status(s, f"wc{i}", "delivered")
            s.commit()
        except Exception as exc:          # pragma: no cover — reported below
            errors.append(exc)
        finally:
            s.close()

    workers = [threading.Thread(target=deliver, args=(i,)) for i in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert not errors
    db.expire_all()
    revs = [m.rev for m in db.query(AgentMessage).all()]
    assert len(set(revs)) == 8
    assert sorted(revs) == list(range(9, 17))


# ── The endpoint ─────────────────────────────────────────────────────────────

def test_changed_after_rev_returns_status_changes(client, db):
    from tests.test_agent_send import THREADS, _agent_headers, _participate, _setup

    comp, *_ = _setup(client, db, code="REVAPI")
    h = _agent_headers(client)
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast",
                                   body_rendered="Offer", wamid="wapi1")
    agent_timeline.record_inbound(db, comp.id, "918075571893", body_rendered="Tell me more",
                                  wamid="wapi2")
    db.commit()
    tid = db.query(AgentThread).filter_by(company_id=comp.id).one().id
    _participate(db, comp.id, tid)
    db.commit()

    r = client.get(f"{THREADS}/{tid}/messages", headers=h, params={"changed_after_rev": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["rev"] for m in body["messages"]] == [1, 2]
    assert body["thread"]["rev"] == 2
    held = max(m["rev"] for m in body["messages"])

    agent_timeline.apply_status(db, "wapi1", "read")
    db.commit()

    r = client.get(f"{THREADS}/{tid}/messages", headers=h,
                   params={"changed_after_rev": held})
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["wamid"] == "wapi1" and msgs[0]["status"] == "read"
    assert msgs[0]["rev"] == 3


def test_changed_after_rev_pages_with_has_more(client, db):
    from tests.test_agent_send import THREADS, _agent_headers, _participate, _setup

    comp, *_ = _setup(client, db, code="REVPG")
    h = _agent_headers(client)
    for i in range(5):
        agent_timeline.record_inbound(db, comp.id, "918075571893", wamid=f"wpg{i}")
    db.commit()
    tid = db.query(AgentThread).filter_by(company_id=comp.id).one().id
    _participate(db, comp.id, tid)
    db.commit()

    r = client.get(f"{THREADS}/{tid}/messages", headers=h,
                   params={"changed_after_rev": 0, "limit": 3}).json()
    assert [m["rev"] for m in r["messages"]] == [1, 2, 3] and r["has_more"] is True
    r = client.get(f"{THREADS}/{tid}/messages", headers=h,
                   params={"changed_after_rev": 3, "limit": 3}).json()
    assert [m["rev"] for m in r["messages"]] == [4, 5] and r["has_more"] is False
