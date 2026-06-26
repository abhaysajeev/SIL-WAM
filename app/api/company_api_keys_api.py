"""Company API Key management — admin/super_admin only.

Keys are generated here and returned ONCE in CompanyApiKeyCreated.
After that the raw key is never exposed; the list endpoint returns masked versions.
Revoke sets is_active=False (soft delete) so last_used_at and audit history are preserved.
"""
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import company_filter, require
from app.models.api_key import CompanyApiKey
from app.schemas.api_key import CompanyApiKeyCreate, CompanyApiKeyCreated, CompanyApiKeyOut, CompanyApiKeyUpdate
from app.utils.error_logger import log_error

router = APIRouter(
    prefix="/api/company-api-keys",
    tags=["API Keys"],
    dependencies=[Depends(require("companies", "write"))],
)


def _mask(key: str) -> str:
    return "••••" + key[-4:]


def _key_to_out(k: CompanyApiKey) -> dict:
    return {
        "id":           str(k.id),
        "company_id":   str(k.company_id),
        "label":        k.label,
        "notify_url":   k.notify_url,
        "is_active":    k.is_active,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at":   k.created_at.isoformat() if k.created_at else None,
        "masked_key":   _mask(k.api_key),
    }


@router.get("/")
def list_keys(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("companies", "write")),
):
    cid = company_filter(user)
    if cid and str(company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    keys = db.query(CompanyApiKey).filter(
        CompanyApiKey.company_id == company_id,
    ).order_by(CompanyApiKey.created_at.desc()).all()

    return [_key_to_out(k) for k in keys]


@router.post("/", status_code=status.HTTP_201_CREATED)
def generate_key(
    request: Request,
    payload: CompanyApiKeyCreate,
    db: Session = Depends(get_db),
    user=Depends(require("companies", "write")),
):
    """Generate a new API key. The raw key is returned ONCE in this response only."""
    cid = company_filter(user)
    if cid and str(payload.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    raw_key = secrets.token_urlsafe(48)
    record = CompanyApiKey(
        company_id=payload.company_id,
        api_key=raw_key,
        label=payload.label,
        notify_url=payload.notify_url,
        is_active=True,
    )
    db.add(record)
    try:
        db.commit()
        db.refresh(record)
    except Exception as exc:
        db.rollback()
        log_error(
            "API key generation failed",
            f"POST /api/company-api-keys/ company_id={payload.company_id}",
            exc,
            request=request,
            user=str(user.id),
        )
        raise HTTPException(status_code=500, detail="Failed to generate key")

    result = _key_to_out(record)
    result["api_key"] = raw_key   # only time the full key is exposed
    return result


@router.put("/{key_id}")
def update_key(
    key_id: uuid.UUID,
    payload: CompanyApiKeyUpdate,
    db: Session = Depends(get_db),
    user=Depends(require("companies", "write")),
):
    """Update label or notify_url — cannot change the key itself."""
    cid = company_filter(user)
    record = db.query(CompanyApiKey).filter(CompanyApiKey.id == key_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="API key not found")
    if cid and str(record.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return _key_to_out(record)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("companies", "write")),
):
    """Revoke a key (soft delete — sets is_active=False)."""
    cid = company_filter(user)
    record = db.query(CompanyApiKey).filter(CompanyApiKey.id == key_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="API key not found")
    if cid and str(record.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    record.is_active = False
    db.commit()


@router.delete("/{key_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
def delete_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("companies", "write")),
):
    """Permanently delete a key from the database."""
    cid = company_filter(user)
    record = db.query(CompanyApiKey).filter(CompanyApiKey.id == key_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="API key not found")
    if cid and str(record.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    db.delete(record)
    db.commit()
