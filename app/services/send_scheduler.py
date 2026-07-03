"""
app/services/send_scheduler.py — asynchronous template dispatch.

POST /client-api/v1/services returns immediately once a Service row is positioned
in the mobile queue (status="in_progress"/"waiting"). This scheduler is the only
place that actually calls the Meta Graph API: it polls for services that have
reached the front of the queue but haven't been sent yet (template_sent=False)
and dispatches them one at a time.

Uses SELECT ... FOR UPDATE SKIP LOCKED so multiple app instances can run this
poller concurrently without double-sending the same service.

The scheduler is a BackgroundScheduler (runs in a daemon thread), same pattern
as expiry_scheduler. Each job invocation opens its own DB session.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.database import SessionLocal
from app.models.conversation import Service
from app.models.whatsapp import WhatsAppAccount
from app.services import queue_manager
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")

_BATCH_LIMIT = 25  # max services dispatched per tick


# ── Public: called from main.py lifespan ────────────────────────────────────

def start() -> None:
    scheduler.add_job(
        _run_send_job,
        trigger="interval",
        seconds=5,
        id="pending_template_send",
        replace_existing=True,
        max_instances=1,         # never overlap if a run takes longer than 5s
    )
    scheduler.start()
    logger.info("Send scheduler started (interval=5s)")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Send scheduler stopped")


# ── Job entry point ──────────────────────────────────────────────────────────

def _run_send_job() -> None:
    """Wrapped in its own try/except so a crash never kills the scheduler thread."""
    db = SessionLocal()
    try:
        sent = 0
        for _ in range(_BATCH_LIMIT):
            if not _send_one_pending(db):
                break
            sent += 1
        if sent:
            logger.info("Send job: dispatched %d service(s)", sent)
    except Exception as exc:
        log_error("Send scheduler job failed", "send_scheduler._run_send_job", exc)
    finally:
        db.close()


# ── Core logic ───────────────────────────────────────────────────────────────

def _send_one_pending(db) -> bool:
    """
    Claim and dispatch a single pending service. Each claim + dispatch is its own
    transaction so a lock is held only as long as one send takes, and one failure
    can't roll back sends already committed earlier in the same job run.

    Returns True if a service was claimed and processed, False if none pending.
    """
    service = (
        db.query(Service)
        .filter(
            Service.status == "in_progress",
            Service.template_sent.is_(False),
        )
        .order_by(Service.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    if not service:
        return False

    account = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == service.company_id
    ).first()

    if not account:
        logger.error(
            "send_scheduler: no WhatsApp account for company_id=%s service=%s",
            service.company_id, service.service_id,
        )
        service.template_sent = True
        service.status = "failed"
        service.failed_reason = "send_error"
    else:
        try:
            queue_manager.send_template_for_service(db, service, account)
        except Exception as exc:
            log_error(
                f"send_template_for_service crashed for service={service.service_id}",
                "send_scheduler._send_one_pending",
                exc,
            )
            service.template_sent = True
            service.status = "failed"
            service.failed_reason = "send_error"

    db.commit()
    return True
