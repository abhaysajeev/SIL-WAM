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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, or_, text

from app.core import broadcast_config, template_body
from app.core.database import SessionLocal
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.messaging import MessagingOptOut
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import agent_timeline, wa_sender
from app.services.broadcast_screening import SKIP_OPTED_OUT
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="UTC")

# Both of these are now per-company settings (app/core/broadcast_config.py). They
# survive as the fallback used when a campaign has no company settings row, and are
# deliberately the same values that were compiled in before that table existed.
_BATCH_LIMIT = broadcast_config.DEFAULTS.batch_size

# How long a row may sit in "sending" before recovery treats it as stranded.
# Must comfortably exceed the worst-case single send (wa_sender uses a 20s HTTP
# timeout) so a slow-but-live send is never reclaimed underneath itself.
_CLAIM_STALE_AFTER = timedelta(minutes=broadcast_config.DEFAULTS.stale_claim_minutes)

# The scheduler polls on one job for the whole install, so this cannot be per-company
# the way everything else here is — a per-company interval could not be honoured and
# would be a setting that silently does nothing.
_POLL_SECONDS = 3


@dataclass(frozen=True)
class _Creds:
    """
    The only two account fields a send needs, copied out of the ORM.

    Worker threads must never touch a mapped object. Session.commit() expires every
    instance it manages, so `account.phone_number_id` after a commit is not a field
    read — it is a lazy SELECT on a Session that belongs to another thread. Copying
    the two values out on the main thread makes that impossible rather than unlikely.
    """
    phone_number_id: str
    access_token_encrypted: str


@dataclass(frozen=True)
class _Tpl:
    """Likewise for the template: send_template reads only these two."""
    name: str
    language: str


@dataclass(frozen=True)
class _Job:
    """One send, fully resolved on the main thread before any worker sees it."""
    recipient_id: object
    mobile_no: str
    params: list
    header_media: dict | None
    attempts: int
    # For the agent chat timeline. Rendered here rather than at write time so the
    # substitution happens once, on the main thread, from the same components the
    # send uses — a bubble cannot then disagree with what Meta was given.
    customer_name: str | None = None
    body_rendered: str | None = None
    # The agent who created this campaign from the app (agents.id), or None for a web
    # panel campaign. Makes them a participant of each recipient's chat thread, which is
    # how that customer's reply reaches their device.
    sent_by_agent_id: object = None


# ── Public: called from main.py lifespan ────────────────────────────────────

def start() -> None:
    _run_reclaim_job()
    scheduler.add_job(
        _run_broadcast_job,
        trigger="interval",
        seconds=_POLL_SECONDS,
        id="broadcast_send",
        replace_existing=True,
        max_instances=1,
    )
    # Recovery also runs periodically, not only at boot. A row claimed moments before
    # a crash is not yet stale when the process restarts, so a boot-only sweep would
    # strand it in "sending" forever.
    scheduler.add_job(
        _run_reclaim_job,
        trigger="interval",
        minutes=1,
        id="broadcast_reclaim",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Broadcast scheduler started (send=%ds, reclaim=60s)", _POLL_SECONDS)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Broadcast scheduler stopped")


def _run_reclaim_job() -> None:
    """Wrapped so a crash never kills the scheduler thread."""
    db = SessionLocal()
    try:
        reclaim_stale(db)
        sweep_unconfirmed(db)
    except Exception as exc:
        db.rollback()
        log_error("Broadcast recovery failed", "broadcast_scheduler._run_reclaim_job", exc)
    finally:
        db.close()


