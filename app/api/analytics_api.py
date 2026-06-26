"""
Analytics API — JWT Bearer, company-scoped.

Three endpoints feed the /reports dashboard page and any external consumer:
  GET /api/analytics/summary       — message volume + delivery funnel
  GET /api/analytics/services      — service status breakdown + daily trend
  GET /api/analytics/conversations — conversation totals
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import company_filter, require
from app.models.conversation import Conversation, Message, Service

router = APIRouter(
    prefix="/api/analytics",
    tags=["Analytics"],
    dependencies=[Depends(require("reports", "read"))],
)

_DEFAULT_DAYS = 30


def _resolve_range(from_date: Optional[date], to_date: Optional[date]):
    today = date.today()
    d_to   = to_date   or today
    d_from = from_date or (today - timedelta(days=_DEFAULT_DAYS - 1))
    start  = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    end    = datetime(d_to.year,   d_to.month,   d_to.day, 23, 59, 59, tzinfo=timezone.utc)
    return start, end, d_from, d_to


@router.get("/summary")
def analytics_summary(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    current_user=Depends(require("reports", "read")),
    db: Session = Depends(get_db),
):
    start, end, d_from, d_to = _resolve_range(from_date, to_date)
    cid = company_filter(current_user)

    def _msg_q():
        q = (
            db.query(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .filter(Message.created_at >= start, Message.created_at <= end)
        )
        if cid:
            q = q.filter(Conversation.company_id == cid)
        return q

    total    = _msg_q().with_entities(func.count(Message.id)).scalar() or 0
    inbound  = _msg_q().filter(Message.direction == "inbound") .with_entities(func.count(Message.id)).scalar() or 0
    outbound = _msg_q().filter(Message.direction == "outbound").with_entities(func.count(Message.id)).scalar() or 0

    def _out_q():
        return _msg_q().filter(Message.direction == "outbound")

    sent      = _out_q().with_entities(func.count(Message.id)).scalar() or 0
    delivered = _out_q().filter(Message.status.in_(["delivered", "read"])).with_entities(func.count(Message.id)).scalar() or 0
    read_cnt  = _out_q().filter(Message.status == "read").with_entities(func.count(Message.id)).scalar() or 0
    failed    = _out_q().filter(Message.status == "failed").with_entities(func.count(Message.id)).scalar() or 0

    delivery_rate = round(delivered / sent * 100, 1) if sent else 0

    # Daily message volume (grouped by calendar date)
    dq = (
        db.query(
            func.date(Message.created_at).label("day"),
            func.count(Message.id).label("count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Message.created_at >= start, Message.created_at <= end)
    )
    if cid:
        dq = dq.filter(Conversation.company_id == cid)
    daily_rows = dq.group_by(func.date(Message.created_at)).order_by(func.date(Message.created_at)).all()
    daily = [{"date": str(r.day), "count": r.count} for r in daily_rows]

    return {
        "total":         total,
        "inbound":       inbound,
        "outbound":      outbound,
        "sent":          sent,
        "delivered":     delivered,
        "read":          read_cnt,
        "failed":        failed,
        "delivery_rate": delivery_rate,
        "daily":         daily,
        "from_date":     str(d_from),
        "to_date":       str(d_to),
    }


@router.get("/services")
def analytics_services(
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    current_user=Depends(require("reports", "read")),
    db: Session = Depends(get_db),
):
    start, end, d_from, d_to = _resolve_range(from_date, to_date)
    cid = company_filter(current_user)

    def _svc_q():
        q = db.query(Service).filter(
            Service.created_at >= start,
            Service.created_at <= end,
        )
        if cid:
            q = q.filter(Service.company_id == cid)
        return q

    status_rows = (
        _svc_q()
        .with_entities(Service.status, func.count(Service.id))
        .group_by(Service.status)
        .all()
    )
    status_counts = {r[0]: r[1] for r in status_rows}
    total = sum(status_counts.values())

    completion_rate = round(status_counts.get("completed", 0) / total * 100, 1) if total else 0

    # Daily services created
    dq = (
        db.query(
            func.date(Service.created_at).label("day"),
            func.count(Service.id).label("count"),
        )
        .filter(Service.created_at >= start, Service.created_at <= end)
    )
    if cid:
        dq = dq.filter(Service.company_id == cid)
    daily_rows = dq.group_by(func.date(Service.created_at)).order_by(func.date(Service.created_at)).all()
    daily = [{"date": str(r.day), "count": r.count} for r in daily_rows]

    return {
        "total":           total,
        "completed":       status_counts.get("completed",   0),
        "in_progress":     status_counts.get("in_progress", 0),
        "waiting":         status_counts.get("waiting",     0),
        "expired":         status_counts.get("expired",     0),
        "failed":          status_counts.get("failed",      0),
        "completion_rate": completion_rate,
        "daily":           daily,
        "from_date":       str(d_from),
        "to_date":         str(d_to),
    }


@router.get("/conversations")
def analytics_conversations(
    current_user=Depends(require("reports", "read")),
    db: Session = Depends(get_db),
):
    cid    = company_filter(current_user)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    q = db.query(Conversation)
    if cid:
        q = q.filter(Conversation.company_id == cid)

    total      = q.with_entities(func.count(Conversation.id)).scalar() or 0
    active_7d  = q.filter(Conversation.last_activity_at >= cutoff).with_entities(func.count(Conversation.id)).scalar() or 0
    total_msgs = q.with_entities(func.sum(Conversation.total_messages)).scalar() or 0

    return {
        "total":          total,
        "active_7d":      active_7d,
        "total_messages": int(total_msgs or 0),
    }
