"""
Writing the per-contact timeline the agent chat window reads.

Three callers, one shape:

| Caller | Writes |
|---|---|
| `broadcast_scheduler._apply_result`, on success | an outbound `broadcast` row |
| `conversation_engine.handle_inbound` | an inbound row, and stamps the 24-hour clock |
| `conversation_engine.handle_status` | updates a row's delivery state by wamid |

**Nothing here commits.** Every function joins the caller's transaction, so a timeline
row and the thing that produced it either both land or neither does. A broadcast send
that recorded a wamid but no chat bubble — or a bubble for a send that rolled back —
would be a chat window disagreeing with the campaign it came from.

**Nothing here raises into the caller either.** A failure to write the timeline must
never lose a customer's actual message or abort a send that already reached Meta; the
timeline is a derived view, and losing a row from it is recoverable in a way that losing
an inbound message is not. Failures are logged and swallowed at the boundary.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.agent_timeline import AgentMessage, AgentThread

logger = logging.getLogger(__name__)

# WhatsApp's service window. Free text is allowed only within this long of the
# customer's last inbound message; outside it, approved templates only.
SERVICE_WINDOW_HOURS = 24


# ── Threads ───────────────────────────────────────────────────────────────────

def get_or_create_thread(db: Session, company_id, mobile_no: str | None,
                         contact_name: str | None = None, *,
                         bsuid: str | None = None) -> AgentThread | None:
    """
    The thread for one customer of one company, created if absent.

    A customer is identified by `mobile_no` (canonical: E.164 digits without '+', as
    app/core/phone.py produces and as Meta reports `from`), by `bsuid` (Meta's
    business-scoped user id for username users), or both. BSUID is matched first: it is
    stable across a number the customer has hidden, and Meta may omit the number
    entirely. Whichever identifier the thread was missing is filled in when learned.

    **Inserted with a Core statement, not `db.add()` + `db.flush()`.** `Session.flush()`
    writes *every* pending object in the session, not only ours — in `handle_inbound`
    that includes the customer's `Message` row, which would then be inside our SAVEPOINT
    and be discarded along with it if a timeline write failed. A Core INSERT executes on
    its own and touches nothing the caller has staged.

    `ON CONFLICT DO NOTHING` handles the race directly, so there is no IntegrityError to
    recover from and no rollback of any kind.
    """
    mobile_no = mobile_no or None
    bsuid = bsuid or None
    if mobile_no is None and bsuid is None:
        return None

    thread = _find_thread(db, company_id, mobile_no, bsuid)
    if thread is not None:
        _fill_identity(db, thread, mobile_no, bsuid, contact_name)
        return thread

    if mobile_no is not None:
        conflict = "(company_id, mobile_no) WHERE mobile_no IS NOT NULL"
    else:
        conflict = "(company_id, bsuid) WHERE bsuid IS NOT NULL"
    db.execute(
        text(f"""
            INSERT INTO agent_threads (id, company_id, mobile_no, bsuid, contact_name)
            VALUES (gen_random_uuid(), :company_id, :mobile_no, :bsuid, :contact_name)
            ON CONFLICT {conflict} DO NOTHING
        """),
        {"company_id": str(company_id), "mobile_no": mobile_no, "bsuid": bsuid,
         "contact_name": contact_name},
    )
    # Re-read rather than use RETURNING: with DO NOTHING, a losing insert returns no
    # row at all, and this one query is correct whether we won the race or not.
    thread = _find_thread(db, company_id, mobile_no, bsuid)
    if thread is not None:
        _fill_identity(db, thread, mobile_no, bsuid, contact_name)
    return thread


def _find_thread(db: Session, company_id, mobile_no, bsuid) -> AgentThread | None:
    if bsuid:
        t = (db.query(AgentThread)
             .filter(AgentThread.company_id == company_id, AgentThread.bsuid == bsuid)
             .first())
        if t is not None:
            return t
    if mobile_no:
        return (db.query(AgentThread)
                .filter(AgentThread.company_id == company_id,
                        AgentThread.mobile_no == mobile_no)
                .first())
    return None


def _fill_identity(db: Session, thread, mobile_no, bsuid, contact_name) -> None:
    """Fill in what the thread did not know. Never overwrites a known value with
    nothing; never takes a number or BSUID another thread of this company holds (that
    would violate the unique index — two threads for one person is for the merge
    script to resolve, not something to paper over mid-webhook)."""
    sets, params = [], {"i": str(thread.id)}
    if contact_name and not thread.contact_name:
        sets.append("contact_name = :n")
        params["n"] = contact_name
        set_committed_value(thread, "contact_name", contact_name)
    if bsuid and not thread.bsuid and not _taken(db, thread, "bsuid", bsuid):
        sets.append("bsuid = :b")
        params["b"] = bsuid
        set_committed_value(thread, "bsuid", bsuid)
    if mobile_no and not thread.mobile_no and not _taken(db, thread, "mobile_no", mobile_no):
        sets.append("mobile_no = :m")
        params["m"] = mobile_no
        set_committed_value(thread, "mobile_no", mobile_no)
    if sets:
        db.execute(text(f"UPDATE agent_threads SET {', '.join(sets)} WHERE id = :i"), params)


def _taken(db: Session, thread, column: str, value: str) -> bool:
    return db.execute(
        text(f"""SELECT 1 FROM agent_threads
                  WHERE company_id = :c AND {column} = :v AND id <> :i LIMIT 1"""),
        {"c": str(thread.company_id), "v": value, "i": str(thread.id)},
    ).first() is not None


def _next_position(db: Session, thread_id) -> tuple[int, int]:
    """
    Allocate the next (seq, rev) in a thread.

    `UPDATE ... RETURNING` in one statement, so the row lock Postgres takes serialises
    concurrent writers and both counters are gap-free. Reading a counter and writing it
    back separately would let two writers read the same value and produce a duplicate —
    caught by the unique constraint for seq, but as a 500 rather than a correct ordering.

    Gap-free matters because it is what lets the app sync with "everything after N" and
    know it has missed nothing. A gap is indistinguishable from a message the client
    failed to fetch.
    """
    row = db.execute(
        text("""
            UPDATE agent_threads
            SET message_seq = message_seq + 1, rev = rev + 1
            WHERE id = :tid
            RETURNING message_seq, rev
        """),
        {"tid": str(thread_id)},
    ).first()
    return row.message_seq, row.rev


def _next_seq(db: Session, thread_id) -> int:
    """The next seq alone. Kept for callers that only need ordering; it advances rev
    too, because a new message is a change."""
    return _next_position(db, thread_id)[0]


def bump_rev(db: Session, thread_id) -> int:
    """
    Allocate the next change number for a thread, for an UPDATE to an existing message
    (status, reaction, media arrived). Same row-lock guarantee as _next_position.
    The caller stores the returned value in the changed message's `rev`.
    """
    return db.execute(
        text("UPDATE agent_threads SET rev = rev + 1 WHERE id = :tid RETURNING rev"),
        {"tid": str(thread_id)},
    ).scalar()


_INSERT_MESSAGE = text("""
    INSERT INTO agent_messages (
        id, thread_id, seq, rev, direction, kind, body_rendered, template_meta, media,
        wamid, status, campaign_id, sent_by_agent_id, replies_to_wamid, sent_at,
        client_msg_id, request_hash
    ) VALUES (
        gen_random_uuid(), :thread_id, :seq, :rev, :direction, :kind, :body_rendered,
        CAST(:template_meta AS jsonb), CAST(:media AS jsonb),
        :wamid, :status, :campaign_id, :sent_by_agent_id, :replies_to_wamid, :sent_at,
        :client_msg_id, :request_hash
    )
    RETURNING id
