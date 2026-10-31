"""
One place to build a Jinja2Templates instance, with every filter already registered.

Before this module, fourteen route files each constructed their own
`Jinja2Templates(...)` and each had to remember to attach `dv_search_text` — a
duplication CLAUDE.md already calls out as a gotcha. Adding a second filter would have
meant fourteen more edits and a fifteenth thing to forget in the next route file.

Route files now call `build_templates()` instead. A filter added here reaches every page
at once.

## Timezones

Everything is stored in UTC — Postgres runs on `Etc/UTC`, and the models use
`DateTime(timezone=True)` with `func.now()`. That is correct and must stay: the message
pipeline compares timestamps across Meta webhooks, schedulers and the agent app, and a
localised database makes those comparisons quietly wrong.

But a UTC timestamp is not what a person in the office should read. `.strftime()` on an
aware UTC datetime prints UTC, so every page that formatted a timestamp server-side was
showing a time five and a half hours behind the wall clock.

`settings.DISPLAY_TIMEZONE` is the single place that is configured, and the `dt` /
`dt_date` / `dt_time` filters are the only correct way to render a timestamp in a
template. Storage stays UTC; display converts.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.core.config import settings
from app.core.pagination import page_url

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)

# Shown where a value is missing, matching what the pages already printed.
EMPTY = "—"

DATETIME_FMT = "%d %b %Y  %H:%M"
DATE_FMT     = "%d %b %Y"
TIME_FMT     = "%H:%M"


def display_tz() -> ZoneInfo:
    """
    The timezone the UI renders in.

    Falls back to UTC rather than raising if the configured zone is unknown — a typo in
    an env var should show the wrong hour, not take every page down with a 500.
    """
    try:
        return ZoneInfo(settings.DISPLAY_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_local(value: datetime | None) -> datetime | None:
    """
    Move an instant into the display timezone.

    A naive datetime is assumed to be UTC. That assumption is safe here because every
    column is `DateTime(timezone=True)` and every write goes through `func.now()` or an
    aware `datetime.now(timezone.utc)` — naive values only appear via drivers that drop
    the offset on the way back.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(display_tz())


def fmt_dt(value: datetime | None, fmt: str = DATETIME_FMT, empty: str = EMPTY) -> str:
    """Format an instant for display. Use this instead of calling .strftime() directly."""
    local = to_local(value)
    return local.strftime(fmt) if local else empty


def fmt_date(value: datetime | None, empty: str = EMPTY) -> str:
    return fmt_dt(value, DATE_FMT, empty)


def fmt_time(value: datetime | None, empty: str = EMPTY) -> str:
    return fmt_dt(value, TIME_FMT, empty)


def ts(value: datetime | None, fmt: str = "datetime", empty: str = EMPTY) -> Markup:
    """
    Render an instant as a <time> tag the browser localises.

    This is how every timestamp on a page should be printed. Not because the server
    cannot format one, but because it cannot know *whose* timezone to format it in:
    SIL staff in India and a client in Dubai can be reading the same page at the same
    moment, and only the browser knows the answer for each of them.

    Storage and the JSON APIs stay UTC — the same rule Meta, Gupshup and Twilio follow.
    Meta's own webhooks hand us a bare Unix timestamp with no zone at all. Conversion
    belongs at the very edge, and the browser is the edge.

    The text inside the tag is a server-rendered fallback in settings.DISPLAY_TIMEZONE,
    so the page is still readable with JavaScript off, or in the instant before the
    formatter runs. base.html rewrites it on load.

        {{ ts(user.created_at) }}            02 Sep 2026  12:02
        {{ ts(pb.created_at, "date") }}      02 Sep 2026
        {{ ts(msg.created_at, "time") }}     12:02
    """
    if value is None:
        return Markup(escape(empty))

    instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    iso = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    fallback = {
        "date": fmt_date, "time": fmt_time,
    }.get(fmt, fmt_dt)(value)

    return Markup(
        f'<time datetime="{escape(iso)}" data-fmt="{escape(fmt)}">{escape(fallback)}</time>'
    )


def _dv_search_text(row) -> str:
    """
    Everything in a row, flattened, for the list view's client-side filter box.

    Required by layouts/list_view.html. Registering it here is why route files no longer
    have to remember it individually.
    """
    if hasattr(row, "values"):
        return " ".join(str(v) for v in row.values() if v is not None)
    return str(row)


def build_templates(directory: str | None = None) -> Jinja2Templates:
    """A Jinja2Templates with every shared filter and global already attached."""
    templates = Jinja2Templates(directory=directory or TEMPLATE_DIR)

    templates.env.filters["dv_search_text"] = _dv_search_text
    # Display-timezone aware. `dt` takes an optional strftime format so a page can be
    # specific without reaching for .strftime() and losing the conversion.
    templates.env.filters["dt"]      = fmt_dt
    templates.env.filters["dt_date"] = fmt_date
    templates.env.filters["dt_time"] = fmt_time

    # The correct way to print a timestamp on a page — see ts() above.
    templates.env.globals["ts"]           = ts
    templates.env.globals["page_url"]     = page_url
    templates.env.globals["display_tz"]   = settings.DISPLAY_TIMEZONE
    # For a page that wants to label a column, e.g. "Created (IST)".
    templates.env.globals["tz_abbrev"]    = datetime.now(display_tz()).strftime("%Z")

    return templates
