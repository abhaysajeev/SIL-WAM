"""
The chat window: the agent's conversation list, and one conversation's messages.

Two read endpoints, both **keyset paginated**, never `OFFSET`. Under offset paging a row
arriving while someone scrolls shifts every later page by one, so a message is silently
skipped or shown twice — and in a chat that is a customer's question the agent never
sees. Keyset asks for "the 30 before seq N", which is stable no matter what lands
meanwhile, and costs the same on page 40 as on page 1.

The thread list and the chat window read the same table with different filters:

  inbox    — threads where the customer has actually written (last_inbound_at IS NOT NULL)
  contact  — one thread by id or by number, whether or not they ever replied

That distinction is the whole reason a 2,000-recipient campaign does not fill an agent's
inbox with 2,000 conversations, while still letting them open any one contact and see
what was sent.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.core.database import get_db
from app.models.agent_timeline import AgentThread
from app.core import phone
from app.services import agent_timeline

router = APIRouter(prefix="/threads", tags=["Agent App"])

# One screen's worth. The app asks for more as the reader scrolls.
DEFAULT_LIMIT = 30
MAX_LIMIT = 100


class MessageOut(BaseModel):
    id:            str
    seq:           int
    # The thread's change number at this message's last change (new, status, …).
    # Sync with changed_after_rev = the highest rev you hold.
    rev:           int
    direction:     str
    kind:          str
    body:          str | None
    template:      dict | None
    media:         dict | None
    status:        str
    error_message: str | None
    # For a failed message: blocked | outside_window | billing | rate_limited |
    # template | other — show this, not a bare "failed".
    failure_category: str | None = None
    wamid:         str | None
    replies_to:    str | None
    # agents.id of the agent who sent it (null for the customer and for web-panel
    # sends). Lets a teammate's reply be labelled with who wrote it.
    sent_by_agent_id: str | None = None
    # {"👍": ["customer"]} — reactions to this message.
    reactions:     dict | None = None
    created_at:    datetime
    delivered_at:  datetime | None
    read_at:       datetime | None


class ThreadOut(BaseModel):
    id:              str
    # Null only for a customer known solely by WhatsApp username (see bsuid).
    mobile_no:       str | None
    contact_name:    str | None
    last_message_at: datetime | None
    message_seq:     int
    # Highest change number in this thread. Larger than what the app holds → sync.
    rev:             int
    # The 24-hour window, resolved server-side. See window_state below.
    last_inbound_at:   datetime | None
    window_expires_at: datetime | None
    window_state:      str
    opted_out:         bool
    last_message:      str | None


class ThreadListOut(BaseModel):
    threads: list[ThreadOut]
    # Pass back as `before` to get the next page. Absent when there are no more.
    next_cursor: str | None
    # The app's clock is not trustworthy — see the window contract below.
    server_time: datetime


class MessagesOut(BaseModel):
    thread:      ThreadOut
    messages:    list[MessageOut]
    # True when older messages exist above what was returned.
    has_more:    bool
    server_time: datetime


def message_out(r) -> MessageOut:
    """One message for the app — from an ORM row or a raw SELECT * row alike."""
    return MessageOut(
        id=str(r.id), seq=r.seq, rev=r.rev, direction=r.direction, kind=r.kind,
        body=r.body_rendered, template=r.template_meta, media=r.media,
        status=r.status, error_message=r.error_message,
        failure_category=getattr(r, "failure_category", None),
        wamid=r.wamid, replies_to=r.replies_to_wamid,
        sent_by_agent_id=str(r.sent_by_agent_id) if r.sent_by_agent_id else None,
        reactions=getattr(r, "reactions", None),
        created_at=r.created_at, delivered_at=r.delivered_at, read_at=r.read_at,
    )


def _window(thread: AgentThread) -> dict:
    return agent_timeline.window_state(thread)


def _thread_out(thread: AgentThread, opted_out: bool = False,
                last_message: str | None = None) -> ThreadOut:
    w = _window(thread)
    return ThreadOut(
        id=str(thread.id),
        mobile_no=thread.mobile_no,
        contact_name=thread.contact_name,
        last_message_at=thread.last_message_at,
        message_seq=thread.message_seq or 0,
        rev=thread.rev or 0,
        last_inbound_at=w["last_inbound_at"],
        window_expires_at=w["window_expires_at"],
        window_state=w["state"],
        opted_out=opted_out,
        last_message=last_message,
    )


@router.get("", response_model=ThreadListOut)
def list_threads(
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    before: str | None = Query(
        None, description="last_message_at of the oldest thread you hold, ISO-8601"),
    include_silent: bool = Query(
        False, description="also return contacts who have never replied"),
):
    """
    The agent's conversation list, most recent first.

    **Only threads the customer has written to**, unless `include_silent=true`. A
    campaign to 2,000 people creates 2,000 threads; an inbox listing all of them would
    bury the handful of people actually waiting for a reply. The silent ones are still
    reachable — open the contact from a phonebook and its whole history is there.

    `opted_out` and the last message line are resolved in the same query rather than
    per row: this endpoint is polled, and N+1 here scales with the size of the inbox.
    """
    rows = db.execute(
        text("""
            SELECT t.id, t.mobile_no, t.contact_name, t.last_inbound_at,
                   t.last_message_at, t.message_seq, t.rev,
                   (mo.mobile_no IS NOT NULL) AS opted_out,
                   (SELECT m.body_rendered
                      FROM agent_messages m
                     WHERE m.thread_id = t.id
                     ORDER BY m.seq DESC
                     LIMIT 1) AS last_message
              FROM agent_threads t
              -- Opt-out is company-scoped: unsubscribing from one business must not
              -- unsubscribe you from another (BROADCAST_DESIGN.md §9).
              LEFT JOIN messaging_optouts mo
                     ON mo.mobile_no  = t.mobile_no
                    AND mo.company_id = t.company_id
             WHERE t.company_id = :company_id
               -- Only customers this agent contacted in the last 30 days.
               AND """ + agent_timeline.VISIBLE_TO_AGENT_SQL + """
               AND (CAST(:include_silent AS boolean) OR t.last_inbound_at IS NOT NULL)
               -- Cast both sides: a bare `:before IS NULL` gives Postgres no way to
               -- infer the parameter's type and fails outright on the first page,
               -- which is the one every client requests.
               AND (CAST(:before AS timestamptz) IS NULL
                    OR t.last_message_at < CAST(:before AS timestamptz))
             ORDER BY t.last_message_at DESC NULLS LAST, t.id DESC
             LIMIT :limit
        """),
        {"company_id": ctx.company_id, "include_silent": include_silent,
         "before": before, "limit": limit, "agent_id": ctx.agent_row_id},
    ).fetchall()

    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        # A lightweight stand-in so window_state can be reused rather than reimplemented
        # in SQL — one rule for the 24-hour window, in one place.
        stub = AgentThread(mobile_no=r.mobile_no, contact_name=r.contact_name,
                           last_inbound_at=r.last_inbound_at,
                           last_message_at=r.last_message_at,
                           message_seq=r.message_seq, rev=r.rev)
        stub.id = r.id
        out.append(_thread_out(stub, opted_out=bool(r.opted_out),
                               last_message=r.last_message))

    # Only offer a cursor when the page was full; a short page is the end of the list,
    # and handing back a cursor there makes the app fetch an empty page to find out.
    next_cursor = (
        out[-1].last_message_at.isoformat()
        if len(out) == limit and out and out[-1].last_message_at else None
    )
    return ThreadListOut(threads=out, next_cursor=next_cursor, server_time=now)


@router.get("/by-mobile/{mobile_no}", response_model=MessagesOut)
def thread_by_mobile(
    mobile_no: str,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    """
    Open a contact from a phonebook, whether or not they have ever replied.

    Returns 404 rather than an empty thread when nothing has ever been sent to this
    number either: an empty chat window for someone we have never contacted is a
    different thing from a conversation, and inventing a thread here would put a row
    in the inbox the moment they did reply to something they never received.
    """
    # The phonebook may hold an older spelling (8075571893); threads are keyed on the
    # canonical form (918075571893). Try canonical first, then the value as given.
    candidates = [mobile_no]
    canonical = phone.try_normalize(mobile_no, phone.company_region(db, ctx.company_id))
    if canonical and canonical != mobile_no:
        candidates.insert(0, canonical)
    thread = (
        db.query(AgentThread)
        .filter(AgentThread.company_id == ctx.company_id,
                AgentThread.mobile_no.in_(candidates))
        .order_by(AgentThread.mobile_no == candidates[0])
        .first()
    )
    # Same rule as the list: a customer another agent is talking to is not found here,
    # however the number was obtained.
    if thread is None or not agent_timeline.is_visible(db, thread.id, ctx.agent_row_id):
        raise errors.not_found("conversation")
    return _messages_page(db, ctx, thread, limit=limit, before_seq=None, after_seq=None)


@router.get("/{thread_id}/messages", response_model=MessagesOut)
def thread_messages(
    thread_id: str,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    before_seq: int | None = Query(
        None, description="scroll up: messages older than this seq"),
    after_seq: int | None = Query(
        None, description="sync: messages newer than this seq"),
    changed_after_rev: int | None = Query(
        None, ge=0, description="sync: messages new OR changed since this rev"),
):
    """
    One conversation's messages, newest first.

    Three calls, one endpoint:

      (none)       open the chat  → the newest `limit`
      before_seq=N scroll upwards → the `limit` immediately older than N
      after_seq=N  poll for new   → everything newer than N, oldest first

    `after_seq` is the sync path and it is exact, not approximate: the sequence is
    gap-free per thread (allocated under the thread's row lock), so "everything after
    N" cannot silently omit a message the way a timestamp cursor can.
    """
    thread = (
        db.query(AgentThread)
        .filter(AgentThread.id == thread_id,
                # Scoped to the caller's company, so a thread id from another tenant
                # is a 404 rather than someone else's conversation.
                AgentThread.company_id == ctx.company_id)
        .first()
    )
    if thread is None or not agent_timeline.is_visible(db, thread.id, ctx.agent_row_id):
        raise errors.not_found("conversation")
    return _messages_page(db, ctx, thread, limit, before_seq, after_seq,
                          changed_after_rev=changed_after_rev)


def _messages_page(db, ctx, thread, limit, before_seq, after_seq,
                   changed_after_rev: int | None = None) -> MessagesOut:
    if changed_after_rev is not None:
        # The full sync: new messages AND existing ones whose status/reaction/media
        # changed, in the order the changes happened. rev is gap-free per thread, so
        # "everything above N" misses nothing; has_more says to call again from the
        # highest rev returned.
        rows = db.execute(
            text("""SELECT * FROM agent_messages
                     WHERE thread_id = :tid AND rev > :rev
                     ORDER BY rev ASC LIMIT :limit"""),
            {"tid": str(thread.id), "rev": changed_after_rev, "limit": limit + 1},
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
    elif after_seq is not None:
        rows = db.execute(
            text("""SELECT * FROM agent_messages
                     WHERE thread_id = :tid AND seq > :seq
                     ORDER BY seq ASC LIMIT :limit"""),
            {"tid": str(thread.id), "seq": after_seq, "limit": limit},
        ).fetchall()
        has_more = False        # catching up forwards; the client already has the rest
    else:
        rows = db.execute(
            text("""SELECT * FROM agent_messages
                     WHERE thread_id = :tid
                       AND (CAST(:before AS integer) IS NULL
                            OR seq < CAST(:before AS integer))
                     ORDER BY seq DESC LIMIT :limit"""),
            {"tid": str(thread.id), "before": before_seq, "limit": limit},
        ).fetchall()
        oldest = min((r.seq for r in rows), default=None)
        # Anything below the oldest row returned is older history still to fetch.
        has_more = bool(oldest and oldest > 1)

    opted_out = db.execute(
        text("""SELECT 1 FROM messaging_optouts
                 WHERE mobile_no = :m AND company_id = :c LIMIT 1"""),
        {"m": thread.mobile_no, "c": ctx.company_id},
    ).first() is not None

    # Populated from the rows already loaded rather than left null. The field exists
    # on every thread object, and a permanent null here reads as missing data to a
    # client that shows it in a list and a chat header from the same model.
    newest = max(rows, key=lambda r: r.seq).body_rendered if rows else None

    return MessagesOut(
        thread=_thread_out(thread, opted_out=opted_out, last_message=newest),
        messages=[message_out(r) for r in rows],
        has_more=has_more,
        server_time=datetime.now(timezone.utc),
    )
