"""
Pre-send screening for a broadcast campaign.

Runs over a campaign's recipients before the confirmation dialog and marks the ones that
must not be sent, so the user sees the real number before committing rather than
discovering it in the results.

Two categories, kept distinct because they mean different things:

  hard   opted out, US number      never sent, no override
  soft   previously invalid        marked, but sent unless the user excludes them

Deliberately *not* checked here (deferred — needs Graph API fields the platform does not
yet fetch, plus a sliding 24h send counter): messaging tier and remaining daily budget,
quality rating, and health_status.can_send_message. See docs/BROADCAST_DESIGN.md §6.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.broadcast_campaign import BroadcastRecipient
from app.models.messaging import InvalidNumber, MessagingOptOut

logger = logging.getLogger(__name__)

SKIP_OPTED_OUT = "opted_out"
SKIP_US_NUMBER = "us_number"

# Meta does not deliver marketing templates to US numbers, so sending is guaranteed
# waste — it consumes tier budget and returns a failure.
_US_PREFIX = "1"
# North American numbers are 1 + 10 digits. Guarding on length as well as prefix stops
# a legitimate country code that merely starts with 1 (e.g. some 7-digit test numbers)
# being excluded.
_US_LENGTH = 11


@dataclass
class ScreeningResult:
    """What the confirmation dialog renders. Counts, not rows — the table shows rows."""
    sendable:          int = 0
    opted_out:         int = 0
    us_numbers:        int = 0
    previously_invalid: int = 0
    # mobile_no → {"occurrences": n, "last_seen_at": dt, "error_code": str}
    invalid_detail: dict = field(default_factory=dict)

    @property
    def total_skipped(self) -> int:
        return self.opted_out + self.us_numbers

    def as_dict(self) -> dict:
        return {
            "sendable":           self.sendable,
            "opted_out":          self.opted_out,
            "us_numbers":         self.us_numbers,
            "previously_invalid": self.previously_invalid,
            "total_skipped":      self.total_skipped,
            "invalid_detail":     {
                m: {
                    "occurrences":  d["occurrences"],
                    "error_code":   d["error_code"],
                    "last_seen_at": d["last_seen_at"].isoformat() if d["last_seen_at"] else None,
                    "days_ago":     d["days_ago"],
                }
                for m, d in self.invalid_detail.items()
            },
        }


def is_us_number(mobile_no: str) -> bool:
    return bool(mobile_no) and mobile_no.startswith(_US_PREFIX) and len(mobile_no) == _US_LENGTH


def screen(db: Session, campaign) -> ScreeningResult:
    """
    Screen every recipient of `campaign`, writing skip decisions to their rows.

    Idempotent: re-screening resets previously skipped rows first, so a user who opts
    someone back in and screens again gets the right answer rather than a stale one.
    Recipients already sent are never touched.
    """
    recipients = (
        db.query(BroadcastRecipient)
        .filter(
            BroadcastRecipient.campaign_id == campaign.id,
            BroadcastRecipient.status.in_(("draft", "skipped")),
        )
        .all()
    )
    if not recipients:
        return ScreeningResult()

    mobiles = {r.mobile_no for r in recipients}

    opted_out = {
        m for (m,) in db.query(MessagingOptOut.mobile_no).filter(
            MessagingOptOut.company_id == campaign.company_id,
            MessagingOptOut.mobile_no.in_(mobiles),
        )
    }
    invalid_rows = db.query(InvalidNumber).filter(
        InvalidNumber.company_id == campaign.company_id,
        InvalidNumber.mobile_no.in_(mobiles),
    ).all()
    invalid = {row.mobile_no: row for row in invalid_rows}

    now = datetime.now(timezone.utc)
    result = ScreeningResult()

    for r in recipients:
        # Reset — this row may have been skipped by an earlier screening pass.
        r.skip_reason = None

        if r.mobile_no in opted_out:
            r.status, r.skip_reason = "skipped", SKIP_OPTED_OUT
            result.opted_out += 1
            continue

        if is_us_number(r.mobile_no):
            r.status, r.skip_reason = "skipped", SKIP_US_NUMBER
            result.us_numbers += 1
            continue

        # Soft flag: sendable, but surfaced so the user can decide.
        row = invalid.get(r.mobile_no)
        if row is not None:
            seen = row.last_seen_at
            if seen is not None and seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            result.previously_invalid += 1
            result.invalid_detail[r.mobile_no] = {
                "occurrences":  row.occurrences,
                "error_code":   row.error_code,
                "last_seen_at": seen,
                # Age rather than silent expiry — a six-month-old failure is a weaker
                # signal than yesterday's, and the user is better placed to judge.
                "days_ago":     (now - seen).days if seen else None,
            }

        r.status = "draft"
        result.sendable += 1

    campaign.skipped = result.total_skipped
    logger.info(
        "Screened campaign %s: %d sendable, %d opted out, %d US, %d previously invalid",
        campaign.id, result.sendable, result.opted_out, result.us_numbers,
        result.previously_invalid,
    )
    return result
