"""Error log SSR pages — super_admin only."""
import json
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import ERROR_LOGS_DOCTYPE
from app.models.error_log import ErrorLog

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter(tags=["Pages"])

_PAGE_SIZE = 50


@router.get("/error-logs", response_class=HTMLResponse)
def error_logs_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if ctx["user"]["role_name"] != "super_admin":
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    logs = (
        db.query(ErrorLog)
        .order_by(ErrorLog.created_at.desc())
        .limit(_PAGE_SIZE)
        .all()
    )

    rows = [
        {
            "id":         str(log.id),
            "title":      log.title,
            "method":     log.method,
            "error_type": log.error_type,
            "user":       log.user or "—",
            "seen":       log.seen,
            "created_at": log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "—",
        }
        for log in logs
    ]

    unseen_count = db.query(ErrorLog).filter(ErrorLog.seen == False).count()

    return templates.TemplateResponse("error_logs/list.html", {
        "request":      request,
        "user":         ctx["user"],
        "perms":        ctx["perms"],
        "dt":           ERROR_LOGS_DOCTYPE,
        "rows":         rows,
        "unseen_count": unseen_count,
        "active":       "error-logs",
    })


@router.get("/error-logs/{log_id}", response_class=HTMLResponse)
def error_log_detail(
    log_id: int,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if ctx["user"]["role_name"] != "super_admin":
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)

    log = db.query(ErrorLog).filter(ErrorLog.id == log_id).first()
    if not log:
        return HTMLResponse("<h2>Error log not found</h2>", status_code=404)

    # Auto-mark as seen when viewed
    if not log.seen:
        log.seen = True
        db.commit()

    req_data_str = None
    if log.request_data:
        try:
            req_data_str = json.dumps(log.request_data, indent=2)
        except Exception:
            req_data_str = str(log.request_data)

    return templates.TemplateResponse("error_logs/detail.html", {
        "request":      request,
        "user":         ctx["user"],
        "perms":        ctx["perms"],
        "log":          log,
        "req_data_str": req_data_str,
        "active":       "error-logs",
    })
