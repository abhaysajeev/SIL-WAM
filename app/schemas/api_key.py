import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CompanyApiKeyCreate(BaseModel):
    company_id: uuid.UUID
    label: Optional[str] = None
    notify_url: Optional[str] = None


class CompanyApiKeyUpdate(BaseModel):
    label: Optional[str] = None
    notify_url: Optional[str] = None


class CompanyApiKeyOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    label: Optional[str]
    notify_url: Optional[str]
    is_active: bool
    last_used_at: Optional[datetime]
    created_at: datetime
    # api_key intentionally excluded — only returned on creation via CompanyApiKeyCreated
    model_config = {"from_attributes": True}


class CompanyApiKeyCreated(CompanyApiKeyOut):
    """Returned once on creation. api_key is never exposed again after this."""
    api_key: str
