"""Per-company broadcast settings — CRUD."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import company_filter, require
from app.models.broadcast_settings import BroadcastSettings
from app.schemas.broadcast_settings import (
    BroadcastSettingsCreate,
    BroadcastSettingsOut,
    BroadcastSettingsUpdate,
)

router = APIRouter(
    prefix="/api/broadcast-settings",
    tags=["Broadcast Settings"],
    dependencies=[Depends(require("broadcast_settings", "read"))],
)


@router.get("/", response_model=list[BroadcastSettingsOut])
def list_settings(
    db: Session = Depends(get_db),
    user=Depends(require("broadcast_settings", "read")),
):
    cid = company_filter(user)
    q = db.query(BroadcastSettings)
    if cid:
        q = q.filter(BroadcastSettings.company_id == cid)
    return q.order_by(BroadcastSettings.created_at.desc()).all()


@router.get("/{settings_id}", response_model=BroadcastSettingsOut)
def get_settings(
    settings_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("broadcast_settings", "read")),
):
    cid = company_filter(user)
    row = db.query(BroadcastSettings).filter(BroadcastSettings.id == settings_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Broadcast settings not found")
    if cid and str(row.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")
    return row


@router.post("/", response_model=BroadcastSettingsOut, status_code=status.HTTP_201_CREATED)
def create_settings(
    payload: BroadcastSettingsCreate,
    db: Session = Depends(get_db),
    user=Depends(require("broadcast_settings", "create")),
):
    cid = company_filter(user)
    if cid and str(payload.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    existing = db.query(BroadcastSettings).filter(
        BroadcastSettings.company_id == payload.company_id
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Broadcast settings already exist for this company",
        )

    row = BroadcastSettings(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{settings_id}", response_model=BroadcastSettingsOut)
def update_settings(
    settings_id: uuid.UUID,
    payload: BroadcastSettingsUpdate,
    db: Session = Depends(get_db),
    user=Depends(require("broadcast_settings", "write")),
):
    cid = company_filter(user)
    row = db.query(BroadcastSettings).filter(BroadcastSettings.id == settings_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Broadcast settings not found")
    if cid and str(row.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    # exclude_none, so a field the form did not send keeps its current value rather
    # than being reset to a default. Every field here is an integer with a meaningful
    # zero, so exclude_unset would be wrong in the other direction.
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{settings_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_settings(
    settings_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("broadcast_settings", "delete")),
):
    """
    Remove a company's settings row.

    The company keeps sending: broadcast_config falls back to the system defaults,
    which are the same values the row was created with. Deleting is therefore a reset,
    not a way to stop a company broadcasting.
    """
    cid = company_filter(user)
    row = db.query(BroadcastSettings).filter(BroadcastSettings.id == settings_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Broadcast settings not found")
    if cid and str(row.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    db.delete(row)
    db.commit()
