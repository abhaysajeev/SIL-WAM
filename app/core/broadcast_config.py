"""
Reading a company's broadcast tuning.

One accessor, so no caller has to decide what to do when a company has no settings row.
`get()` always returns something usable: the company's row if it exists, otherwise a
frozen object carrying the same defaults the table declares.

That fallback is a safety net, not a configuration tier. The migration backfills every
existing company and company creation seeds a row, so in practice it never fires. It
exists because the alternative — a company unable to send because nobody visited a
settings page — is a far worse failure than silently using the defaults it would have
been given anyway.

Values are read once per batch rather than per recipient. A settings change therefore
takes effect on the next batch, which is the right granularity: applying a new
concurrency mid-batch would mean resizing a thread pool that is already running.
"""
from dataclasses import dataclass

from app.core.phone import is_supported_region
from app.models.broadcast_settings import BroadcastSettings


@dataclass(frozen=True)
class BroadcastTuning:
    """A snapshot of one company's settings, safe to read from worker threads."""
    agent_max_recipients_per_send: int = 100
    agent_daily_message_limit:     int = 0      # 0 = unlimited
    agent_max_contacts_per_phonebook: int = 2000
    send_concurrency:              int = 10
    batch_size:                    int = 20
    max_retries:                   int = 2
    retry_backoff_seconds:         int = 30
    receipt_timeout_minutes:       int = 15
    stale_claim_minutes:           int = 5
    # ISO alpha-2 country a number without a country code is read in (app/core/phone.py).
    default_country:               str = "IN"


# Module-level so the fallback is allocated once and is obviously identical to the
# column defaults above it.
DEFAULTS = BroadcastTuning()


def get(db, company_id) -> BroadcastTuning:
    """
    This company's tuning, or the defaults if it has no row.

    Returns a detached snapshot rather than the ORM object on purpose: the dispatch
    loop hands these values to worker threads, and a live ORM instance carries a
    Session reference that is not safe to touch from another thread.
    """
    row = (
        db.query(BroadcastSettings)
        .filter(BroadcastSettings.company_id == company_id)
        .first()
    )
    if row is None:
        return DEFAULTS

    return BroadcastTuning(
        agent_max_recipients_per_send=_at_least(row.agent_max_recipients_per_send, 0),
        agent_daily_message_limit=_at_least(row.agent_daily_message_limit, 0),
        agent_max_contacts_per_phonebook=_clamp(row.agent_max_contacts_per_phonebook, 1, 10000),
        # A concurrency of 0 would dispatch nothing at all and read as a hung campaign.
        # Clamped rather than validated away, so a bad value degrades to "serial" —
        # the behaviour before this setting existed — instead of stalling the queue.
        send_concurrency=_clamp(row.send_concurrency, 1, 50),
        batch_size=_clamp(row.batch_size, 1, 500),
        max_retries=_clamp(row.max_retries, 0, 10),
        retry_backoff_seconds=_clamp(row.retry_backoff_seconds, 1, 3600),
        receipt_timeout_minutes=_clamp(row.receipt_timeout_minutes, 1, 1440),
        # Must stay above wa_sender's 20s HTTP timeout, or recovery reclaims a send
        # that is still in flight and the recipient is messaged twice.
        stale_claim_minutes=_clamp(row.stale_claim_minutes, 1, 120),
        # An unsupported code degrades to India rather than making every number in
        # the company unreadable.
        default_country=(row.default_country or "IN").upper()
        if is_supported_region(row.default_country) else "IN",
    )


def _clamp(value, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _at_least(value, low: int) -> int:
    return max(low, int(value))
