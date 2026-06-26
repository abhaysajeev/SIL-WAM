from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class ErrorLogListItem(BaseModel):
    id: int
    title: str
    method: str
    error_type: str
    user: Optional[str]
    seen: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ErrorLogDetail(ErrorLogListItem):
    traceback: str
    request_data: Optional[Any]
    ip_address: Optional[str]


class ErrorLogListResponse(BaseModel):
    items: list[ErrorLogListItem]
    total: int
    page: int
    page_size: int


class ErrorLogPatch(BaseModel):
    seen: bool