""")


def _write(db: Session, thread, *, direction: str, kind: str,
           body_rendered=None, template_meta=None, media=None, wamid=None,
           status="sent", campaign_id=None, sent_by_agent_id=None,
           replies_to_wamid=None, sent_at=None, client_msg_id=None, request_hash=None):
    """Insert one message row and return its id. Core, for the reason above."""
    seq, rev = _next_position(db, thread.id)
    # Mirror the database onto the loaded object without marking it dirty — a dirty
    # attribute would be written back by the next flush, a pointless second UPDATE.
    set_committed_value(thread, "message_seq", seq)
    set_committed_value(thread, "rev", rev)
    return db.execute(_INSERT_MESSAGE, {
        "thread_id":        str(thread.id),
        "seq":              seq,
        "rev":              rev,
        "direction":        direction,
        "kind":             kind,
        "body_rendered":    body_rendered,
        "template_meta":    json.dumps(template_meta) if template_meta else None,
        "media":            json.dumps(media) if media else None,
        "wamid":            wamid,
        "status":           status,
        "campaign_id":      str(campaign_id) if campaign_id else None,
        "sent_by_agent_id": str(sent_by_agent_id) if sent_by_agent_id else None,
        "replies_to_wamid": replies_to_wamid,
        "sent_at":          sent_at,
        "client_msg_id":    client_msg_id,
        "request_hash":     request_hash,
    }).scalar()


def _touch(db: Session, thread, *, now: datetime, inbound: bool) -> None:
    """
    Update the thread's activity stamps.

    `last_inbound_at` is set **only** when inbound is True. That column is the 24-hour
    window's clock, and sending never starts it — however much a template going out
    looks like the beginning of a conversation.
    """
    if inbound:
        db.execute(
            text("""UPDATE agent_threads
                    SET last_inbound_at = :now, last_message_at = :now
                    WHERE id = :i"""),
            {"now": now, "i": str(thread.id)},
        )
        thread.last_inbound_at = now
    else:
        db.execute(
            text("UPDATE agent_threads SET last_message_at = :now WHERE id = :i"),
            {"now": now, "i": str(thread.id)},
        )
    thread.last_message_at = now


# ── Participants: who sees a thread ───────────────────────────────────────────

# A participation counts for this long after the agent last contacted the customer.
PARTICIPANT_WINDOW_DAYS = 30

# The one visibility rule, as SQL, for every agent read and send. Expects `:agent_id`
# and an outer alias `t` for agent_threads. Used verbatim by threads.py, messages.py
# and media.py so the list, the chat, the by-number lookup, sending and file downloads
# can never disagree about who may see a customer.
VISIBLE_TO_AGENT_SQL = f"""
    EXISTS (SELECT 1 FROM agent_thread_participants p
             WHERE p.thread_id = t.id
               AND p.agent_id = CAST(:agent_id AS uuid)
               AND p.removed_at IS NULL
               AND p.last_contact_at >= now() - interval '{PARTICIPANT_WINDOW_DAYS} days')