def reclaim_stale(db) -> int:
    """
    Reset rows stranded in "sending" by a crash or restart.

    "sending" means some worker claimed the row but never recorded an outcome.
    Resetting to "pending" is safe *because* status is per-recipient: a row that was
    actually sent is already "sent" and is never revisited. A batch-level cursor could
    not make that distinction and would have to fail the whole run instead.

    Only rows whose claim has gone stale are reset. Age is the only safe discriminator
    here: a second instance booting while this one is mid-batch cannot tell "abandoned
    by a dead process" from "in flight elsewhere" any other way, and resetting the
    latter sends those recipients the campaign twice. Rows predating the claimed_at
    column carry NULL and are treated as stale, which is correct — they can only be
    left over from a process that is no longer running.

    The cost of the age window is that recovery after a crash is delayed by up to
    _CLAIM_STALE_AFTER for the last batch claimed. That is the right trade against
    double-sending to real customers.
    """
    # A stranded row that already carries a wamid was accepted by Meta — the send
    # happened and only the bookkeeping was lost. Resetting it to "pending" would
    # send that customer the campaign a second time, so resolve it to "sent" instead.
    # Per-recipient commits make this window very small, but it is the difference
    # between recovery and a duplicate, so it is checked first and separately.
    resolved = db.execute(text("""
        UPDATE broadcast_recipients r
        SET status = 'sent', claimed_at = NULL,
            sent_at = COALESCE(sent_at, now())
        FROM broadcast_campaigns c
        LEFT JOIN broadcast_settings s ON s.company_id = c.company_id
        WHERE r.campaign_id = c.id
          AND r.status = 'sending'
          AND r.wamid IS NOT NULL
          AND (r.claimed_at IS NULL
               OR r.claimed_at < now() - make_interval(
                    mins => COALESCE(s.stale_claim_minutes, :default_stale)))
    """), {"default_stale": broadcast_config.DEFAULTS.stale_claim_minutes}).rowcount

    # Everything else stranded goes back to the queue. The staleness window is
    # per-company because the send timeout it must exceed is too.
    n = db.execute(text("""
        UPDATE broadcast_recipients r
        SET status = 'pending', claimed_at = NULL
        FROM broadcast_campaigns c
        LEFT JOIN broadcast_settings s ON s.company_id = c.company_id
        WHERE r.campaign_id = c.id
          AND r.status = 'sending'
          AND r.wamid IS NULL
          AND (r.claimed_at IS NULL
               OR r.claimed_at < now() - make_interval(
                    mins => COALESCE(s.stale_claim_minutes, :default_stale)))
    """), {"default_stale": broadcast_config.DEFAULTS.stale_claim_minutes}).rowcount

    db.commit()
    if resolved:
        logger.warning(
            "Resolved %d stranded recipient(s) that Meta had already accepted", resolved)
    if n:
        logger.warning("Reclaimed %d stale broadcast recipient(s) to pending", n)
    return n


