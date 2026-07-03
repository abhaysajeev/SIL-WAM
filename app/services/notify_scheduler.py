"""
app/services/notify_scheduler.py — durable delivery of outbound status notifications.

notify_queue.enqueue_notification() writes fully-materialized rows to
OutboundNotification; this poller is the only place that actually POSTs them
to a client's notify_url. Same BackgroundScheduler/SKIP LOCKED pattern as
send_scheduler.py.

Known limitation: no sequencing between sibling rows for the same message_id/
service_id — a delayed "sent" retry can reach the client after a "delivered"
that succeeded on the first try. Not solved here (would need coalescing logic).
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.database import SessionLocal
from app.models.outbound_notification import OutboundNotification
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")

_BATCH_LIMIT = 25   # max notifications dispatched per tick
_MAX_ATTEMPTS = 8   # give up after this many failed attempts


# ── Public: called from main.py lifespan ────────────────────────────────────

def start() -> None:
    scheduler.add_job(
        _run_notify_job,
        trigger="interval",
        seconds=5,
        id="pending_notification_delivery",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Notify scheduler started (interval=5s)")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Notify scheduler stopped")


# ── Job entry point ──────────────────────────────────────────────────────────

def _run_notify_job() -> None:
    """Wrapped in its own try/except so a crash never kills the scheduler thread."""
    db = SessionLocal()
    try:
        sent = 0
        for _ in range(_BATCH_LIMIT):
            if not _send_one_pending(db):
                break
            sent += 1
        if sent:
            logger.info("Notify job: delivered/attempted %d notification(s)", sent)
    except Exception as exc:
        log_error("Notify scheduler job failed", "notify_scheduler._run_notify_job", exc)
    finally:
        db.close()


# ── Core logic ───────────────────────────────────────────────────────────────

def _send_one_pending(db) -> bool:
    """
    Claim and POST a single pending notification. Each claim + POST is its own
    transaction, mirroring send_scheduler's per-row commit discipline.

    Returns True if a notification was claimed and processed, False if none pending.
    """
    now = datetime.now(timezone.utc)

    notif = (
        db.query(OutboundNotification)
        .filter(
            OutboundNotification.status == "pending",
            OutboundNotification.next_attempt_at <= now,
        )
        .order_by(OutboundNotification.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    if not notif:
        return False

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(notif.notify_url, json=notif.payload)
        if 200 <= resp.status_code < 300:
            notif.status = "delivered"
            notif.delivered_at = now
        else:
            _record_failure(notif, f"HTTP {resp.status_code}: {resp.text[:500]}")
    except Exception as exc:
        _record_failure(notif, str(exc))

    db.commit()
    return True


def _record_failure(notif: OutboundNotification, error: str) -> None:
    notif.attempts += 1
    notif.last_error = error
    if notif.attempts >= _MAX_ATTEMPTS:
        notif.status = "failed"
        log_error(
            f"Notification delivery permanently failed after {notif.attempts} attempts",
            f"notify_scheduler → {notif.notify_url}",
            Exception(error),
        )
    else:
        backoff_seconds = min(30 * (2 ** (notif.attempts - 1)), 3600)
        notif.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
