from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator
import re


class WhatsAppAccountOut(BaseModel):
    id: UUID
    company_id: UUID
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    business_name: Optional[str] = None
    business_id: Optional[str] = None
    connection_status: str
    last_sync_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class OnboardingSessionOut(BaseModel):
    id: UUID
    company_id: UUID
    current_step: int
    status: str
    last_completed_step: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class MetaCallbackRequest(BaseModel):
    code: str
    waba_id: Optional[str] = None          # provided by EBS message event; queried if absent
    phone_number_id: Optional[str] = None  # provided by EBS message event; queried if absent


class StepUpdateRequest(BaseModel):
    step: int


class ManualSetupRequest(BaseModel):
    waba_id: str
    access_token: str
    phone_number_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    business_name: Optional[str] = None

    @field_validator("waba_id", "access_token")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field is required")
        return v


# ── Template schemas ───────────────────────────────────────

# A header is either absent, TEXT, or one of the three media formats. Media headers
# carry no text; they need a header_handle at create time so Meta's reviewer can see
# a sample, and a per-send URL at send time.
HEADER_FORMATS = ("TEXT", "IMAGE", "DOCUMENT", "VIDEO")
MEDIA_HEADER_FORMATS = ("IMAGE", "DOCUMENT", "VIDEO")


def _validate_header_format(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return None
    if v.upper() not in HEADER_FORMATS:
        raise ValueError(f"header_format must be one of: {', '.join(HEADER_FORMATS)}")
    return v.upper()


class TemplateButtonIn(BaseModel):
    type: str                    # QUICK_REPLY | URL | PHONE_NUMBER
    text: str
    url: Optional[str] = None
    phone_number: Optional[str] = None


class TemplateCreateRequest(BaseModel):
    name: str
    category: str                # MARKETING | UTILITY | AUTHENTICATION
    language: str = "en_US"
    header_format: Optional[str] = None    # None | TEXT | IMAGE | DOCUMENT | VIDEO
    header_text: Optional[str] = None      # TEXT headers only
    header_handle: Optional[str] = None    # media headers only — from the media-handle endpoint
    body_text: str
    footer_text: Optional[str] = None
    buttons: Optional[List[TemplateButtonIn]] = None

    @field_validator("header_format")
    @classmethod
    def validate_header_format(cls, v: Optional[str]) -> Optional[str]:
        return _validate_header_format(v)

    @model_validator(mode="after")
    def validate_header(self):
        # Meta rejects a media header without a sample, so catch it here rather than
        # spending a round trip to be told the same thing less clearly.
        if self.header_format in MEDIA_HEADER_FORMATS and not self.header_handle:
            raise ValueError(
                f"A {self.header_format} header needs a sample file — "
                "upload one to get a header_handle first"
            )
        return self

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9_]+$", v):
            raise ValueError("Template name must be lowercase letters, numbers, and underscores only")
        if len(v) > 512:
            raise ValueError("Template name must be 512 characters or fewer")
        return v

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        allowed = {"MARKETING", "UTILITY", "AUTHENTICATION"}
        if v.upper() not in allowed:
            raise ValueError(f"Category must be one of: {', '.join(allowed)}")
        return v.upper()


class TemplateUpdateRequest(BaseModel):
    category: Optional[str] = None
    header_format: Optional[str] = None
    header_text: Optional[str] = None
    # Optional on update: omitting it means "keep the media already approved", and
    # _build_components carries the existing header forward. Supplying one replaces it.
    header_handle: Optional[str] = None
    body_text: str
    footer_text: Optional[str] = None
    buttons: Optional[List[TemplateButtonIn]] = None

    @field_validator("header_format")
    @classmethod
    def validate_header_format(cls, v: Optional[str]) -> Optional[str]:
        return _validate_header_format(v)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"MARKETING", "UTILITY", "AUTHENTICATION"}
        if v.upper() not in allowed:
            raise ValueError(f"Category must be one of: {', '.join(allowed)}")
        return v.upper()


class TemplateOut(BaseModel):
    id: UUID
    company_id: UUID
    waba_id: str
    meta_template_id: Optional[str] = None
    name: str
    category: str
    language: str
    status: str
    components: Any
    rejection_reason: Optional[str] = None
    param_mapping: Optional[dict] = None
    cta_mapping: Optional[dict] = None
    mobile_mapping: Optional[str] = None
    header_mapping: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    synced_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TemplateMappingUpdate(BaseModel):
    param_mapping:  Optional[dict] = None   # {"1": "customer_name", "2": "order.amount"}
    cta_mapping:    Optional[dict] = None   # {"0": "invoice_url"}
    mobile_mapping: Optional[str]  = None   # dot-path to phone number in data, e.g. "customer.phone"
    header_mapping: Optional[str]  = None   # dot-path to media URL, e.g. "data.receipt_image_url"


class TemplateSyncResponse(BaseModel):
    synced_count: int
    created: int
    updated: int