def sweep_unconfirmed(db) -> int:
    """
    Retire rows that were accepted by Meta but never confirmed by a receipt.

    "sent" means Meta took the request. It is not evidence the message reached a
    phone, and a row that sits in that state indefinitely quietly turns into a claim
    of delivery that nobody ever made — the campaign looks like it is still settling
    weeks later. After the company's receipt_timeout_minutes the row becomes
    "unconfirmed": the system stating plainly that it does not know.

    This is not failure. Receipts can be late, and a late one still wins —
    broadcast_status ranks "unconfirmed" level with "sent", so delivered/read
    continue to overwrite it whenever they turn up.
    """
    n = db.execute(text("""
        UPDATE broadcast_recipients r
        SET status = 'unconfirmed'
        FROM broadcast_campaigns c
        LEFT JOIN broadcast_settings s ON s.company_id = c.company_id
        WHERE r.campaign_id = c.id
          AND r.status = 'sent'
          AND r.sent_at IS NOT NULL
          AND r.sent_at < now() - make_interval(
                mins => COALESCE(s.receipt_timeout_minutes, :default_timeout))
    """), {"default_timeout": broadcast_config.DEFAULTS.receipt_timeout_minutes}).rowcount
    db.commit()
    if n:
        logger.info("Marked %d recipient(s) unconfirmed — no receipt within the timeout", n)
    return n


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
            .update({"status": "failed", "claimed_at": None, "error_message": reason},
                    synchronize_session=False)
        )
        campaign.failed = (campaign.failed or 0) + stuck
        db.commit()
        _finish(db, campaign)
        return True

    tuning = broadcast_config.get(db, campaign.company_id)

    # Claim a batch. SKIP LOCKED means a second process would take different rows
    # rather than blocking or double-sending.
    batch = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status == "pending",
            # A row waiting out its retry backoff is still "pending" but not yet
            # eligible. NULL means never deferred, which is every row on its first
            # attempt and every row that predates retries.
            or_(
                BroadcastRecipient.next_attempt_at.is_(None),
                BroadcastRecipient.next_attempt_at <= datetime.now(timezone.utc),
            ),
        )
        .order_by(BroadcastRecipient.id)
        .with_for_update(skip_locked=True)
        .limit(tuning.batch_size)
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

    # Enforcement point 3 (BROADCAST_DESIGN §9) — the one the design warns gets missed.
    # broadcast_screening runs once, before dispatch. A campaign then takes minutes to
    # drain, and anyone who texts STOP during that window was screened as sendable and
    # would still be messaged. Re-check here, against exactly the rows about to go out,
    # so the gap between the decision and the send is a single batch rather than the
    # whole campaign.
    opted_out = {
        m for (m,) in db.query(MessagingOptOut.mobile_no).filter(
            MessagingOptOut.company_id == campaign.company_id,
            MessagingOptOut.mobile_no.in_([r.mobile_no for r in batch]),
        )
    }

    claimed_at = datetime.now(timezone.utc)
    sendable = []
    for r in batch:
        if r.mobile_no in opted_out:
            r.status, r.skip_reason = "skipped", SKIP_OPTED_OUT
            campaign.skipped = (campaign.skipped or 0) + 1
            continue
        r.status = "sending"
        r.claimed_at = claimed_at
        sendable.append(r)
    db.commit()

    if len(sendable) != len(batch):
        logger.info(
            "Campaign %s: skipped %d recipient(s) who opted out after screening",
            campaign.id, len(batch) - len(sendable),
        )

    # Everything a worker needs, resolved here so no mapped object crosses a thread
    # boundary. campaign/account/template are expired by the commit above; reading
    # them now refreshes them on this thread, which is safe, and the copies below
    # are then plain values.
    creds = _Creds(account.phone_number_id, account.access_token_encrypted)
    tpl   = _Tpl(template.name, template.language)
    components = template.components
    # Resolved once per batch, on this thread: the creating user's agent row, if any.
    from app.models.agent import Agent
    sender_agent_id = (
        db.query(Agent.id).filter(Agent.user_id == campaign.created_by_id).scalar()
        if campaign.created_by_id else None
    )
    jobs  = [
        _Job(
            recipient_id=r.id,
            mobile_no=r.mobile_no,
            params=list(r.params or []),
            header_media=_header_media_for(campaign, r, template),
            attempts=r.attempts or 0,
            customer_name=r.customer_name,
            body_rendered=template_body.render(components, list(r.params or [])),
            sent_by_agent_id=sender_agent_id,
        )
        for r in sendable
    ]
    campaign_id = campaign.id
    company_id = campaign.company_id
    stop_on_error = bool(campaign.stop_on_error)

    stop_requested = _run_jobs(db, campaign_id, company_id, tuning, creds, tpl,
                               jobs, stop_on_error)

    if stop_requested:
        # Rows never attempted go back to the queue untouched.
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == campaign_id,
            BroadcastRecipient.status == "sending",
        ).update({"status": "pending", "claimed_at": None}, synchronize_session=False)
        db.commit()

    db.refresh(campaign)

    if stop_requested:
        campaign.status = "dispatched"
        campaign.dispatched_at = campaign.dispatched_at or datetime.now(timezone.utc)
        db.commit()
        logger.warning("Campaign %s halted on first error (stop_on_error)", campaign.id)
        return True

    return True


def _header_media_for(campaign, recipient, template) -> dict | None:
    """
    Build wa_sender's header payload for one send. Two shapes, by campaign mode.

    **shared** — one file, addressed by media id. The user's upload, or the link they
    pasted, was uploaded to Meta once at attach time, so the campaign is one file on
    Meta's servers referenced N times rather than N fetches of an address that only
    has to fail once to cost a customer their message.

    **per_row** — this recipient's own URL, which Meta fetches. Correct here and not
    a repeat of the mistake shared mode avoids: each row is one message, so each
    address is fetched exactly once — the same single request downloading it
    ourselves would have cost. Reachability was already checked at import, so a URL
    reaching this point has been seen to work.

    Stored formats are Meta's upper-case ones ("IMAGE"); the send payload wants the
    lower-case component type ("image").
    """
    if campaign.header_media_mode == "per_row":
        url = getattr(recipient, "header_media_url", None)
        if not url:
            return None
        fmt = template_body.header_format(template.components if template else None)
        return {"format": str(fmt or "IMAGE").lower(), "link": url}

    hm = campaign.header_media or None
    if not hm or not hm.get("media_id"):
        return None
    out = {
        "format":   str(hm.get("format", "IMAGE")).lower(),
        "media_id": hm["media_id"],
        "filename": hm.get("filename"),
    }
    # Our stored copy (agent uploads). Carried into the timeline's media JSON so the
    # chat bubble can be opened via GET /agent-api/v1/media/{asset_id}; wa_sender
    # ignores keys it does not use.
    if hm.get("asset_id"):
        out["asset_id"] = hm["asset_id"]
    return out


