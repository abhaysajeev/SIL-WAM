"""
SSR routes for the broadcast module.

/broadcast is a launcher listing the sub-areas a user can reach. Sub-pages live
under /broadcast/* so the feature shares one namespace.

Route order matters: /broadcast/phonebooks/new must be declared before
/broadcast/phonebooks/{phonebook_id} or "new" is parsed as a UUID.
"""
import os
import uuid as _uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import template_body
from app.core.database import get_db
from app.core.deps import get_page_user
from app.core.doctypes import PHONEBOOKS_DOCTYPE
from app.core.pagination import page_url, paginate
from app.models.company import Company
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.phonebook import Phonebook, PhonebookContact
from app.models.whatsapp import WhatsAppTemplate

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Required by layouts/list_view.html — see CLAUDE.md, "dv_search_text filter".
templates.env.filters["dv_search_text"] = lambda row: " ".join(
    str(v) for v in row.values() if v is not None
)
templates.env.globals["page_url"] = page_url

router = APIRouter(tags=["Pages"])

_DENIED = HTMLResponse("<h2>Access denied</h2>", status_code=403)


def _can_enter_broadcast(perms: dict) -> bool:
    """Either sub-area is enough to reach the launcher."""
    return bool(
        perms.get("phonebooks", {}).get("read")
        or perms.get("campaigns", {}).get("read")
    )


def _scoped_company_id(ctx) -> str | None:
    """
    None for admin-tier users (they see every company), otherwise the company
    UUID string to filter on. Page routes get ctx["user"] as a dict, so this is
    the page-side equivalent of deps.company_filter().
    """
    return ctx["user"].get("company_id")


def _owned_phonebook(db: Session, list_id, ctx):
    """Phonebook if it exists and the user's company owns it, else None."""
    pb = db.query(Phonebook).filter(Phonebook.id == list_id).first()
    if not pb:
        return None
    cid = _scoped_company_id(ctx)
    if cid and str(pb.company_id) != cid:
        return None
    return pb


# ── Launcher ──────────────────────────────────────────────────────────────────

@router.get("/broadcast", response_class=HTMLResponse)
def broadcast_home(request: Request, ctx=Depends(get_page_user)):
    if not _can_enter_broadcast(ctx["perms"]):
        return _DENIED
    return templates.TemplateResponse("broadcast/index.html", {
        "request": request, "user": ctx["user"], "perms": ctx["perms"],
        "active": "broadcast",
    })


# ── Phonebook list ────────────────────────────────────────────────────────────

