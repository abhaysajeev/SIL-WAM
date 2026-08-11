"""Pydantic schemas for phonebooks and their contacts."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.service import _validate_mobile as _validate_mobile_raw

_MOBILE_HELP = (
    "is not a valid WhatsApp number — use 7 to 15 digits including the country "
    "code, with no spaces or dashes"
)


def _validate_mobile(v: str) -> str:
    """
    Same rule as everywhere else (7–15 digits, '+' stripped), but reworded.

    The shared validator names the field "customer_mobile", which is the client
    ingest API's terminology and means nothing to someone typing into a
    phonebook. The rule is identical — only the message differs.
    """
    try:
        return _validate_mobile_raw(v)
    except ValueError:
        raise ValueError(f"'{(v or '').strip()}' {_MOBILE_HELP}")


# ── Phonebook ─────────────────────────────────────────────────────────────────

class PhonebookCreate(BaseModel):
    name: str
    # Ignored for company-scoped users — the route forces their own company so a
    # crafted payload cannot create a phonebook under someone else's.
    company_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("PhoneBook name is required")
        return v


class PhonebookUpdate(BaseModel):
    name: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("PhoneBook name cannot be blank")
        return v


class PhonebookOut(BaseModel):
    id:            uuid.UUID
    company_id:    uuid.UUID
    name:          str
    created_at:    datetime | None
    contact_count: int = 0

    model_config = ConfigDict(from_attributes=True)


# ── Contact ───────────────────────────────────────────────────────────────────

class ContactCreate(BaseModel):
    mobile_no:     str
    customer_name: str
    email:         str | None = None
    agent_id:      str | None = None

    @field_validator("mobile_no")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        # Same rule as every other number in the system: 7–15 digits, '+' stripped.
        # Storing it any other way breaks opt-out matching and inbound routing.
        return _validate_mobile(v)

    @field_validator("customer_name")
    @classmethod
    def name_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Customer name is required")
        return v

    @field_validator("email", "agent_id")
    @classmethod
    def blank_to_none(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


class ContactUpdate(BaseModel):
    mobile_no:     str | None = None
    customer_name: str | None = None
    email:         str | None = None
    agent_id:      str | None = None

    @field_validator("mobile_no")
    @classmethod
    def validate_mobile(cls, v: str | None) -> str | None:
        return _validate_mobile(v) if v else None

    @field_validator("customer_name")
    @classmethod
    def name_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("Customer name cannot be blank")
        return v

    @field_validator("email", "agent_id")
    @classmethod
    def blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class ContactOut(BaseModel):
    id:            uuid.UUID
    phonebook_id:  uuid.UUID
    mobile_no:     str
    customer_name: str
    email:         str | None
    agent_id:      str | None
    created_at:    datetime | None

    model_config = ConfigDict(from_attributes=True)