# Meta error codes that mean "this exact request will fail again". Retrying them
# wastes the number's daily quota and, for 131026, signals bad list hygiene to Meta —
# which is one of the inputs to the quality rating that governs delivery for every
# other campaign on the number.
_PERMANENT_CODES = {
    "131026",   # not a WhatsApp user / cannot receive
    "131042",   # business eligibility — payment issue on the WABA (fixed in Meta, not by retrying)
    "131050",   # user stopped marketing messages from this business
    "131047",   # outside the 24-hour window
    "131049",   # per-user marketing frequency cap
    "132000",   # template parameter count mismatch
    "132001",   # template does not exist in this language
    "132005",   # template text has changed since approval
    "132012",   # parameter format mismatch
    "133010",   # phone number not registered
}


def _run_jobs(db, campaign_id, company_id, tuning, creds, tpl, jobs, stop_on_error) -> bool:
    """
    Run one batch of sends and record every outcome. Returns True if the run halted.

    **Each outcome is committed on its own.** The old loop shared a single commit
    across the whole batch, so a restart mid-batch left rows that Meta had already
    accepted sitting in "sending" with no wamid — recovery then returned them to
    "pending" and those customers were messaged twice. Committing per result narrows
    that window to a single in-flight send.

    A window remains, and it is irreducible without an idempotency key Meta does not
    offer: a crash between Meta accepting a send and our commit landing loses the
    wamid entirely, and nothing downstream can distinguish that from a send that never
    happened. reclaim_stale resolves the rows where the wamid did land; this is the
    honest limit of the rest.
    """
    # stop_on_error is a request to halt before the next send goes out. That is a
    # serial promise, so it forces serial execution — you cannot both have ten sends
    # in flight and stop before the second. The flag is opt-in and rare; a campaign
    # that wants throughput simply does not set it.
    concurrency = 1 if stop_on_error else min(tuning.send_concurrency, len(jobs))

    if concurrency <= 1:
        for job in jobs:
            result = _call_meta(creds, tpl, job)
            ok = _apply_result(db, campaign_id, company_id, tuning, tpl, job, result)
            if not ok and stop_on_error:
                logger.warning("Campaign %s halted on first error (stop_on_error)", campaign_id)
                return True
        return False

    # Results are applied on this thread, in completion order, so all database
    # writes stay single-threaded. See _call_meta on why the workers themselves do
    # not hold a connection.
    with ThreadPoolExecutor(max_workers=concurrency,
                            thread_name_prefix="bcast") as pool:
        futures = {pool.submit(_call_meta, creds, tpl, job): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            _apply_result(db, campaign_id, company_id, tuning, tpl, job, fut.result())
    return False


def _call_meta(creds, tpl, job: _Job):
    """
    The network call, and nothing else. Runs on a worker thread.

    Takes plain values and returns a SendResult; every write to a broadcast row
    happens on the caller's thread. On the normal path this touches no Session at
    all, which is why concurrency here costs threads rather than connections from
    the pool the other three schedulers share.

    The one exception is the defensive path below: log_error opens its own
    short-lived session, as it does everywhere. That is thread-safe — each thread
    gets its own connection — and bounded by send_concurrency even if every worker
    fails at once, which is comfortably inside the pool's ceiling of 30.
    """
    started = time.monotonic()
    try:
        result = wa_sender.send_template(
            creds, tpl, job.params, job.mobile_no, header_media=job.header_media,
        )
    except Exception as exc:                  # defensive — send_template shouldn't raise
        log_error("Broadcast send raised", f"to={job.mobile_no}", exc)
        return wa_sender.SendResult(
            ok=False, meta_message_id=None,
            error=str(exc)[:500], is_transient=False,
        )

    # Almost all of a broadcast's wall-clock time is this one call. Logged so
    # "the send felt slow" is answerable from the record rather than re-measured.
    rtt = time.monotonic() - started
    if rtt > 5:
        logger.warning("Meta send took %.2fs for %s", rtt, job.mobile_no)
    else:
        logger.info("Meta send %.2fs for %s", rtt, job.mobile_no)
    return result


def _apply_result(db, campaign_id, company_id, tuning, tpl, job: _Job, result) -> bool:
    """
    Record one outcome and commit it. Returns False if the send failed.

    Writes by primary key rather than through the mapped instance: the instances were
    expired by the claim commit, so touching one would re-SELECT it, and there is
    nothing on the row worth re-reading. Campaign counters use SQL increments for the
    same reason and because concurrent workers would otherwise lose counts to a
    read-modify-write race.

    "Sent" here means Meta accepted the request and returned a message id. It says
    nothing about the customer's phone — that only arrives later as a delivery
    receipt, and sweep_unconfirmed retires the rows where it never does.
    """
    attempts = job.attempts + 1
    row = db.query(BroadcastRecipient).filter(BroadcastRecipient.id == job.recipient_id)

    if result.ok:
        row.update({
            "status":      "sent",
            "claimed_at":  None,
            "wamid":       result.meta_message_id,
            "sent_at":     datetime.now(timezone.utc),
            "attempts":    attempts,
            # Clear the previous attempt's error. A row that failed transiently and
            # then succeeded would otherwise keep that message, and the campaign page
            # renders it underneath the pill — an error printed under a successful
            # send, describing something that no longer happened.
            "error_code":    None,
            "error_message": None,
            "next_attempt_at": None,
        }, synchronize_session=False)
        _bump(db, campaign_id, "sent")

        # The customer's own record of this message, for the agent chat window.
        # Broadcast writes no Message row (BROADCAST_DESIGN.md §8), so without this
        # the outbound half of a contact's history exists nowhere the window can read.
        # In the same transaction as the recipient update, so a bubble can never
        # describe a send that rolled back.
        agent_timeline.record_outbound(
            db, company_id, job.mobile_no,
            kind="broadcast",
            body_rendered=job.body_rendered,
            template_meta={
                "name":     tpl.name,
                "language": tpl.language,
                "params":   job.params,
            },
            media=job.header_media,
            wamid=result.meta_message_id,
            status="sent",
            campaign_id=campaign_id,
            sent_by_agent_id=job.sent_by_agent_id,
            contact_name=job.customer_name,
        )

        db.commit()
        return True

    err  = result.error or ""
    code = _error_code(err)

    if _should_retry(result, code, attempts, tuning):
        # Exponential backoff from the first retry: 30s, 60s, 120s at the default.
        delay = tuning.retry_backoff_seconds * (2 ** (attempts - 1))
        row.update({
            "status":          "pending",
            "claimed_at":      None,
            "attempts":        attempts,
            "next_attempt_at": datetime.now(timezone.utc) + timedelta(seconds=delay),
            "error_message":   err[:500],
            "error_code":      code,
        }, synchronize_session=False)
        db.commit()
        logger.info(
            "Broadcast send to %s failed transiently (attempt %d/%d), retrying in %ds: %s",
            job.mobile_no, attempts, tuning.max_retries + 1, delay, err[:200],
        )
        return False

    row.update({
        "status":        "failed",
        "claimed_at":    None,
        "attempts":      attempts,
        "error_message": err[:500],
        # Meta returns the code inside the error text; surface it so insights can
        # group by it.
        "error_code":    code,
    }, synchronize_session=False)
    _bump(db, campaign_id, "failed")
    db.commit()
    logger.warning(
        "Broadcast send failed campaign=%s to=%s error=%s",
        campaign_id, job.mobile_no, err[:200],
    )
    return False


def _should_retry(result, code, attempts, tuning) -> bool:
    """
    Only a failure that could plausibly succeed on a second attempt.

    Two independent gates, and both must open. `is_transient` is decided in wa_sender
    at the point the exception type and HTTP status still exist — a timeout or a 5xx,
    never a rejection. The code check then vetoes anything Meta has told us is about
    the request itself, which would fail identically however often it is repeated.
    """
    if attempts > tuning.max_retries:
        return False
    if code in _PERMANENT_CODES:
        return False
    return bool(getattr(result, "is_transient", False))


def _bump(db, campaign_id, column: str) -> None:
    """
    Increment a campaign counter in SQL.

    `campaign.sent = campaign.sent + 1` in Python is a read-modify-write, and with
    concurrent workers two increments landing together lose one. These counters are
    advisory — campaign_progress derives its numbers from the recipient rows — but a
    counter that drifts is still a counter somebody will eventually believe.
    """
    col = getattr(BroadcastCampaign, column)
    db.query(BroadcastCampaign).filter(BroadcastCampaign.id == campaign_id).update(
        {column: func.coalesce(col, 0) + 1}, synchronize_session=False,
    )


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
