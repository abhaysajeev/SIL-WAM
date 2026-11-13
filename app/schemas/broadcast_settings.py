"""
Per-company broadcast settings.

Bounds are declared here as well as in app/core/broadcast_config.py, and that
duplication is deliberate rather than an oversight: this layer rejects a bad value at
the edge with a clear message, while the accessor clamps whatever it finds in the
database. Only the second protects a row written before a bound existed, or by hand.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.phone import is_supported_region


def _check_country(v: str) -> str:
    code = (v or "").strip().upper()
    if not is_supported_region(code):
        raise ValueError("Use a two-letter country code, e.g. IN for India or AE for the UAE")
    return code


class BroadcastSettingsCreate(BaseModel):
    company_id: uuid.UUID
    agent_max_recipients_per_send: int = Field(100, ge=0, le=100000)
    agent_daily_message_limit:     int = Field(0,   ge=0, le=100000)
    agent_max_contacts_per_phonebook: int = Field(2000, ge=1, le=10000)
    send_concurrency:              int = Field(10,  ge=1, le=50)
    batch_size:                    int = Field(20,  ge=1, le=500)
    max_retries:                   int = Field(2,   ge=0, le=10)
    retry_backoff_seconds:         int = Field(30,  ge=1, le=3600)
    receipt_timeout_minutes:       int = Field(15,  ge=1, le=1440)
    # Floored at 1 minute because it must exceed wa_sender's 20-second HTTP timeout.
    # A shorter window lets recovery requeue a send that is still in flight, and the
    # customer receives the message twice.
    stale_claim_minutes:           int = Field(5,   ge=1, le=120)
    default_country:               str = "IN"

    @field_validator("default_country")
    @classmethod
    def _country(cls, v: str) -> str:
        return _check_country(v)


class BroadcastSettingsUpdate(BaseModel):
    agent_max_recipients_per_send: int | None = Field(None, ge=0, le=100000)
    agent_daily_message_limit:     int | None = Field(None, ge=0, le=100000)
    agent_max_contacts_per_phonebook: int | None = Field(None, ge=1, le=10000)
    send_concurrency:              int | None = Field(None, ge=1, le=50)
    batch_size:                    int | None = Field(None, ge=1, le=500)
    max_retries:                   int | None = Field(None, ge=0, le=10)
    retry_backoff_seconds:         int | None = Field(None, ge=1, le=3600)
    receipt_timeout_minutes:       int | None = Field(None, ge=1, le=1440)
    stale_claim_minutes:           int | None = Field(None, ge=1, le=120)
    default_country:               str | None = None

    @field_validator("default_country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        return None if v is None else _check_country(v)


class BroadcastSettingsOut(BaseModel):
    id:         uuid.UUID
    company_id: uuid.UUID
    agent_max_recipients_per_send: int
    agent_daily_message_limit:     int
    agent_max_contacts_per_phonebook: int
    send_concurrency:              int
    batch_size:                    int
    max_retries:                   int
    retry_backoff_seconds:         int
    receipt_timeout_minutes:       int
    stale_claim_minutes:           int
    default_country:               str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
