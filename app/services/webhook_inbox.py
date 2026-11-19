"""
Durable intake for Meta webhooks: store first, then process.

Two entry points do the processing, and they share `process_row`:

  * `process_now(row_id)` — called from the request's BackgroundTask right after the
    row is committed, so the normal case is handled within milliseconds.
  * `sweep()` — the scheduler's safety net. Claims anything still pending (the
    BackgroundTask died, a restart happened, an attempt failed and its backoff elapsed)
    or stuck in `processing` past its lease (the worker crashed mid-row).

**Claiming is a lease, not a lock held across processing.** The handlers in
conversation_engine commit as they go, so the work cannot share a transaction with the
"done" mark. Instead a row is claimed in its own short transaction (`processing` with a
`lease_until`), processed, then marked done. A crash after the handlers committed but
before the mark leaves an expired lease; the row is reclaimed and processed again, which
is safe because every handler is idempotent — inbound messages dedupe on wamid, status
receipts only ever move forward.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import SessionLocal
from app.models.failed_webhook import FailedWebhook
from app.models.webhook_inbox import WebhookInbox
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

MAX_ATTEMPTS   = 8
LEASE_SECONDS  = 300       # a row claimed longer ago than this is presumed abandoned
BASE_BACKOFF_S = 30        # 30s, 60s, 2m, 4m … capped below
MAX_BACKOFF_S  = 3600
SWEEP_BATCH    = 50
KEEP_DONE_DAYS = 14        # done/shadow rows are pruned after this


def payload_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def store(db, raw_body: bytes, payload: dict, *, status: str = "pending") -> int | None:
    """
    Insert one delivery and commit. Returns the row id, or None if this exact body was
    already stored (a Meta retry of something we have — nothing more to do).
    """
    stmt = (
        pg_insert(WebhookInbox)
        .values(source="meta", payload=payload, payload_sha256=payload_hash(raw_body),
                status=status)
        .on_conflict_do_nothing(constraint="uq_webhook_inbox_payload_sha256")
        .returning(WebhookInbox.id)
    )
    row_id = db.execute(stmt).scalar()
    db.commit()
    return row_id


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BASE_BACKOFF_S * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_S))


def _claim(db, row_id: int | None = None) -> list[tuple[int, dict]]:
    """
    Claim rows in one short transaction. With `row_id`, only that row (and only if it is
    still claimable); otherwise up to SWEEP_BATCH due or abandoned rows.
    """
    now = datetime.now(timezone.utc)
    rows = db.execute(
        text("""
            UPDATE webhook_inbox w
               SET status = 'processing',
                   lease_until = :lease,
                   attempts = w.attempts + 1
             WHERE w.id IN (
                SELECT id FROM webhook_inbox
                 WHERE (CAST(:rid AS bigint) IS NULL OR id = CAST(:rid AS bigint))
                   AND ((status = 'pending'
                         AND (next_attempt_at IS NULL OR next_attempt_at <= :now))
                        OR (status = 'processing' AND lease_until < :now))
                 ORDER BY id
                 LIMIT :lim
                 FOR UPDATE SKIP LOCKED)
            RETURNING w.id, w.payload, w.attempts
        """),
        {"lease": now + timedelta(seconds=LEASE_SECONDS), "now": now,
         "rid": row_id, "lim": 1 if row_id else SWEEP_BATCH},
    ).fetchall()
    db.commit()
    return [(r.id, r.payload, r.attempts) for r in rows]


def _finish(row_id: int, attempts: int, payload: dict, exc: Exception | None) -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        if exc is None:
            db.execute(text("""UPDATE webhook_inbox SET status='done', processed_at=:now,
                                      lease_until=NULL, last_error=NULL WHERE id=:id"""),
                       {"now": now, "id": row_id})
        elif attempts >= MAX_ATTEMPTS:
            db.execute(text("""UPDATE webhook_inbox SET status='dead', lease_until=NULL,
                                      last_error=:err WHERE id=:id"""),
                       {"err": f"{type(exc).__name__}: {exc}"[:2000], "id": row_id})
            # The existing replay page reads failed_webhooks; a dead row belongs there.
            db.add(FailedWebhook(source="meta", raw_payload=payload,
                                 error_type=type(exc).__name__,
                                 traceback=f"webhook_inbox id={row_id}: {exc}"[:4000],
                                 replayed=False))
        else:
            db.execute(text("""UPDATE webhook_inbox SET status='pending', lease_until=NULL,
                                      next_attempt_at=:nxt, last_error=:err WHERE id=:id"""),
                       {"nxt": now + _backoff(attempts),
                        "err": f"{type(exc).__name__}: {exc}"[:2000], "id": row_id})
        db.commit()
    except Exception as mark_exc:
        db.rollback()
        # Not fatal: an unmarked row's lease expires and the sweep reprocesses it.
        logger.error("webhook_inbox: could not mark row %s: %s", row_id, mark_exc)
    finally:
        db.close()


def process_row(row_id: int, payload: dict, attempts: int) -> bool:
    """Run the normal webhook processing for one claimed row. Returns True on success."""
    # Imported here: meta_webhook imports this module.
    from app.api.meta_webhook import _process_payload

    db = SessionLocal()
    error: Exception | None = None
    try:
        _process_payload(payload, db)
    except Exception as exc:
        db.rollback()
        error = exc
        log_error("Webhook inbox processing failed", f"webhook_inbox id={row_id}", exc,
                  request_data={"attempt": attempts})
    finally:
        db.close()
    _finish(row_id, attempts, payload, error)
    return error is None


def process_now(row_id: int) -> None:
    """BackgroundTask entry: process the row just stored, if nobody else has claimed it."""
    db = SessionLocal()
    try:
        claimed = _claim(db, row_id)
    except Exception as exc:
        logger.error("webhook_inbox: claim of %s failed, sweep will retry: %s", row_id, exc)
        return
    finally:
        db.close()
    for rid, payload, attempts in claimed:
        process_row(rid, payload, attempts)


def sweep() -> int:
    """Scheduler entry: claim and process due/abandoned rows. Returns how many ran."""
    db = SessionLocal()
    try:
        claimed = _claim(db)
    finally:
        db.close()
    for rid, payload, attempts in claimed:
        process_row(rid, payload, attempts)
    return len(claimed)


def prune() -> int:
    """Delete processed rows past their keep period. Dead rows are kept for inspection."""
    db = SessionLocal()
    try:
        n = db.execute(
            text("""DELETE FROM webhook_inbox
                     WHERE status IN ('done', 'shadow') AND received_at < :cut"""),
            {"cut": datetime.now(timezone.utc) - timedelta(days=KEEP_DONE_DAYS)},
        ).rowcount
        db.commit()
        return n or 0
    finally:
        db.close()

