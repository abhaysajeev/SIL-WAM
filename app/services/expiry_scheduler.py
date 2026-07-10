"""
app/services/expiry_scheduler.py — Condition A service expiry.

Condition A (architecture §9.6):
  If a customer does not tap the template button within template_expiry_hours,
  the service is marked expired with reason "timeout" and the queue advances.

This applies only when NO questions have been answered yet (sent==0 for all questions).
Once the customer taps the button, Q1 fires and the flow is "started" — Condition A
no longer applies, only normal completion (or a later timeout mid-flow, same deadline).

Template-only services (questions=None or []) are marked completed immediately on
template send by queue_manager, so they are never in_progress when this job runs.

The scheduler is a BackgroundScheduler (runs in a daemon thread). Each job invocation
opens its own DB session and closes it on completion, following the same pattern as
meta_webhook BackgroundTasks.
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.database import SessionLocal
from app.models.conversation import MobileQueue, Service
from app.models.whatsapp import WhatsAppAccount
from app.services import notify_queue, queue_manager
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")


# ── Public: called from main.py lifespan ────────────────────────────────────

def start() -> None:
    scheduler.add_job(
        _run_expiry_job,
        trigger="interval",
        minutes=10,
        id="condition_a_expiry",
        replace_existing=True,
        max_instances=1,         # never overlap if a run takes longer than 10 min
    )
    scheduler.start()
    logger.info("Expiry scheduler started (interval=10min)")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Expiry scheduler stopped")


# ── Job entry point ──────────────────────────────────────────────────────────

def _run_expiry_job() -> None:
    """Wrapped in its own try/except so a crash never kills the scheduler thread."""
    db = SessionLocal()
    try:
        expired = _expire_timed_out_services(db)
        if expired:
            logger.info("Expiry job: expired %d service(s)", expired)
    except Exception as exc:
        log_error("Condition A expiry job failed", "expiry_scheduler._run_expiry_job", exc)
    finally:
        db.close()


# ── Core logic ───────────────────────────────────────────────────────────────

def _expire_timed_out_services(db) -> int:
    """
    Find and expire all in_progress services past their deadline.

    Applies to ALL in_progress services — whether the customer never tapped
    the template button (Condition A) or started answering but went silent
    mid-flow. The deadline is always created_at + template_expiry_hours.

    Returns the number of services expired.
    """
    now = datetime.now(timezone.utc)

    # Load all in_progress services via their queue entries
    candidates = (
        db.query(Service, MobileQueue)
        .join(MobileQueue, MobileQueue.service_id == Service.id)
        .filter(
            Service.status        == "in_progress",
            Service.template_sent.is_(True),
            MobileQueue.status    == "in_progress",
        )
        .all()
    )

    expired_count = 0

    for svc, queue_entry in candidates:
        # Skip template-only services — they complete immediately, shouldn't appear here
        if not svc.questions:
            continue

        # Check expiry deadline
        expiry_hours = svc.template_expiry_hours or 24
        created = svc.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        deadline = created + timedelta(hours=expiry_hours)

        if now < deadline:
            continue

        # Expire this service
        logger.info(
            "Expiring service %s (deadline=%s now=%s)",
            svc.service_id, deadline.isoformat(), now.isoformat(),
        )
        svc.status         = "expired"
        svc.expired_reason = "timeout"
        queue_entry.status = "completed"
        notify_queue.enqueue_notification(db, svc, "expired", note="timeout")

        # Advance the queue for this mobile — start the next waiting service
        mobile_no = (svc.data or {}).get("customer_mobile", "")
        if mobile_no:
            account = db.query(WhatsAppAccount).filter(
                WhatsAppAccount.company_id == svc.company_id
            ).first()
            if account:
                try:
                    queue_manager.advance_queue(db, mobile_no, svc.company_id, account)
                except Exception as exc:
                    log_error(
                        f"advance_queue failed after expiry service={svc.service_id}",
                        "expiry_scheduler._expire_timed_out_services",
                        exc,
                    )

        expired_count += 1

    if expired_count:
        db.commit()

    return expired_count


# ── Manual trigger (for tests and admin tooling) ─────────────────────────────

def run_once_now() -> int:
    """
    Run the expiry job synchronously in the calling thread.
    Useful in tests: call this directly instead of waiting for the scheduler.
    Opens and closes its own DB session.
    """
    db = SessionLocal()
    try:
        return _expire_timed_out_services(db)
    finally:
        db.close()
