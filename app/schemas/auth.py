import uuid
from typing import Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class AccessTokenResponse(BaseModel):
    access_token: str
    refresh_token: str          # rotated on every refresh — client must save the new one
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str
    phone: Optional[str]
    role_name: Optional[str]
    company_id: Optional[uuid.UUID]
    is_active: bool
    must_change_password: bool

    model_config = {"from_attributes": True}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=200)


class MeResponse(BaseModel):
    """Response for GET /api/auth/me — includes full permissions dict."""
    id: uuid.UUID
    username: str
    full_name: str
    role_name: Optional[str]
    company_id: Optional[uuid.UUID]
    permissions: dict  # {page: {read, create, write, delete}}