@router.get("/broadcast/phonebooks", response_class=HTMLResponse)
def phonebooks_page(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("phonebooks", {}).get("read"):
        return _DENIED

    q = db.query(Phonebook, Company).join(Company, Phonebook.company_id == Company.id)
    cid = _scoped_company_id(ctx)
    if cid:
        q = q.filter(Phonebook.company_id == _uuid.UUID(cid))

    pairs = q.order_by(Phonebook.created_at.desc()).all()

    # One grouped query for all counts rather than one per row.
    counts = {}
    if pairs:
        counts = dict(
            db.query(PhonebookContact.phonebook_id, func.count(PhonebookContact.id))
            .filter(PhonebookContact.phonebook_id.in_([p.id for p, _ in pairs]))
            .group_by(PhonebookContact.phonebook_id)
            .all()
        )

    rows = [
        {
            "id":            str(pb.id),
            "name":          pb.name,
            "company_name":  comp.name if comp else "—",
            "contact_count": counts.get(pb.id, 0),
            "created_at":    pb.created_at.strftime("%d %b %Y") if pb.created_at else "—",
        }
        for pb, comp in pairs
    ]

    return templates.TemplateResponse("layouts/list_view.html", {
        "request": request, "user": ctx["user"], "perms": ctx["perms"],
        "active": "broadcast", "dt": PHONEBOOKS_DOCTYPE, "rows": rows,
    })


# ── Create form ───────────────────────────────────────────────────────────────
# Declared before /{phonebook_id} so "new" is not parsed as a UUID.

@router.get("/broadcast/phonebooks/new", response_class=HTMLResponse)
def phonebook_new(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("phonebooks", {}).get("create"):
        return _DENIED

    cid = _scoped_company_id(ctx)
    companies = db.query(Company).filter(Company.is_active.is_(True))
    if cid:
        companies = companies.filter(Company.id == _uuid.UUID(cid))

    return templates.TemplateResponse("layouts/form_view.html", {
        "request": request, "user": ctx["user"], "perms": ctx["perms"],
        "active": "broadcast", "dt": PHONEBOOKS_DOCTYPE,
        "record": None, "record_id": None,
        "roles": [], "companies": companies.order_by(Company.name).all(),
    })


# ── Detail: contacts ──────────────────────────────────────────────────────────

@router.get("/broadcast/phonebooks/{phonebook_id}", response_class=HTMLResponse)
def phonebook_detail(
    phonebook_id: _uuid.UUID,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if not ctx["perms"].get("phonebooks", {}).get("read"):
        return _DENIED

    pb = _owned_phonebook(db, phonebook_id, ctx)
    if not pb:
        return HTMLResponse("<h2>PhoneBook not found</h2>", status_code=404)

    company = db.query(Company).filter(Company.id == pb.company_id).first()

    q = db.query(PhonebookContact).filter(PhonebookContact.phonebook_id == pb.id)

    search = (request.query_params.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.filter(
            PhonebookContact.customer_name.ilike(like)
            | PhonebookContact.mobile_no.ilike(like)
            | PhonebookContact.email.ilike(like)
        )

    # Ordered before paginate() — an unordered query lets Postgres return rows in
    # a different order per page, silently dropping and duplicating across pages.
    page = paginate(q.order_by(PhonebookContact.created_at.desc()), request, per_page=50)

    return templates.TemplateResponse("broadcast/phonebook_detail.html", {
        "request": request, "user": ctx["user"], "perms": ctx["perms"],
        "active": "broadcast",
        "pb": pb, "company": company,
        "contacts": page.items, "page": page, "search": search,
    })


# ── Campaigns ─────────────────────────────────────────────────────────────────
# Declared after /broadcast/phonebooks/* so neither namespace shadows the other. The
# doctype layouts handle flat resources only; a campaign owns recipient rows, so
# these use their own templates.

@router.get("/broadcast/campaigns", response_class=HTMLResponse)
def campaigns_list(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    """Every broadcast, newest first, filterable by company."""
    perms = ctx["perms"]
    if not perms.get("campaigns", {}).get("read"):
        return _DENIED

    cid = _scoped_company_id(ctx)
    q = db.query(BroadcastCampaign, Company).join(
        Company, BroadcastCampaign.company_id == Company.id)
    if cid:
        q = q.filter(BroadcastCampaign.company_id == _uuid.UUID(cid))

    selected = (request.query_params.get("company") or "").strip()
    if selected and not cid:
        try:
            q = q.filter(BroadcastCampaign.company_id == _uuid.UUID(selected))
        except ValueError:
            selected = ""

    rows = [
        {
            "id": str(c.id), "name": c.name, "company_id": str(c.company_id),
            "company_name": comp.name, "status": c.status,
            "total": c.total or 0, "sent": c.sent or 0,
            "delivered": c.delivered or 0, "failed": c.failed or 0,
            "skipped": c.skipped or 0,
            "created_at": c.created_at,
        }
        for c, comp in q.order_by(BroadcastCampaign.created_at.desc()).all()
    ]

    # Only offer the filter when there is something to filter between.
    companies = []
    if not cid:
        companies = [
            {"id": str(x.id), "name": x.name}
            for x in db.query(Company).filter(Company.is_active.is_(True))
                       .order_by(Company.name).all()
        ]

    return templates.TemplateResponse("broadcast/campaign_list.html", {
        "request": request, "user": ctx["user"], "perms": perms,
        "active": "broadcast",
        "rows": rows, "companies": companies, "selected_company": selected,
    })


@router.get("/broadcast/campaigns/new", response_class=HTMLResponse)
def campaign_new(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    """
    Company → template → then either typed parameters + PhoneBooks (Standard),
    or a CSV upload (Personalized). All client-side until Save.
    """
    perms = ctx["perms"]
    if not perms.get("campaigns", {}).get("create"):
        return _DENIED

    cid = _scoped_company_id(ctx)
    companies = db.query(Company).filter(Company.is_active.is_(True))
    if cid:
        companies = companies.filter(Company.id == _uuid.UUID(cid))
    companies = companies.order_by(Company.name).all()

    # Approved templates and lists for every company the user can reach, sent up
    # front so switching company does not need a round trip.
    tq = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.status == "APPROVED")
    lq = db.query(Phonebook)
    if cid:
        tq = tq.filter(WhatsAppTemplate.company_id == _uuid.UUID(cid))
        lq = lq.filter(Phonebook.company_id == _uuid.UUID(cid))

    templates_by_company: dict[str, list] = {}
    for t in tq.order_by(WhatsAppTemplate.name).all():
        templates_by_company.setdefault(str(t.company_id), []).append({
            "id": str(t.id), "name": t.name, "language": t.language,
            "category": t.category, "components": t.components or [],
        })

    counts = dict(
        db.query(PhonebookContact.phonebook_id, func.count(PhonebookContact.id))
        .group_by(PhonebookContact.phonebook_id).all()
    )
    lists_by_company: dict[str, list] = {}
    for bl in lq.order_by(Phonebook.name).all():
        lists_by_company.setdefault(str(bl.company_id), []).append({
            "id": str(bl.id), "name": bl.name, "contacts": counts.get(bl.id, 0),
        })

    # The template reads ?mode= from the request itself — see campaign_new.html.
    # Anything other than "personalized" falls back to Standard rather than
    # erroring; a mistyped query string should not be a dead end.
    return templates.TemplateResponse("broadcast/campaign_new.html", {
        "request": request, "user": ctx["user"], "perms": perms,
        "active": "broadcast",
        "companies": [{"id": str(c.id), "name": c.name} for c in companies],
        "templates_by_company": templates_by_company,
        "lists_by_company": lists_by_company,
    })


@router.get("/broadcast/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(
    campaign_id: str,
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    """Recipient table, screening summary, send, then progress and insights."""
    perms = ctx["perms"]
    if not perms.get("campaigns", {}).get("read"):
        return _DENIED

    try:
        oid = _uuid.UUID(campaign_id)
    except ValueError:
        return HTMLResponse("<h2>Campaign not found</h2>", status_code=404)

    campaign = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == oid).first()
    if not campaign:
        return HTMLResponse("<h2>Campaign not found</h2>", status_code=404)

    cid = _scoped_company_id(ctx)
    if cid and str(campaign.company_id) != cid:
        return _DENIED

    company  = db.query(Company).filter(Company.id == campaign.company_id).first()
    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == campaign.template_id).first()

    q = (
        db.query(BroadcastRecipient)
        .filter(BroadcastRecipient.campaign_id == campaign.id)
        .order_by(BroadcastRecipient.mobile_no)
    )
    page = paginate(q, request, per_page=50)

    return templates.TemplateResponse("broadcast/campaign_detail.html", {
        "request": request, "user": ctx["user"], "perms": perms,
        "active": "broadcast",
        "campaign": campaign, "company": company, "template": template,
        "preview": template_body.render(
            template.components if template else [], campaign.shared_params
        ),
        "recipients": page.items, "page": page,
    })
