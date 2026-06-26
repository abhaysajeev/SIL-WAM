"""Error log admin API — super_admin only."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_super_admin
from app.models.error_log import ErrorLog
from app.schemas.error_log import (
    ErrorLogDetail,
    ErrorLogListResponse,
    ErrorLogPatch,
)

router = APIRouter(
    prefix="/api/error-logs",
    tags=["Error Logs"],
    dependencies=[Depends(require_super_admin)],
)

PAGE_SIZE = 50


@router.get("/", response_model=ErrorLogListResponse)
def list_error_logs(
    seen: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    order: str = Query("latest"),
    db: Session = Depends(get_db),
):
    q = db.query(ErrorLog)

    if seen is not None:
        q = q.filter(ErrorLog.seen == seen)
    if search:
        pattern = f"%{search}%"
        q = q.filter(
            ErrorLog.title.ilike(pattern) | ErrorLog.method.ilike(pattern)
        )

    total = q.with_entities(func.count()).scalar()

    if order == "oldest":
        q = q.order_by(ErrorLog.created_at.asc())
    else:
        q = q.order_by(ErrorLog.created_at.desc())

    items = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    return ErrorLogListResponse(
        items=items,
        total=total,
        page=page,
        page_size=PAGE_SIZE,
    )


@router.get("/{log_id}", response_model=ErrorLogDetail)
def get_error_log(log_id: int, db: Session = Depends(get_db)):
    log = db.query(ErrorLog).filter(ErrorLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Error log not found")
    return log


@router.patch("/{log_id}", response_model=ErrorLogDetail)
def patch_error_log(log_id: int, payload: ErrorLogPatch, db: Session = Depends(get_db)):
    log = db.query(ErrorLog).filter(ErrorLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Error log not found")
    log.seen = payload.seen
    db.commit()
    db.refresh(log)
    return log


@router.delete("/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_error_log(log_id: int, db: Session = Depends(get_db)):
    log = db.query(ErrorLog).filter(ErrorLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Error log not found")
    db.delete(log)
    db.commit()


@router.post("/mark-all-seen", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_seen(db: Session = Depends(get_db)):
    db.query(ErrorLog).filter(ErrorLog.seen == False).update({"seen": True})
    db.commit()
