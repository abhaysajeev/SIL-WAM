"""
Telling agents' devices that a chat changed — the outbox behind push and /events.

Every change to a thread already bumps its `rev`. This job finds threads whose rev moved
past `notified_rev`, tells each active participant ("sync thread X, rev N") by FCM push
and on any open /events connection, then records notified_rev = rev.

Why a sweep and not a call at every write site: there are many writers (webhook inbound,
receipts, broadcasts, agent replies, media downloads, reactions) and each would have to
remember to notify, after its commit, without failing its own work. Here none of them has
to. A burst of changes in one thread (a broadcast's sent→delivered→read) becomes ONE
push. A crash loses nothing: the rows are still rev > notified_rev on the next run.

Latency is the sweep interval (2s).
"""
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import agent_timeline, live_events, push

logger = logging.getLogger(__name__)

BATCH = 200


def run_once(db: Session) -> int:
    rows = db.execute(text("""
        SELECT t.id, t.rev, t.notified_rev
          FROM agent_threads t
         WHERE t.rev > t.notified_rev
         ORDER BY t.updated_at NULLS FIRST
         LIMIT :n
           FOR UPDATE SKIP LOCKED
    """), {"n": BATCH}).fetchall()
    if not rows:
        db.rollback()
        return 0

    work = []
    for r in rows:
        participants = agent_timeline.active_participants(db, r.id)
        # A new customer message among the unnotified changes → a user-visible push.
        has_inbound = db.execute(text("""
            SELECT 1 FROM agent_messages
             WHERE thread_id = :t AND direction = 'inbound'
               AND rev > :lo AND rev <= :hi LIMIT 1"""),
            {"t": str(r.id), "lo": r.notified_rev, "hi": r.rev}).first() is not None
        tokens = []
        if participants:
            tokens = db.execute(text("""
                SELECT s.id, s.agent_id, s.push_token FROM agent_sessions s
                 WHERE s.agent_id = ANY(CAST(:a AS uuid[]))
                   AND s.revoked = false AND s.push_token IS NOT NULL"""),
                {"a": participants}).fetchall()
        work.append((str(r.id), r.rev, has_inbound, participants, tokens))
        db.execute(text("UPDATE agent_threads SET notified_rev = :rev WHERE id = :t"),
                   {"rev": r.rev, "t": str(r.id)})
    # Commit BEFORE any network call: the row lock is not held across FCM.
    db.commit()

    fcm = push.client()
    dead_sessions = []
    for thread_id, rev, has_inbound, participants, tokens in work:
        event = {"type": "sync", "thread_id": thread_id, "rev": rev,
                 "new_message": has_inbound}
        for agent_id in participants:
            live_events.publish(agent_id, event)
        if fcm is None:
            continue
        for s in tokens:
            outcome = fcm.send(s.push_token,
                               {"type": "sync", "thread_id": thread_id, "rev": rev,
                                "new_message": "1" if has_inbound else "0"},
                               high=has_inbound, collapse_key=thread_id)
            if outcome == push.UNREGISTERED:
                dead_sessions.append(str(s.id))

    if dead_sessions:
        db.execute(text("""UPDATE agent_sessions SET push_token = NULL
                            WHERE id = ANY(CAST(:ids AS uuid[]))"""), {"ids": dead_sessions})
        db.commit()
    return len(work)
