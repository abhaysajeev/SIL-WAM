import uuid
from typing import Optional

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    full_name: str = Field(..., min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    password: str = Field(..., min_length=8, max_length=200)
    role_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    must_change_password: bool = True


class UserUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=20)
    role_id: Optional[uuid.UUID] = None
    company_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None
    must_change_password: Optional[bool] = None


class UserPasswordChange(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=200)


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str
    phone: Optional[str]
    role_id: Optional[uuid.UUID]
    company_id: Optional[uuid.UUID]
    is_active: bool
    must_change_password: bool

    model_config = {"from_attributes": True}
