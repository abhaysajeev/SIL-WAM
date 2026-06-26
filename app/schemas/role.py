import uuid
from typing import Optional

from pydantic import BaseModel, Field


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class RoleUpdate(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: Optional[str]
    is_system: bool

    model_config = {"from_attributes": True}


class PagePermission(BaseModel):
    page_name: str
    can_read: bool = False
    can_create: bool = False
    can_write: bool = False
    can_delete: bool = False


class PermissionMatrixUpdate(BaseModel):
    """Body for PUT /api/roles/{id}/permissions — full matrix replacement."""
    permissions: list[PagePermission]