"""


def add_participant(db: Session, thread_id, agent_id, reason: str,
                    now: datetime | None = None) -> None:
    """
    Make (or keep) an agent a participant of a thread, renewing the 30-day window.
    A removed participant contacting the customer again is restored. Joins the caller's
    transaction; Core statement, for the same reason as get_or_create_thread.
    """
    if not agent_id:
        return
    now = now or datetime.now(timezone.utc)
    db.execute(
        text("""
            INSERT INTO agent_thread_participants
                   (thread_id, agent_id, reason, first_contact_at, last_contact_at)
            VALUES (:t, :a, :r, :now, :now)
            ON CONFLICT (thread_id, agent_id) DO UPDATE
               SET last_contact_at = GREATEST(agent_thread_participants.last_contact_at,
                                              EXCLUDED.last_contact_at),
                   removed_at = NULL
        """),
        {"t": str(thread_id), "a": str(agent_id), "r": reason, "now": now},
    )


def active_participants(db: Session, thread_id) -> list[str]:
    """agents.id of everyone who currently sees this thread — the push recipients."""
    rows = db.execute(
        text(f"""SELECT p.agent_id FROM agent_thread_participants p
                  WHERE p.thread_id = :t AND p.removed_at IS NULL
                    AND p.last_contact_at >= now() - interval '{PARTICIPANT_WINDOW_DAYS} days'"""),
        {"t": str(thread_id)},
    ).fetchall()
    return [str(r.agent_id) for r in rows]


def is_visible(db: Session, thread_id, agent_id) -> bool:
    return db.execute(
        text(f"SELECT 1 FROM agent_threads t WHERE t.id = :t AND {VISIBLE_TO_AGENT_SQL}"),
        {"t": str(thread_id), "agent_id": str(agent_id)},
    ).first() is not None


def remove_agent_everywhere(db: Session, agent_id) -> int:
    """An agent deactivated or their device revoked: they stop seeing every thread.
    Their threads stay — teammates who also contacted the customer keep them, and the
    rest are in the web panel. Returns how many participations ended."""
    return db.execute(
        text("""UPDATE agent_thread_participants SET removed_at = now()
                 WHERE agent_id = :a AND removed_at IS NULL"""),
        {"a": str(agent_id)},
    ).rowcount or 0


# ── Writing messages ──────────────────────────────────────────────────────────

def record_outbound(db: Session, company_id, mobile_no: str, *,
                    kind: str,
                    body_rendered: str | None = None,
                    template_meta: dict | None = None,
                    media: dict | None = None,
                    wamid: str | None = None,
                    status: str = "sent",
                    campaign_id=None,
                    sent_by_agent_id=None,
                    contact_name: str | None = None,
                    sent_at: datetime | None = None,
                    client_msg_id: str | None = None,
                    request_hash: str | None = None,
                    thread_id=None):
    """
    Record a message we sent. Returns the new row's id, or None if the write failed.

    Deliberately does NOT touch `last_inbound_at`: sending never opens the 24-hour
    window. Only the customer writing to us does, and conflating the two produces an app
    that offers free-text replies Meta rejects with 131047.
    """
    try:
        # Every statement below inside one savepoint, so a failure undoes only the
        # timeline rows. The send already reached Meta; it must stay recorded.
        with db.begin_nested():
            if thread_id is not None:
                # An agent writing into a thread they opened — address it directly,
                # which also works for a customer known only by username (no number).
                thread = db.query(AgentThread).filter(AgentThread.id == thread_id).first()
            else:
                thread = get_or_create_thread(db, company_id, mobile_no, contact_name)
            if thread is None:
                return None
            now = sent_at or datetime.now(timezone.utc)
            msg_id = _write(
                db, thread, direction="outbound", kind=kind,
                body_rendered=body_rendered, template_meta=template_meta, media=media,
                wamid=wamid, status=status, campaign_id=campaign_id,
                sent_by_agent_id=sent_by_agent_id,
                sent_at=now if status in ("sent", "delivered", "read") else None,
                client_msg_id=client_msg_id, request_hash=request_hash,
            )
            _touch(db, thread, now=now, inbound=False)
            # Whoever contacted the customer sees their replies from now on.
            if sent_by_agent_id:
                add_participant(db, thread.id, sent_by_agent_id,
                                "broadcast" if kind == "broadcast" else "reply", now)
        return msg_id
    except Exception as exc:
        # Never abort the send that produced this. The message reached Meta; a missing
        # chat bubble is a worse-looking chat window, not a lost customer message.
        logger.warning("agent_timeline.record_outbound failed for %s: %s", mobile_no, exc)
        return None


def record_inbound(db: Session, company_id, mobile_no: str | None, *,
                   bsuid: str | None = None,
                   body_rendered: str | None = None,
                   media: dict | None = None,
                   wamid: str | None = None,
                   replies_to_wamid: str | None = None,
                   contact_name: str | None = None,
                   received_at: datetime | None = None):
    """
    Record a message the customer sent, and open the 24-hour window.

    Called for **every** inbound, not only those outside a service flow. The chat window
    shows the whole conversation with a customer; recording only the messages that failed
    to match a flow would produce a transcript with the answers missing.
    """
    try:
        with db.begin_nested():
            thread = get_or_create_thread(db, company_id, mobile_no, contact_name,
                                          bsuid=bsuid)
            if thread is None:
                return None
            now = received_at or datetime.now(timezone.utc)
            msg_id = _write(
                db, thread, direction="inbound", kind="reply",
                body_rendered=body_rendered, media=media, wamid=wamid,
                replies_to_wamid=replies_to_wamid, status="received",
            )
            # This, and only this, starts the clock — and it restarts on every inbound.
            _touch(db, thread, now=now, inbound=True)
        return msg_id
    except Exception as exc:
        logger.warning("agent_timeline.record_inbound failed for %s: %s", mobile_no, exc)
        return None


def relink_identity(db: Session, company_id, *, old_mobile=None, new_mobile=None,
                    old_bsuid=None, new_bsuid=None) -> bool:
    """
    Move a customer's thread to their new number and/or user id, so the conversation
    continues in the same thread. Returns True if a thread was updated.

    If the new identifier already belongs to another thread of this company, nothing is
    changed: two threads for one person is resolved by scripts/normalize_numbers.py, not
    by overwriting mid-webhook. Never raises.
    """
    try:
        with db.begin_nested():
            thread = _find_thread(db, company_id, old_mobile, old_bsuid)
            if thread is None:
                return False
            sets, params = [], {"i": str(thread.id)}
            if new_mobile and new_mobile != thread.mobile_no \
                    and not _taken(db, thread, "mobile_no", new_mobile):
                sets.append("mobile_no = :m")
                params["m"] = new_mobile
            if new_bsuid and new_bsuid != thread.bsuid \
                    and not _taken(db, thread, "bsuid", new_bsuid):
                sets.append("bsuid = :b")
                params["b"] = new_bsuid
            if not sets:
                return False
            db.execute(text(f"UPDATE agent_threads SET {', '.join(sets)} WHERE id = :i"),
                       params)
            bump_rev(db, thread.id)
        return True
    except Exception as exc:
        logger.warning("agent_timeline.relink_identity failed: %s", exc)
        return False


# ── Delivery receipts ─────────────────────────────────────────────────────────

# Same ranking rule as broadcast_status: receipts arrive out of order, and a late
# "delivered" must not walk a row back from "read".
_RANK = {"queued": 0, "sent": 1, "unconfirmed": 1, "delivered": 2, "read": 3}


# Meta error code → what the app shows on a failed bubble.
_FAILURE_CATEGORIES = {
    "131026": "blocked", "131049": "blocked", "131050": "blocked",
    "131047": "outside_window",
    "131042": "billing",
    "130429": "rate_limited", "131056": "rate_limited", "131048": "rate_limited",
}


def failure_category(code) -> str:
    code = str(code or "")
    if code in _FAILURE_CATEGORIES:
        return _FAILURE_CATEGORIES[code]
    if code.startswith("132"):
        return "template"
    return "other"


def apply_status(db: Session, wamid: str, state: str,
                 ts: datetime | None = None, errors: list | None = None) -> bool:
    """
    Move a timeline row's delivery state. Returns True if a row matched.

    Called from handle_status for *every* receipt, before it branches on which store
    owns the message — a timeline row can exist for a broadcast send or an agent's own
    reply, and the caller should not have to know which.

    A `failed` receipt always applies, even after `read`: Meta contradicting itself is
    news, and the ranking guard exists to stop progress running backwards, not to
    suppress a failure.
    """
    if not wamid or not state:
        return False

    msg = db.query(AgentMessage).filter(AgentMessage.wamid == wamid).first()
    if msg is None:
        return False

    if state == "failed":
        if msg.status != "failed":
            msg.status = "failed"
            err = (errors or [{}])[0] or {}
            code = err.get("code")
            msg.error_code = str(code)[:20] if code else None
            msg.error_message = (err.get("title") or err.get("message") or "")[:500] or None
            msg.failure_category = failure_category(code)
            msg.rev = bump_rev(db, msg.thread_id)
        return True

    if _RANK.get(state, 0) <= _RANK.get(msg.status, 0):
        return True                      # matched, but this receipt is stale

    msg.status = state
    if state == "delivered":
        msg.delivered_at = ts or datetime.now(timezone.utc)
    elif state == "read":
        msg.read_at = ts or datetime.now(timezone.utc)
    # A real change: give it a change number so the app's changed_after_rev sync
    # picks up the new tick. A stale receipt (returned above) bumps nothing.
    msg.rev = bump_rev(db, msg.thread_id)
    return True


# ── Reactions ─────────────────────────────────────────────────────────────────

def apply_reaction(db: Session, company_id, target_wamid: str, emoji: str | None,
                   who: str = "customer") -> bool:
    """
    A reaction updates the message it reacts to — never a new bubble. An empty emoji
    removes `who`'s reaction (WhatsApp's "un-react"). Returns True if a message matched.
    Joins the caller's transaction; never raises.
    """
    try:
        with db.begin_nested():
            msg = (db.query(AgentMessage)
                   .join(AgentThread, AgentThread.id == AgentMessage.thread_id)
                   .filter(AgentThread.company_id == company_id,
                           AgentMessage.wamid == target_wamid)
                   .first())
            if msg is None:
                return False
            reactions = {k: [w for w in v if w != who]
                         for k, v in dict(msg.reactions or {}).items()}
            if emoji:
                reactions.setdefault(emoji, []).append(who)
            msg.reactions = {k: v for k, v in reactions.items() if v} or None
            msg.rev = bump_rev(db, msg.thread_id)
        return True
    except Exception as exc:
        logger.warning("agent_timeline.apply_reaction failed: %s", exc)
        return False


# ── The 24-hour window ────────────────────────────────────────────────────────

def window_state(thread: AgentThread, now: datetime | None = None) -> dict:
    """
    Whether free text is currently allowed on this thread.

    Returned to the app so it can disable a composer it knows will be rejected — but
    the server re-checks at send and is the only authority. The app's countdown is a
    courtesy that stops an agent typing a paragraph they cannot send.
    """
    now = now or datetime.now(timezone.utc)
    opened = thread.last_inbound_at

    if opened is None:
        # They have never written to us, so the window has never been open. A template
        # we sent does not count and never did.
        return {"last_inbound_at": None, "window_expires_at": None, "state": "closed"}

    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    expires = opened + timedelta(hours=SERVICE_WINDOW_HOURS)

    if expires <= now:
        state = "closed"
    elif (expires - now).total_seconds() <= 3600:
        state = "closing_soon"
    else:
        state = "open"

    return {"last_inbound_at": opened, "window_expires_at": expires, "state": state}


def is_open(thread: AgentThread, now: datetime | None = None) -> bool:
    return window_state(thread, now)["state"] != "closed"
