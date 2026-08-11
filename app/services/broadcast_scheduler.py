"""
app/services/broadcast_scheduler.py — the broadcast send loop.

Polls for campaigns in "sending" and dispatches their pending recipients a batch at a
time. Deliberately a fourth in-process BackgroundScheduler rather than a separate
container (docs/BROADCAST_DESIGN.md §12): each scheduler owns its own thread pool, so a
long campaign cannot starve send_scheduler's transactional dispatch, and a worker
container would not isolate the real contention point — status receipts always return
through the main app's webhook.

Claims rows with SELECT ... FOR UPDATE SKIP LOCKED, the same pattern send_scheduler uses.
That is not needed today with one process, but it is what makes moving this into its own
container later a deployment change rather than a rewrite.

The progress this drives is *dispatch*, not delivery. Meta accepts a send and can report
failure seconds later through an async status webhook — broadcast_status.handle applies
those. A campaign is "dispatched" when every send was attempted, "settled" only once the
receipts are in.
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.database import SessionLocal
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import wa_sender
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")

# Recipients claimed per tick. Small enough that the transaction stays short and the
# DB pool (15 connections, shared with uvicorn and three other schedulers) is never
# held hostage by one campaign.
_BATCH_LIMIT = 20


# ── Public: called from main.py lifespan ────────────────────────────────────

def start() -> None:
    _recover_interrupted()
    scheduler.add_job(
        _run_broadcast_job,
        trigger="interval",
        seconds=3,
        id="broadcast_send",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Broadcast scheduler started (interval=3s)")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Broadcast scheduler stopped")


def _recover_interrupted() -> None:
    """
    Reset rows left mid-flight by a crash or restart.

    "sending" means a previous process claimed the row but never recorded an outcome.
    Resetting to "pending" is safe *because* status is per-recipient: a row that was
    actually sent is already "sent" and is never revisited. A batch-level cursor could
    not make that distinction and would have to fail the whole run instead.
    """
    db = SessionLocal()
    try:
        n = (
            db.query(BroadcastRecipient)
            .filter(BroadcastRecipient.status == "sending")
            .update({"status": "pending"}, synchronize_session=False)
        )
        db.commit()
        if n:
            logger.warning("Reset %d interrupted broadcast recipient(s) to pending", n)
    except Exception as exc:
        db.rollback()
        log_error("Broadcast recovery failed", "broadcast_scheduler._recover_interrupted", exc)
    finally:
        db.close()


# ── Job entry point ──────────────────────────────────────────────────────────

def _run_broadcast_job() -> None:
    """Wrapped so a crash never kills the scheduler thread."""
    db = SessionLocal()
    try:
        dispatch_pending(db)
    except Exception as exc:
        db.rollback()
        log_error("Broadcast send job failed", "broadcast_scheduler._run_broadcast_job", exc)
    finally:
        db.close()


def dispatch_pending(db) -> None:
    """
    Drain every campaign in "sending" using the caller's session.

    Separate from the job entry point so tests can drive a send without the scheduler
    opening its own session — same reason send_scheduler exposes _send_one_pending(db).
    """
    while _dispatch_one_batch(db):
        pass


def _dispatch_one_batch(db) -> bool:
    """Send one batch. Returns True if there may be more work for this tick."""
    campaign = (
        db.query(BroadcastCampaign)
        .filter(BroadcastCampaign.status == "sending")
        .order_by(BroadcastCampaign.dispatched_at)
        .first()
    )
    if campaign is None:
        return False

    account = (
        db.query(WhatsAppAccount)
        .filter(WhatsAppAccount.company_id == campaign.company_id)
        .first()
    )
    template = (
        db.query(WhatsAppTemplate)
        .filter(WhatsAppTemplate.id == campaign.template_id)
        .first()
    )
    if account is None or template is None:
        logger.error(
            "Campaign %s cannot send: account=%s template=%s",
            campaign.id, bool(account), bool(template),
        )
        # Fail the outstanding rows first. Without this the campaign keeps its
        # pending rows, _finish refuses to mark it done, and every tick picks it
        # up again forever.
        reason = "WhatsApp account not configured" if account is None else "Template no longer exists"
        stuck = (
            db.query(BroadcastRecipient)
            .filter(
                BroadcastRecipient.campaign_id == campaign.id,
                BroadcastRecipient.status.in_(("pending", "sending")),
            )
            .update({"status": "failed", "error_message": reason}, synchronize_session=False)
        )
        campaign.failed = (campaign.failed or 0) + stuck
        db.commit()
        _finish(db, campaign)
        return True

    # Claim a batch. SKIP LOCKED means a second process would take different rows
    # rather than blocking or double-sending.
    batch = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status == "pending",
        )
        .order_by(BroadcastRecipient.id)
        .with_for_update(skip_locked=True)
        .limit(_BATCH_LIMIT)
        .all()
    )
    if not batch:
        # An empty claim does NOT mean the campaign is finished, for two reasons:
        # the other runner (the immediate kick from /send, or this scheduler) may be
        # mid-flight with a batch it already marked "sending"; and SKIP LOCKED hides
        # rows it has locked but not yet updated, so they are still "pending" while
        # looking absent here. Count both states without locking — declaring the
        # campaign dispatched early reports it complete while messages are still
        # going out, which is exactly what the progress bar then shows.
        unfinished = (
            db.query(BroadcastRecipient)
            .filter(
                BroadcastRecipient.campaign_id == campaign.id,
                BroadcastRecipient.status.in_(("pending", "sending")),
            )
            .count()
        )
        if unfinished:
            return False        # another runner is mid-flight; yield to it
        _finish(db, campaign)
        return True             # this one is done — there may be other campaigns

    for r in batch:
        r.status = "sending"
    db.commit()

    stop_requested = False
    for r in batch:
        if stop_requested:
            r.status = "pending"      # hand back untouched for a later run
            continue
        if not _send_one(db, campaign, account, template, r):
            if campaign.stop_on_error:
                stop_requested = True

    db.commit()

    if stop_requested:
        campaign.status = "dispatched"
        campaign.dispatched_at = campaign.dispatched_at or datetime.now(timezone.utc)
        db.commit()
        logger.warning("Campaign %s halted on first error (stop_on_error)", campaign.id)
        return True

    return True


def _send_one(db, campaign, account, template, recipient) -> bool:
    """Send one template. Returns False on failure. Never raises."""
    try:
        result = wa_sender.send_template(
            account,
            template,
            recipient.params or [],
            recipient.mobile_no,
        )
    except Exception as exc:                      # defensive — _post_message shouldn't raise
        recipient.status = "failed"
        recipient.error_message = str(exc)[:500]
        campaign.failed = (campaign.failed or 0) + 1
        log_error("Broadcast send raised", f"campaign={campaign.id} to={recipient.mobile_no}", exc)
        return False

    if result.ok:
        recipient.status  = "sent"
        recipient.wamid   = result.meta_message_id
        recipient.sent_at = datetime.now(timezone.utc)
        campaign.sent = (campaign.sent or 0) + 1
        return True

    err = result.error or ""
    recipient.status        = "failed"
    recipient.error_message = err[:500]
    # Meta returns the code inside the error text; surface it so insights can group by it.
    recipient.error_code    = _error_code(err)
    campaign.failed = (campaign.failed or 0) + 1
    logger.warning(
        "Broadcast send failed campaign=%s to=%s error=%s",
        campaign.id, recipient.mobile_no, err[:200],
    )
    return False


def _error_code(err: str) -> str | None:
    """Pull a Meta numeric error code out of the error text, if present."""
    import re
    m = re.search(r"\b(1\d{5})\b", err or "")
    return m.group(1) if m else None


def _finish(db, campaign) -> None:
    """
    Mark the campaign dispatched — every send attempted.

    Not "settled": delivery receipts keep arriving afterwards and will move the
    delivered/read/failed counters. Conflating the two would report every campaign as a
    success the moment the last send left.

    The guard lives here rather than at the call sites so it cannot be bypassed: two
    runners (the kick from /send and this scheduler) race, and an empty claim is not
    proof of completion — SKIP LOCKED hides rows the other runner holds, and rows it
    has already claimed are "sending". Marking dispatched while work is outstanding
    reports the campaign complete while messages are still going out.
    """
    outstanding = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status.in_(("pending", "sending")),
        )
        .count()
    )
    if outstanding:
        logger.debug(
            "Campaign %s not finished: %d recipient(s) still outstanding",
            campaign.id, outstanding,
        )
        return

    campaign.status = "dispatched"
    campaign.dispatched_at = campaign.dispatched_at or datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "Campaign %s dispatched: %d sent, %d failed, %d skipped",
        campaign.id, campaign.sent or 0, campaign.failed or 0, campaign.skipped or 0,
    )
