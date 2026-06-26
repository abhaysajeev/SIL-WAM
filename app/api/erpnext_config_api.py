"""ERPNextConfig CRUD — per-company ERPNext integration credentials."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import company_filter, require
from app.models.erpnext_config import ERPNextConfig
from app.schemas.erpnext_config import ERPNextConfigCreate, ERPNextConfigOut, ERPNextConfigUpdate

router = APIRouter(
    prefix="/api/erpnext-configs",
    tags=["ERPNext Config"],
    dependencies=[Depends(require("erpnext_configs", "read"))],
)


@router.get("/", response_model=list[ERPNextConfigOut])
def list_configs(
    db: Session = Depends(get_db),
    user=Depends(require("erpnext_configs", "read")),
):
    cid = company_filter(user)
    q = db.query(ERPNextConfig)
    if cid:
        q = q.filter(ERPNextConfig.company_id == cid)
    return q.order_by(ERPNextConfig.created_at.desc()).all()


@router.get("/{config_id}", response_model=ERPNextConfigOut)
def get_config(
    config_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("erpnext_configs", "read")),
):
    cid = company_filter(user)
    cfg = db.query(ERPNextConfig).filter(ERPNextConfig.id == config_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="ERPNext config not found")
    if cid and str(cfg.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")
    return cfg


@router.post("/", response_model=ERPNextConfigOut, status_code=status.HTTP_201_CREATED)
def create_config(
    payload: ERPNextConfigCreate,
    db: Session = Depends(get_db),
    user=Depends(require("erpnext_configs", "create")),
):
    cid = company_filter(user)
    if cid and str(payload.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    existing = db.query(ERPNextConfig).filter(
        ERPNextConfig.company_id == payload.company_id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="An ERPNext config already exists for this company")

    cfg = ERPNextConfig(**payload.model_dump())
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


@router.put("/{config_id}", response_model=ERPNextConfigOut)
def update_config(
    config_id: uuid.UUID,
    payload: ERPNextConfigUpdate,
    db: Session = Depends(get_db),
    user=Depends(require("erpnext_configs", "write")),
):
    cid = company_filter(user)
    cfg = db.query(ERPNextConfig).filter(ERPNextConfig.id == config_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="ERPNext config not found")
    if cid and str(cfg.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(cfg, field, value)
    db.commit()
    db.refresh(cfg)
    return cfg


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_config(
    config_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("erpnext_configs", "delete")),
):
    cid = company_filter(user)
    cfg = db.query(ERPNextConfig).filter(ERPNextConfig.id == config_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="ERPNext config not found")
    if cid and str(cfg.company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")

    db.delete(cfg)
    db.commit()
