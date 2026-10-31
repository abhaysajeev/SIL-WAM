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

from app.models.agent_timeline import AgentMessage, AgentThread

logger = logging.getLogger(__name__)

# WhatsApp's service window. Free text is allowed only within this long of the
# customer's last inbound message; outside it, approved templates only.
SERVICE_WINDOW_HOURS = 24


# ── Threads ───────────────────────────────────────────────────────────────────

def get_or_create_thread(db: Session, company_id, mobile_no: str,
                         contact_name: str | None = None) -> AgentThread | None:
    """
    The thread for one customer of one company, created if absent.

    `mobile_no` must already be in the canonical stored form — pass the value
    `conversation_engine._resolve_stored_mobile` returned, not Meta's raw `from`. A
    thread keyed on the unnormalised number splits one customer into two, and the
    unique constraint cannot catch that because both spellings are genuinely distinct
    strings.

    **Inserted with a Core statement, not `db.add()` + `db.flush()`.** `Session.flush()`
    writes *every* pending object in the session, not only ours — in `handle_inbound`
    that includes the customer's `Message` row, which would then be inside our SAVEPOINT
    and be discarded along with it if a timeline write failed. A Core INSERT executes on
    its own and touches nothing the caller has staged.

    `ON CONFLICT DO NOTHING` handles the race directly, so there is no IntegrityError to
    recover from and no rollback of any kind.
    """
    thread = (
        db.query(AgentThread)
        .filter(AgentThread.company_id == company_id,
                AgentThread.mobile_no == mobile_no)
        .first()
    )
    if thread is not None:
        # Fill in a name we did not have; never overwrite a known one with nothing,
        # which is what a broadcast row lacking customer_name would otherwise do.
        if contact_name and not thread.contact_name:
            thread.contact_name = contact_name
            db.execute(
                text("UPDATE agent_threads SET contact_name = :n WHERE id = :i"),
                {"n": contact_name, "i": str(thread.id)},
            )
        return thread

    db.execute(
        text("""
            INSERT INTO agent_threads (id, company_id, mobile_no, contact_name)
            VALUES (gen_random_uuid(), :company_id, :mobile_no, :contact_name)
            ON CONFLICT (company_id, mobile_no) DO NOTHING
        """),
        {"company_id": str(company_id), "mobile_no": mobile_no,
         "contact_name": contact_name},
    )
    # Re-read rather than use RETURNING: with DO NOTHING, a losing insert returns no
    # row at all, and this one query is correct whether we won the race or not.
    return (
        db.query(AgentThread)
        .filter(AgentThread.company_id == company_id,
                AgentThread.mobile_no == mobile_no)
        .first()
    )


def _next_seq(db: Session, thread_id) -> int:
    """
    Allocate the next position in a thread.

    `UPDATE ... RETURNING` in one statement, so the row lock Postgres takes serialises
    concurrent writers and the sequence is gap-free. Reading the counter and writing it
    back separately would let two writers read the same value and produce a duplicate
    seq — caught by the unique constraint, but as a 500 rather than a correct ordering.

    Gap-free matters because it is what lets the app sync with "everything after seq N"
    and know it has missed nothing. A gap is indistinguishable from a message the client
    failed to fetch.
    """
    return db.execute(
        text("""
            UPDATE agent_threads
            SET message_seq = message_seq + 1
            WHERE id = :tid
            RETURNING message_seq
        """),
        {"tid": str(thread_id)},
    ).scalar()


_INSERT_MESSAGE = text("""
    INSERT INTO agent_messages (
        id, thread_id, seq, direction, kind, body_rendered, template_meta, media,
        wamid, status, campaign_id, sent_by_agent_id, replies_to_wamid, sent_at
    ) VALUES (
        gen_random_uuid(), :thread_id, :seq, :direction, :kind, :body_rendered,
        CAST(:template_meta AS jsonb), CAST(:media AS jsonb),
        :wamid, :status, :campaign_id, :sent_by_agent_id, :replies_to_wamid, :sent_at
    )
    RETURNING id
""")


def _write(db: Session, thread, *, direction: str, kind: str,
           body_rendered=None, template_meta=None, media=None, wamid=None,
           status="sent", campaign_id=None, sent_by_agent_id=None,
           replies_to_wamid=None, sent_at=None):
    """Insert one message row and return its id. Core, for the reason above."""
    return db.execute(_INSERT_MESSAGE, {
        "thread_id":        str(thread.id),
        "seq":              _next_seq(db, thread.id),
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
                    sent_at: datetime | None = None):
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
            )
            _touch(db, thread, now=now, inbound=False)
        return msg_id
    except Exception as exc:
        # Never abort the send that produced this. The message reached Meta; a missing
        # chat bubble is a worse-looking chat window, not a lost customer message.
        logger.warning("agent_timeline.record_outbound failed for %s: %s", mobile_no, exc)
        return None


def record_inbound(db: Session, company_id, mobile_no: str, *,
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
            thread = get_or_create_thread(db, company_id, mobile_no, contact_name)
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


# ── Delivery receipts ─────────────────────────────────────────────────────────

# Same ranking rule as broadcast_status: receipts arrive out of order, and a late
# "delivered" must not walk a row back from "read".
_RANK = {"queued": 0, "sent": 1, "unconfirmed": 1, "delivered": 2, "read": 3}


def apply_status(db: Session, wamid: str, state: str,
                 ts: datetime | None = None) -> bool:
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
        msg.status = "failed"
        return True

    if _RANK.get(state, 0) <= _RANK.get(msg.status, 0):
        return True                      # matched, but this receipt is stale

    msg.status = state
    if state == "delivered":
        msg.delivered_at = ts or datetime.now(timezone.utc)
    elif state == "read":
        msg.read_at = ts or datetime.now(timezone.utc)
    return True


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
