"""Request and response shapes for the agent app API."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class AgentLoginRequest(BaseModel):
    username:    str = Field(..., min_length=1, max_length=100)
    password:    str = Field(..., min_length=1, max_length=200)
    # The licence key issued by the external licensing server. Matched against what
    # the admin recorded; never validated against that server, which is not ours.
    customer_id: str = Field(..., min_length=1, max_length=100)
    # Settings.Secure.ANDROID_ID in practice. Survives a reinstall, not a factory
    # reset — see the release-device admin action.
    device_id:   str = Field(..., min_length=1, max_length=200)

    # Support context. Optional so a login never fails for want of them.
    app_version: str | None = Field(default=None, max_length=50)
    os_version:  str | None = Field(default=None, max_length=50)

    @field_validator("username", "customer_id", "device_id")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()


class AgentIdentity(BaseModel):
    """Who the caller is. Embedded in login so the app needs no second call."""
    user_id:      uuid.UUID
    username:     str
    full_name:    str
    agent_id:     str
    company_id:   uuid.UUID | None
    company_name: str | None


class DeviceInfo(BaseModel):
    device_id: str
    bound_at:  datetime | None
    # True only on the login that claimed this handset, so the app can say "this
    # phone is now registered to your account" — and the agent learns the one-device
    # rule immediately rather than the first time they try a different phone.
    newly_bound: bool = False


class AgentSessionResponse(BaseModel):
    """
    The login and session-check payload.

    `expires_at` is null in normal operation: agent sessions live until revoked. The
    app should render "signed in" without a date and still handle a 401 at any moment,
    since revocation can happen while it is running.
    """
    token:       str | None = None       # only present on login
    expires_at:  datetime | None = None
    # The baseline for the app's clock offset. Android clocks drift and users set them
    # by hand, so the 24-hour window countdown must tick against our clock, not the
    # handset's — see AGENT_INBOX_DESIGN.md §7.1.
    server_time: datetime

    agent:  AgentIdentity
    device: DeviceInfo
