import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ERPNextConfigCreate(BaseModel):
    company_id: uuid.UUID
    base_url:   str
    api_key:    str
    api_secret: str
    pdf_method: str | None = None
    is_active:  bool = True


class ERPNextConfigUpdate(BaseModel):
    base_url:   str | None = None
    api_key:    str | None = None
    api_secret: str | None = None
    pdf_method: str | None = None
    is_active:  bool | None = None


class ERPNextConfigOut(BaseModel):
    id:         uuid.UUID
    company_id: uuid.UUID
    base_url:   str
    api_key:    str
    pdf_method: str | None
    is_active:  bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
