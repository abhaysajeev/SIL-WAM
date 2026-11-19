"""
GET /agent-api/v1/limits — every ceiling that applies to this agent, in one call.

So the app can show a limit BEFORE the agent hits it: "This broadcast would reach 240
people; your limit is 100" on the confirm screen, not as an error after pressing Send.
Every value is also enforced server-side; this is the courtesy copy.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.broadcasts import MAX_PHONEBOOKS
from app.agent.deps import AgentContext, get_agent_session
from app.agent.messages import _daily_count
from app.core import broadcast_config
from app.core.database import get_db
from app.services import account_health, contact_import, media_assets

router = APIRouter(prefix="/limits", tags=["Agent App"])


class MediaRule(BaseModel):
    format:    str
    mimes:     list[str]
    max_bytes: int


class AccountStatus(BaseModel):
    # ok | warning (a problem was detected, sending still allowed) | blocked | not_connected
    state:  str
    reason: str | None
    # Meta's messaging tier for the company's number, e.g. TIER_1K, when known.
    messaging_limit_tier: str | None


class LimitsOut(BaseModel):
    # One broadcast may reach at most this many people (after duplicates and opt-outs
    # are removed). 0 = no limit.
    max_recipients_per_send:    int
    # Rolling 24 hours, everything this agent sends. 0 = no limit.
    daily_message_limit:        int
    sent_last_24h:              int
    max_phonebooks_per_broadcast: int
    # Contacts in one phonebook the agent builds (create, add, import).
    max_contacts_per_phonebook: int
    # Phonebook file import.
    import_max_file_bytes:      int
    import_formats:             list[str]
    # Broadcast header files.
    media:                      list[MediaRule]
    # Numbers without a country code are read as numbers in this country.
    default_country:            str
    account:                    AccountStatus
    server_time:                datetime


@router.get("", response_model=LimitsOut)
def get_limits(
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    if not ctx.company_id:
        raise errors.not_an_agent()
    t = broadcast_config.get(db, ctx.company_id)
    return LimitsOut(
        max_recipients_per_send=t.agent_max_recipients_per_send,
        daily_message_limit=t.agent_daily_message_limit,
        sent_last_24h=_daily_count(db, ctx.agent_row_id),
        max_phonebooks_per_broadcast=MAX_PHONEBOOKS,
        max_contacts_per_phonebook=t.agent_max_contacts_per_phonebook,
        import_max_file_bytes=contact_import.MAX_FILE_BYTES,
        import_formats=list(contact_import.FORMATS),
        media=[MediaRule(**r) for r in media_assets.limits()],
        default_country=t.default_country,
        account=AccountStatus(**account_health.status_for(db, ctx.company_id)),
        server_time=datetime.now(timezone.utc),
    )
