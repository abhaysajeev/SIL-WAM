"""
app/services/webhook_inbox_scheduler.py — the safety net behind the webhook inbox.

Three jobs, same BackgroundScheduler pattern as notify_scheduler:

  * sweep   (5s)   — process inbox rows the request's BackgroundTask did not finish.
                     Only meaningful when WEBHOOK_INBOX_MODE=on.
  * prune   (6h)   — delete processed rows older than webhook_inbox.KEEP_DONE_DAYS.
  * silence (10m)  — alarm for a number that is sending but hearing nothing back.

The silence alarm exists because of how the 403 incident looked from the inside: sends
succeeded, campaigns sat at "awaiting confirmation", and nothing anywhere said the
webhook was being rejected. A number that has sent in the last hour and received no
webhook at all for 30 minutes is almost always a webhook problem, not a quiet customer
base — Meta sends a `sent` receipt for every accepted message.
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import webhook_inbox
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")

SILENCE_SENT_WINDOW = timedelta(hours=1)
SILENCE_QUIET_FOR   = timedelta(minutes=30)

# Throttle: one alarm per number per this long, so a long outage is one entry an hour
# in the error log rather than six.
_ALARM_EVERY = timedelta(hours=1)
_last_alarm: dict[str, datetime] = {}


def start() -> None:
    scheduler.add_job(_run_sweep, trigger="interval", seconds=5,
                      id="webhook_inbox_sweep", replace_existing=True, max_instances=1)
    scheduler.add_job(_run_prune, trigger="interval", hours=6,
                      id="webhook_inbox_prune", replace_existing=True, max_instances=1)
    scheduler.add_job(_run_silence_check, trigger="interval", minutes=10,
                      id="webhook_silence_alarm", replace_existing=True, max_instances=1)
    scheduler.start()
    logger.info("Webhook inbox scheduler started (mode=%s)", settings.WEBHOOK_INBOX_MODE)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _run_sweep() -> None:
    if settings.WEBHOOK_INBOX_MODE != "on":
        return
    try:
        n = webhook_inbox.sweep()
        if n:
            logger.info("Webhook inbox sweep processed %d row(s)", n)
    except Exception as exc:
        log_error("Webhook inbox sweep failed", "webhook_inbox_scheduler._run_sweep", exc)


def _run_prune() -> None:
    try:
        webhook_inbox.prune()
    except Exception as exc:
        log_error("Webhook inbox prune failed", "webhook_inbox_scheduler._run_prune", exc)


def silent_numbers(db, now: datetime | None = None) -> list:
    """Numbers that sent in the last hour but have had no webhook for 30 minutes."""
    now = now or datetime.now(timezone.utc)
    return db.execute(
        text("""
            SELECT w.company_id, w.phone_number_id, w.last_webhook_at,
                   MAX(r.sent_at) AS last_sent_at
              FROM whatsapp_accounts w
              JOIN broadcast_campaigns c ON c.company_id = w.company_id
              JOIN broadcast_recipients r ON r.campaign_id = c.id
             WHERE r.sent_at >= :sent_after
               AND w.phone_number_id IS NOT NULL
             GROUP BY w.company_id, w.phone_number_id, w.last_webhook_at
            HAVING (w.last_webhook_at IS NULL OR w.last_webhook_at < :quiet_before)
               AND MAX(r.sent_at) < :quiet_before
        """),
        {"sent_after": now - SILENCE_SENT_WINDOW, "quiet_before": now - SILENCE_QUIET_FOR},
    ).fetchall()


def _run_silence_check() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        for row in silent_numbers(db, now):
            key = str(row.phone_number_id)
            if key in _last_alarm and now - _last_alarm[key] < _ALARM_EVERY:
                continue
            _last_alarm[key] = now
            log_error(
                "No Meta webhooks for a sending number",
                "webhook_inbox_scheduler._run_silence_check",
                Exception(
                    f"phone_number_id={row.phone_number_id} sent at {row.last_sent_at} "
                    f"but last webhook was {row.last_webhook_at or 'never'}. Check the "
                    f"webhook signature (403s) and the WABA's subscribed apps."),
                request_data={"company_id": str(row.company_id)},
            )
    except Exception as exc:
        log_error("Webhook silence check failed",
                  "webhook_inbox_scheduler._run_silence_check", exc)
    finally:
        db.close()
