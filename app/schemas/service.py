"""Pydantic schemas for the service flow engine."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
import re

_E164_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


def _validate_mobile(v: str) -> str:
    cleaned = v.strip()
    if not _E164_RE.match(cleaned):
        raise ValueError(
            f"customer_mobile '{cleaned}' is not a valid phone number "
            "(must be 7–15 digits, optional leading +, no spaces or dashes)"
        )
    return cleaned


# ── Inbound: client places a service request ──────────────────────────────────

class ServiceCreate(BaseModel):
    """
    Posted by a client (Type B) via X-API-Key to trigger a WhatsApp service flow.

    Fixed envelope fields are top-level.
    Everything the client owns goes inside `data`.
    `questions` is optional — client can send inline or rely on stored profile (future).
    """
    service_id:            str               # client's globally-unique reference
    template_id:           uuid.UUID         # which WA template to send first
    template_expiry_hours: int = 24

    data: dict[str, Any]                     # opaque blob — stored as-is, never interpreted
    # data must contain: customer_mobile (str)
    # data may contain: questions list (see architecture doc §9.1)


class ServiceIngestRequest(BaseModel):
    """Full client ingest payload — replaces ServiceCreate for the client API."""
    service_id:            str
    template_name:         str               # template name as shown in Meta (e.g. "order_confirm_sa")
    template_expiry_hours: int = 24
    template_params:       list[str] = []
    cta_urls:              dict[str, str] | None = None
    data:                  dict[str, Any]   # must contain customer_mobile; may contain questions

    @field_validator("data")
    @classmethod
    def validate_customer_mobile(cls, v: dict) -> dict:
        mobile = v.get("customer_mobile")
        if not mobile:
            raise ValueError("data.customer_mobile is required")
        _validate_mobile(str(mobile))
        return v


class ServiceRetryRequest(BaseModel):
    customer_mobile: str

    @field_validator("customer_mobile")
    @classmethod
    def validate_mobile(cls, v: str) -> str:
        return _validate_mobile(v)


class ServiceIngestResponse(BaseModel):
    service_id:   str
    reference_id: uuid.UUID  # our internal Service.id — for support/log lookups
    status:       str        # "in_progress" | "waiting"


# ── Outbound: API responses ───────────────────────────────────────────────────

class ServiceResponseItem(BaseModel):
    sequence:       int
    field_key:      str
    question:       str
    answer_type:    int
    response_value: str | None
    responded_at:   datetime | None

    model_config = ConfigDict(from_attributes=True)


class ServiceOut(BaseModel):
    id:                    uuid.UUID
    service_id:            str
    company_id:            uuid.UUID
    status:                str
    expired_reason:        str | None
    failed_reason:         str | None
    data:                  dict[str, Any] | None
    questions:             list[dict] | None
    template_expiry_hours: int
    created_at:            datetime
    completed_at:          datetime | None

    model_config = ConfigDict(from_attributes=True)


class ServiceDetail(ServiceOut):
    """Full detail including Q&A responses — returned by the client GET API."""
    responses: list[ServiceResponseItem] = []


class ServiceGetResponse(BaseModel):
    """Response for client GET /client-api/v1/services/{service_id}."""
    service_id:          str
    status:              str
    failed_reason:       str | None
    expired_reason:      str | None
    completed_questions: int
    total_questions:     int
    data:                dict[str, Any] | None
    responses:           list[ServiceResponseItem]
    created_at:          datetime
    completed_at:        datetime | None

    model_config = ConfigDict(from_attributes=True)


# ── Conversation ──────────────────────────────────────────────────────────────

class ConversationOut(BaseModel):
    id:               uuid.UUID
    company_id:       uuid.UUID
    mobile_no:        str
    first_contact_at: datetime
    last_activity_at: datetime
    total_messages:   int

    model_config = ConfigDict(from_attributes=True)


# ── Message ───────────────────────────────────────────────────────────────────

class MessageOut(BaseModel):
    id:              uuid.UUID
    conversation_id: uuid.UUID
    service_id:      uuid.UUID | None
    wamid:           str | None
    direction:       str
    message_type:    str
    content:         dict | None
    is_flow_message: bool
    status:          str | None
    sent_at:         datetime | None
    delivered_at:    datetime | None
    read_at:         datetime | None
    created_at:      datetime

    model_config = ConfigDict(from_attributes=True)
