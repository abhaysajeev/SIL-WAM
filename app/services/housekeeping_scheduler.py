"""
app/services/housekeeping_scheduler.py — daily clean-up jobs.

  * media expiry (hourly) — delete stored media files past MEDIA_RETENTION_DAYS
    (media_assets.expire_due). The row stays, marked deleted, so a chat bubble can say
    "no longer available" rather than break.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.database import SessionLocal
from app.services import media_assets
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")


def start() -> None:
    scheduler.add_job(_run_media_expiry, trigger="interval", hours=1,
                      id="media_expiry", replace_existing=True, max_instances=1)
    scheduler.add_job(_run_inbound_media, trigger="interval", seconds=10,
                      id="inbound_media_fetch", replace_existing=True, max_instances=1)
    scheduler.add_job(_run_send_recovery, trigger="interval", seconds=30,
                      id="agent_send_recovery", replace_existing=True, max_instances=1)
    scheduler.add_job(_run_sync_notifier, trigger="interval", seconds=2,
                      id="agent_sync_notifier", replace_existing=True, max_instances=1)
    scheduler.add_job(_run_account_health, trigger="interval", seconds=60,
                      id="account_health", replace_existing=True, max_instances=1)
    scheduler.start()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _run_media_expiry() -> None:
    db = SessionLocal()
    try:
        while (n := media_assets.expire_due(db)):
            logger.info("Media expiry removed %d file(s)", n)
            if n < 500:
                break
    except Exception as exc:
        db.rollback()
        log_error("Media expiry failed", "housekeeping_scheduler._run_media_expiry", exc)
    finally:
        db.close()


def _run_inbound_media() -> None:
    db = SessionLocal()
    try:
        media_assets.fetch_inbound(db)
    except Exception as exc:
        db.rollback()
        log_error("Inbound media fetch failed", "housekeeping_scheduler._run_inbound_media", exc)
    finally:
        db.close()


def _run_send_recovery() -> None:
    from app.services import agent_send
    db = SessionLocal()
    try:
        agent_send.recover_stuck(db)
    except Exception as exc:
        db.rollback()
        log_error("Agent send recovery failed", "housekeeping_scheduler._run_send_recovery", exc)
    finally:
        db.close()


def _run_sync_notifier() -> None:
    from app.services import sync_notifier
    db = SessionLocal()
    try:
        while sync_notifier.run_once(db) >= sync_notifier.BATCH:
            pass
    except Exception as exc:
        db.rollback()
        log_error("Agent sync notifier failed", "housekeeping_scheduler._run_sync_notifier", exc)
    finally:
        db.close()


def _run_account_health() -> None:
    from app.services import account_health
    db = SessionLocal()
    try:
        account_health.check(db)
    except Exception as exc:
        db.rollback()
        log_error("Account health check failed", "housekeeping_scheduler._run_account_health", exc)
    finally:
        db.close()
