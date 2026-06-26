import uuid
from typing import Optional

from pydantic import BaseModel, Field


class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    company_code: str = Field(..., min_length=1, max_length=50, pattern=r"^[A-Z0-9_\-]+$")
    is_active: bool = True


class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    company_code: Optional[str] = Field(None, min_length=1, max_length=50, pattern=r"^[A-Z0-9_\-]+$")
    is_active: Optional[bool] = None


class CompanyOut(BaseModel):
    id: uuid.UUID
    name: str
    company_code: str
    is_active: bool

    model_config = {"from_attributes": True}
