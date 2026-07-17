import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require
import re

from app.models.company import Company
from app.models.whatsapp import WhatsAppAccount, WhatsAppOnboardingSession, WhatsAppTemplate
from app.schemas.whatsapp import (
    ManualSetupRequest,
    MetaCallbackRequest,
    OnboardingSessionOut,
    StepUpdateRequest,
    TemplateCreateRequest,
    TemplateMappingUpdate,
    TemplateOut,
    TemplateSyncResponse,
    TemplateUpdateRequest,
    WhatsAppAccountOut,
)
from app.utils.error_logger import log_error
from app.utils.whatsapp_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/whatsapp",
    tags=["WhatsApp"],
    dependencies=[Depends(require("companies", "read"))],
)

GRAPH_BASE = "https://graph.facebook.com/v22.0"


def _get_company_or_404(company_id: uuid.UUID, db: Session) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _account_to_dict(acc: WhatsAppAccount) -> dict:
    return {
        "id": str(acc.id),
        "company_id": str(acc.company_id),
        "waba_id": acc.waba_id,
        "phone_number_id": acc.phone_number_id,
        "display_phone_number": acc.display_phone_number,
        "business_name": acc.business_name,
        "business_id": acc.business_id,
        "connection_status": acc.connection_status,
        "last_sync_at": acc.last_sync_at.isoformat() if acc.last_sync_at else None,
        "created_at": acc.created_at.isoformat() if acc.created_at else None,
        "updated_at": acc.updated_at.isoformat() if acc.updated_at else None,
    }


# ── GET account ───────────────────────────────────────────────

@router.get("/{company_id}/account")
def get_whatsapp_account(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    _get_company_or_404(company_id, db)
    acc = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()
    if not acc:
        return {"connected": False}
    return {"connected": acc.connection_status == "active", "account": _account_to_dict(acc)}


# ── POST onboarding/session — create or resume ─────────────────

@router.post("/{company_id}/onboarding/session", response_model=OnboardingSessionOut)
def start_or_resume_session(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    _get_company_or_404(company_id, db)

    # Resume latest in-progress session
    session = db.query(WhatsAppOnboardingSession).filter(
        WhatsAppOnboardingSession.company_id == company_id,
        WhatsAppOnboardingSession.status == "in_progress",
    ).order_by(WhatsAppOnboardingSession.created_at.desc()).first()

    if session:
        return session

    # Create new session
    session = WhatsAppOnboardingSession(
        company_id=company_id,
        current_step=1,
        status="in_progress",
        last_completed_step=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ── PATCH onboarding/session — update current step ────────────

@router.patch("/{company_id}/onboarding/session", response_model=OnboardingSessionOut)
def update_session_step(
    company_id: uuid.UUID,
    payload: StepUpdateRequest,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    _get_company_or_404(company_id, db)
    session = db.query(WhatsAppOnboardingSession).filter(
        WhatsAppOnboardingSession.company_id == company_id,
        WhatsAppOnboardingSession.status == "in_progress",
    ).order_by(WhatsAppOnboardingSession.created_at.desc()).first()

    if not session:
        raise HTTPException(status_code=404, detail="No active onboarding session")

    session.current_step = payload.step
    if payload.step - 1 > session.last_completed_step:
        session.last_completed_step = payload.step - 1
    db.commit()
    db.refresh(session)
    return session


# ── POST callback — receive Meta code, exchange, store ────────

@router.post("/{company_id}/callback")
def meta_callback(
    company_id: uuid.UUID,
    payload: MetaCallbackRequest,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    _get_company_or_404(company_id, db)

    if not settings.FB_APP_ID or not settings.META_APP_SECRET:
        raise HTTPException(status_code=503, detail="Meta credentials not configured")

    try:
        # 1. Exchange authorization code for short-lived user access token
        with httpx.Client(timeout=15) as client:
            token_res = client.get(
                f"{GRAPH_BASE}/oauth/access_token",
                # No redirect_uri here: codes delivered via the WA_EMBEDDED_SIGNUP
                # message event are exchanged with client_id+client_secret+code only.
                # Sending redirect_uri (even empty) fails the exchange with OAuth
                # subcode 36008 because the dialog never used one for these codes.
                params={
                    "client_id":     settings.FB_APP_ID,
                    "client_secret": settings.META_APP_SECRET,
                    "code":          payload.code,
                },
            )
        if token_res.status_code != 200:
            logger.warning("Meta token exchange failed: %s", token_res.text)
            raise HTTPException(status_code=400, detail="Meta authentication failed. Please try again.")
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token returned by Meta.")

        # 2. Exchange short-lived token for long-lived token (~60 days)
        with httpx.Client(timeout=15) as client:
            lt_res = client.get(
                f"{GRAPH_BASE}/oauth/access_token",
                params={
                    "grant_type":       "fb_exchange_token",
                    "client_id":        settings.FB_APP_ID,
                    "client_secret":    settings.META_APP_SECRET,
                    "fb_exchange_token": access_token,
                },
            )
        if lt_res.status_code == 200:
            lt_data = lt_res.json()
            if lt_data.get("access_token"):
                access_token = lt_data["access_token"]
                logger.info("Long-lived token obtained for company %s", company_id)
        else:
            logger.warning("Long-lived token exchange failed, using short-lived token: %s", lt_res.text)

        token_expiry = datetime.now(timezone.utc) + timedelta(days=60)

        return _finalize_meta_connection(
            db, company_id, access_token, token_expiry,
            payload.waba_id, payload.phone_number_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        log_error(
            "WhatsApp Meta callback failed",
            f"POST /api/whatsapp/{company_id}/callback",
            e,
            request=request,
        )
        raise HTTPException(status_code=500, detail="Connection failed. Please try again.")


def _finalize_meta_connection(
    db: Session,
    company_id: uuid.UUID,
    access_token: str,
    token_expiry: datetime,
    waba_id: str = None,
    phone_number_id: str = None,
) -> dict:
    """
    Shared tail of the Meta connection flow — everything after an access
    token is in hand. Used by the JS-SDK popup callback (POST
    /{company_id}/callback) and the full-page redirect flow
    (GET /meta/oauth/callback). Resolves the WABA / phone number when not
    supplied, fetches their details, subscribes the app to the WABA
    (required for inbound webhooks), and upserts the account row.
    Raises HTTPException on unrecoverable failures; callers handle logging.
    """

    if not waba_id:
        # Fallback: query WABA list
        with httpx.Client(timeout=15) as client:
            waba_list_res = client.get(
                f"{GRAPH_BASE}/me/whatsapp_business_accounts",
                params={"fields": "id,name", "access_token": access_token},
            )
        if waba_list_res.status_code == 200:
            waba_list = waba_list_res.json().get("data", [])
            if waba_list:
                waba_id = waba_list[0]["id"]
            else:
                raise HTTPException(status_code=400, detail="No WhatsApp Business Account found for this account.")
        else:
            raise HTTPException(status_code=400, detail="Could not retrieve WhatsApp Business Account.")

    if not phone_number_id and waba_id:
        # Fallback: query phone numbers for the WABA
        with httpx.Client(timeout=15) as client:
            phones_fallback = client.get(
                f"{GRAPH_BASE}/{waba_id}/phone_numbers",
                params={"fields": "id,display_phone_number,verified_name,status", "access_token": access_token},
            )
        if phones_fallback.status_code == 200:
            phones = phones_fallback.json().get("data", [])
            if phones:
                phone_number_id = phones[0]["id"]

    # 3. Fetch phone number details
    if not phone_number_id:
        raise HTTPException(
            status_code=400,
            detail="No phone number found on your WhatsApp Business Account. Add a verified phone number in Meta Business Manager before connecting.",
        )

    display_phone_number = None
    phone_status = "unknown"
    business_name = ""
    with httpx.Client(timeout=15) as client:
        phone_res = client.get(
            f"{GRAPH_BASE}/{phone_number_id}",
            params={
                "fields":       "id,display_phone_number,verified_name,status,quality_rating",
                "access_token": access_token,
            },
        )
    if phone_res.status_code == 200:
        phone_data = phone_res.json()
        display_phone_number = phone_data.get("display_phone_number")
        business_name        = phone_data.get("verified_name", "")
        phone_status         = phone_data.get("status", "unknown")
    else:
        logger.warning("Phone number fetch failed: %s", phone_res.text)

    # 4. Fetch WABA details — name and on_behalf_of_business_info (business_id)
    business_id = None
    with httpx.Client(timeout=15) as client:
        waba_res = client.get(
            f"{GRAPH_BASE}/{waba_id}",
            params={
                "fields":       "id,name,on_behalf_of_business_info,ownership_type",
                "access_token": access_token,
            },
        )
    if waba_res.status_code == 200:
        waba_data = waba_res.json()
        if not business_name:
            business_name = waba_data.get("name", "")
        # on_behalf_of_business_info is a nested object with an "id" field
        obo = waba_data.get("on_behalf_of_business_info") or {}
        business_id = obo.get("id")
    else:
        logger.warning("WABA details fetch failed: %s", waba_res.text)

    # 5. Subscribe app to WABA — required to receive webhooks and make API calls
    with httpx.Client(timeout=15) as client:
        sub_res = client.post(
            f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if sub_res.status_code != 200:
        logger.warning("WABA subscription failed (non-fatal): %s", sub_res.text)
    else:
        logger.info("WABA %s subscribed to app successfully", waba_id)

    # Determine connection status from phone status
    connection_status = "active" if phone_status == "CONNECTED" else "pending"

    # 6. Upsert WhatsAppAccount
    encrypted_token = encrypt_token(access_token)
    acc = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()
    now = datetime.now(timezone.utc)
    if acc:
        acc.waba_id = waba_id
        acc.phone_number_id = phone_number_id
        acc.display_phone_number = display_phone_number
        acc.business_name = business_name
        acc.business_id = business_id
        acc.access_token_encrypted = encrypted_token
        acc.token_expiry = token_expiry
        acc.connection_status = connection_status
        acc.last_sync_at = now
    else:
        acc = WhatsAppAccount(
            company_id=company_id,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            business_name=business_name,
            business_id=business_id,
            access_token_encrypted=encrypted_token,
            token_expiry=token_expiry,
            connection_status=connection_status,
            last_sync_at=now,
        )
        db.add(acc)

    # 6. Complete any active onboarding session
    session = db.query(WhatsAppOnboardingSession).filter(
        WhatsAppOnboardingSession.company_id == company_id,
        WhatsAppOnboardingSession.status == "in_progress",
    ).order_by(WhatsAppOnboardingSession.created_at.desc()).first()
    if session:
        session.status = "completed"
        session.last_completed_step = 5
        session.current_step = 5

    db.commit()
    db.refresh(acc)
    return {"success": True, "account": _account_to_dict(acc)}


# ── DELETE disconnect ─────────────────────────────────────────

@router.delete("/{company_id}/disconnect")
def disconnect_whatsapp(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    _get_company_or_404(company_id, db)
    acc = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()
    if not acc:
        raise HTTPException(status_code=404, detail="No WhatsApp account connected")

    acc.waba_id = None
    acc.phone_number_id = None
    acc.display_phone_number = None
    acc.business_name = None
    acc.business_id = None
    acc.access_token_encrypted = None
    acc.token_expiry = None
    acc.connection_status = "disconnected"
    acc.last_sync_at = None
    db.commit()
    return {"success": True}


# ── POST refresh — re-sync from Meta ─────────────────────────

@router.post("/{company_id}/refresh")
def refresh_whatsapp_status(
    company_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    _get_company_or_404(company_id, db)
    acc = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()
    if not acc or not acc.access_token_encrypted:
        raise HTTPException(status_code=404, detail="No connected WhatsApp account")

    try:
        access_token = decrypt_token(acc.access_token_encrypted)

        if acc.phone_number_id:
            # Check phone number status — source of truth for connection health
            with httpx.Client(timeout=15) as client:
                phone_res = client.get(
                    f"{GRAPH_BASE}/{acc.phone_number_id}",
                    params={
                        "fields":       "id,display_phone_number,verified_name,status,quality_rating",
                        "access_token": access_token,
                    },
                )
            if phone_res.status_code == 200:
                phone_data = phone_res.json()
                phone_status = phone_data.get("status", "")
                # "CONNECTED" = production verified number
                # "UNVERIFIED" / "" = sandbox/test number — still functional, treat as active
                if phone_status == "CONNECTED":
                    acc.connection_status = "active"
                elif phone_status in ("UNVERIFIED", "PENDING", "") or not phone_status:
                    acc.connection_status = "active"   # test/sandbox — credentials valid
                else:
                    acc.connection_status = "error"
                if phone_data.get("display_phone_number"):
                    acc.display_phone_number = phone_data["display_phone_number"]
                if phone_data.get("verified_name") and not acc.business_name:
                    acc.business_name = phone_data["verified_name"]
            else:
                _raise_if_meta_auth_error(phone_res.json())
                acc.connection_status = "error"
        else:
            # No phone_number_id — verify by checking the WABA is still accessible
            with httpx.Client(timeout=15) as client:
                waba_res = client.get(
                    f"{GRAPH_BASE}/{acc.waba_id}",
                    params={"fields": "id", "access_token": access_token},
                )
            if waba_res.status_code == 200:
                acc.connection_status = "active"
            else:
                _raise_if_meta_auth_error(waba_res.json())
                acc.connection_status = "error"

        # Self-heal: re-subscribe on every refresh, not just at initial connect.
        # Accounts connected via manual-setup before this subscription step existed
        # (or where subscription failed silently at connect time) are otherwise
        # stuck forever — sends work, but inbound replies never arrive and nothing
        # errors anywhere to flag it. Idempotent and non-fatal if it fails.
        if acc.connection_status == "active":
            with httpx.Client(timeout=15) as client:
                sub_res = client.post(
                    f"{GRAPH_BASE}/{acc.waba_id}/subscribed_apps",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            if sub_res.status_code != 200:
                logger.warning("WABA re-subscription failed (non-fatal): %s", sub_res.text)

        acc.last_sync_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(acc)
        return {"success": True, "account": _account_to_dict(acc)}
    except Exception as e:
        db.rollback()
        log_error(
            "WhatsApp status refresh failed",
            f"POST /api/whatsapp/{company_id}/refresh",
            e,
            request=request,
        )
        raise HTTPException(status_code=500, detail="Refresh failed.")


# ── POST manual-setup — enter credentials directly ───────────
#    Verifies WABA + token against Meta, then upserts WhatsAppAccount.
#    Used while embedded signup is not yet available.

@router.post("/{company_id}/manual-setup")
def manual_setup(
    company_id: uuid.UUID,
    payload: ManualSetupRequest,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    _get_company_or_404(company_id, db)

    waba_id      = payload.waba_id.strip()
    access_token = payload.access_token.strip()
    phone_number_id      = (payload.phone_number_id or "").strip() or None
    display_phone_number = (payload.display_phone_number or "").strip() or None
    business_name        = (payload.business_name or "").strip() or None
    business_id  = None
    token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)  # conservative default

    try:
        with httpx.Client(timeout=15) as client:
            # Step 0 — try to exchange for a long-lived token (~60 days)
            #           Graph API Explorer tokens are short-lived (~1h); this extends them.
            if settings.FB_APP_ID and settings.META_APP_SECRET:
                lt_res = client.get(
                    f"{GRAPH_BASE}/oauth/access_token",
                    params={
                        "grant_type":        "fb_exchange_token",
                        "client_id":         settings.FB_APP_ID,
                        "client_secret":     settings.META_APP_SECRET,
                        "fb_exchange_token": access_token,
                    },
                )
                if lt_res.status_code == 200 and lt_res.json().get("access_token"):
                    access_token = lt_res.json()["access_token"]
                    token_expiry = datetime.now(timezone.utc) + timedelta(days=60)
                    logger.info("Long-lived token obtained for manual setup, company %s", company_id)
                else:
                    logger.warning("Long-lived token exchange failed (will use original): %s", lt_res.text)

            # Step 1 — verify token has access to this WABA.
            # Request only `id` first — test/sandbox WABAs don't expose `name`
            # or `on_behalf_of_business_info` and return error #100 if requested.
            waba_res = client.get(
                f"{GRAPH_BASE}/{waba_id}",
                params={
                    "fields":       "id",
                    "access_token": access_token,
                },
            )

        if waba_res.status_code != 200:
            err = waba_res.json().get("error", {})
            msg = err.get("message", "")
            code = err.get("code", 0)
            if code == 190 or "access token" in msg.lower() or "invalid oauth" in msg.lower():
                raise HTTPException(
                    status_code=400,
                    detail="Invalid or expired access token. Make sure it has the whatsapp_business_management permission.",
                )
            if waba_res.status_code == 404 or "does not exist" in msg.lower():
                raise HTTPException(
                    status_code=400,
                    detail="WABA ID not found. Check the ID in Meta Business Manager → WhatsApp Manager.",
                )
            raise HTTPException(
                status_code=400,
                detail=msg or "Could not verify credentials with Meta. Check your WABA ID and token.",
            )

        # Step 1b — subscribe app to WABA — required to receive webhooks. Non-fatal:
        # a subscription hiccup shouldn't block saving otherwise-valid credentials,
        # but it means messages will send fine while inbound replies silently vanish
        # until "Refresh Status" (which retries this) or manual-setup is re-run.
        with httpx.Client(timeout=15) as client:
            sub_res = client.post(
                f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if sub_res.status_code != 200:
            logger.warning("WABA subscription failed (non-fatal): %s", sub_res.text)
        else:
            logger.info("WABA %s subscribed to app successfully", waba_id)

        # Try to fetch richer WABA fields separately — non-fatal if unavailable (sandbox/test tokens)
        waba_data: dict = {}
        try:
            with httpx.Client(timeout=10) as client:
                rich_res = client.get(
                    f"{GRAPH_BASE}/{waba_id}",
                    params={
                        "fields":       "id,name,on_behalf_of_business_info",
                        "access_token": access_token,
                    },
                )
            if rich_res.status_code == 200:
                waba_data = rich_res.json()
        except Exception:
            pass

        if not business_name:
            business_name = waba_data.get("name") or None
        obo = waba_data.get("on_behalf_of_business_info") or {}
        business_id = obo.get("id")

        # Step 2 — if Phone Number ID provided, fetch display number
        if phone_number_id:
            with httpx.Client(timeout=15) as client:
                phone_res = client.get(
                    f"{GRAPH_BASE}/{phone_number_id}",
                    params={
                        "fields":       "id,display_phone_number,verified_name,status",
                        "access_token": access_token,
                    },
                )
            if phone_res.status_code == 200:
                phone_data = phone_res.json()
                if not display_phone_number:
                    display_phone_number = phone_data.get("display_phone_number") or None
                if not business_name:
                    business_name = phone_data.get("verified_name") or None
            else:
                logger.warning(
                    "Phone Number ID %s lookup failed (non-fatal): %s",
                    phone_number_id, phone_res.text,
                )

    except HTTPException:
        raise
    except Exception as e:
        log_error(
            "Manual WhatsApp setup failed",
            f"POST /api/whatsapp/{company_id}/manual-setup",
            e,
            request=request,
        )
        raise HTTPException(status_code=500, detail="Could not verify credentials with Meta. Check your token and try again.")

    encrypted_token = encrypt_token(access_token)
    now = datetime.now(timezone.utc)

    acc = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()

    if acc:
        acc.waba_id               = waba_id
        acc.phone_number_id       = phone_number_id
        acc.display_phone_number  = display_phone_number
        acc.business_name         = business_name
        acc.business_id           = business_id
        acc.access_token_encrypted = encrypted_token
        acc.token_expiry          = token_expiry
        acc.connection_status     = "active"
        acc.last_sync_at          = now
    else:
        acc = WhatsAppAccount(
            company_id=company_id,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            business_name=business_name,
            business_id=business_id,
            access_token_encrypted=encrypted_token,
            token_expiry=token_expiry,
            connection_status="active",
            last_sync_at=now,
        )
        db.add(acc)

    db.commit()
    db.refresh(acc)
    return {"success": True, "account": _account_to_dict(acc)}


# ════════════════════════════════════════════════════════════
# Template Management
# ════════════════════════════════════════════════════════════

def _raise_if_meta_auth_error(res_json: dict) -> None:
    """Convert Meta OAuth error codes into a readable 400."""
    err = res_json.get("error", {})
    if err.get("code") == 190 or err.get("error_subcode") in (460, 461, 462, 463, 464, 467):
        raise HTTPException(
            status_code=400,
            detail="WhatsApp access token has expired or been revoked. Disconnect and reconnect with a fresh token.",
        )


def _get_active_account(company_id: uuid.UUID, db: Session) -> WhatsAppAccount:
    """Return active WhatsApp account or raise HTTPException."""
    _get_company_or_404(company_id, db)
    acc = db.query(WhatsAppAccount).filter(
        WhatsAppAccount.company_id == company_id
    ).first()
    if not acc or not acc.access_token_encrypted:
        raise HTTPException(status_code=400, detail="WhatsApp account not connected.")
    if acc.connection_status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"WhatsApp account is not active (status: {acc.connection_status}). Click 'Refresh Status' to re-verify.",
        )
    return acc


def _clean_rejection(val) -> str | None:
    """Meta returns the string 'NONE' when there is no rejection reason."""
    if not val:
        return None
    s = str(val).strip()
    return None if s.upper() in ("NONE", "NULL", "") else s


def _template_to_dict(t: WhatsAppTemplate) -> dict:
    return {
        "id": str(t.id),
        "company_id": str(t.company_id),
        "waba_id": t.waba_id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "category": t.category,
        "language": t.language,
        "status": t.status,
        "components": t.components,
        "rejection_reason": t.rejection_reason,
        "param_mapping": t.param_mapping or {},
        "cta_mapping": t.cta_mapping or {},
        "mobile_mapping": t.mobile_mapping or "",
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "synced_at": t.synced_at.isoformat() if t.synced_at else None,
    }


def _build_components(payload) -> list:
    """Build Meta-compatible components array from create request."""
    components = []

    if payload.header_text:
        components.append({"type": "HEADER", "format": "TEXT", "text": payload.header_text})

    # Body — Meta requires an "example" block for every variable present
    body_component: dict = {"type": "BODY", "text": payload.body_text}
    var_indices = sorted(set(int(v) for v in re.findall(r"\{\{(\d+)\}\}", payload.body_text)))
    if var_indices:
        body_component["example"] = {
            "body_text": [[f"value_{i}" for i in var_indices]]
        }
    components.append(body_component)

    if payload.footer_text:
        components.append({"type": "FOOTER", "text": payload.footer_text})

    if payload.buttons:
        btns = []
        for b in payload.buttons[:3]:
            btn: dict = {"type": b.type.upper(), "text": b.text}
            if b.type.upper() == "URL" and b.url:
                btn["url"] = b.url
                # URL buttons with variables also need an example
                if "{{" in b.url:
                    btn["example"] = [re.sub(r"\{\{\d+\}\}", "example", b.url)]
            if b.type.upper() == "PHONE_NUMBER" and b.phone_number:
                btn["phone_number"] = b.phone_number
            btns.append(btn)
        if btns:
            components.append({"type": "BUTTONS", "buttons": btns})

    return components


# ── GET list ──────────────────────────────────────────────

@router.get("/{company_id}/templates")
def list_templates(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    _get_company_or_404(company_id, db)
    templates = (
        db.query(WhatsAppTemplate)
        .filter(WhatsAppTemplate.company_id == company_id)
        .order_by(WhatsAppTemplate.created_at.desc())
        .all()
    )
    return [_template_to_dict(t) for t in templates]


# ── POST sync ─────────────────────────────────────────────

@router.post("/{company_id}/templates/sync")
def sync_templates(
    company_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    acc = _get_active_account(company_id, db)
    try:
        access_token = decrypt_token(acc.access_token_encrypted)
        with httpx.Client(timeout=20) as client:
            res = client.get(
                f"{GRAPH_BASE}/{acc.waba_id}/message_templates",
                params={
                    "fields": "id,name,category,language,status,components,rejected_reason",
                    "limit": 100,
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if res.status_code != 200:
            _raise_if_meta_auth_error(res.json())
            raise HTTPException(status_code=400, detail=f"Meta API error: {res.text}")

        data = res.json().get("data", [])
        now = datetime.now(timezone.utc)
        created_count = 0
        updated_count = 0
        deleted_count = 0

        # Track every meta_template_id Meta returned in this sync
        meta_ids_seen: set[str] = set()

        for item in data:
            meta_id = item.get("id")
            if meta_id:
                meta_ids_seen.add(meta_id)

            # Match by meta_template_id first, fall back to name+language
            existing = None
            if meta_id:
                existing = db.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.company_id == company_id,
                    WhatsAppTemplate.meta_template_id == meta_id,
                ).first()
            if not existing:
                existing = db.query(WhatsAppTemplate).filter(
                    WhatsAppTemplate.company_id == company_id,
                    WhatsAppTemplate.name == item.get("name"),
                    WhatsAppTemplate.language == item.get("language", "en_US"),
                ).first()

            if existing:
                existing.status = item.get("status", existing.status)
                existing.meta_template_id = meta_id
                existing.components = item.get("components") or existing.components
                existing.rejection_reason = _clean_rejection(item.get("rejected_reason"))
                existing.synced_at = now
                updated_count += 1
            else:
                tpl = WhatsAppTemplate(
                    company_id=company_id,
                    waba_id=acc.waba_id,
                    meta_template_id=meta_id,
                    name=item.get("name", ""),
                    category=item.get("category", "MARKETING"),
                    language=item.get("language", "en_US"),
                    status=item.get("status", "PENDING"),
                    components=item.get("components") or [],
                    rejection_reason=_clean_rejection(item.get("rejected_reason")),
                    synced_at=now,
                )
                db.add(tpl)
                created_count += 1

        # Remove local templates that Meta no longer has.
        # Only delete records that have a meta_template_id (i.e. were previously synced
        # from Meta). Templates with no meta_template_id were created locally and not yet
        # confirmed by Meta — leave them alone.
        if meta_ids_seen:
            stale = db.query(WhatsAppTemplate).filter(
                WhatsAppTemplate.company_id == company_id,
                WhatsAppTemplate.meta_template_id.isnot(None),
                WhatsAppTemplate.meta_template_id.notin_(meta_ids_seen),
            ).all()
            for s in stale:
                db.delete(s)
                deleted_count += 1

        db.commit()
        return {
            "synced_count": len(data),
            "created": created_count,
            "updated": updated_count,
            "deleted": deleted_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        log_error("Template sync failed", f"POST /api/whatsapp/{company_id}/templates/sync", e, request=request)
        raise HTTPException(status_code=500, detail="Sync failed.")


# ── POST create ───────────────────────────────────────────

@router.post("/{company_id}/templates", response_model=TemplateOut)
def create_template(
    company_id: uuid.UUID,
    payload: TemplateCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    acc = _get_active_account(company_id, db)

    # Check duplicate name+language
    exists = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.company_id == company_id,
        WhatsAppTemplate.name == payload.name,
        WhatsAppTemplate.language == payload.language,
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="A template with this name and language already exists.")

    components = _build_components(payload)
    meta_body = {
        "name": payload.name,
        "language": payload.language,
        "category": payload.category,
        "components": components,
    }

    try:
        access_token = decrypt_token(acc.access_token_encrypted)
        with httpx.Client(timeout=20) as client:
            res = client.post(
                f"{GRAPH_BASE}/{acc.waba_id}/message_templates",
                json=meta_body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        res_data = res.json()
        if res.status_code not in (200, 201):
            err = res_data.get("error", {})
            _raise_if_meta_auth_error(res_data)
            msg = err.get("message", "")
            # Meta returns error_data.code == 100 + "already exists" for duplicates
            if "already exists" in msg.lower() or err.get("error_user_title", "").lower().find("exist") != -1:
                raise HTTPException(
                    status_code=409,
                    detail="This template already exists on Meta. Click Sync to import it.",
                )
            raise HTTPException(status_code=400, detail=msg or "Meta API error")

        tpl = WhatsAppTemplate(
            company_id=company_id,
            waba_id=acc.waba_id,
            meta_template_id=res_data.get("id"),
            name=payload.name,
            category=payload.category,
            language=payload.language,
            status=res_data.get("status", "PENDING"),
            components=components,
            synced_at=datetime.now(timezone.utc),
        )
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        return tpl
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        log_error("Template create failed", f"POST /api/whatsapp/{company_id}/templates", e, request=request)
        raise HTTPException(status_code=500, detail="Template creation failed.")


# ── PUT update (edit + re-submit for review) ─────────────
#
# Meta Cloud API: POST /{message_template_id}
# Editable fields: components, category.
# Name and language cannot be changed.
# After edit: REJECTED → PENDING; APPROVED → re-reviewed (stays live until decision).

@router.put("/{company_id}/templates/{template_id}")
def update_template(
    company_id: uuid.UUID,
    template_id: uuid.UUID,
    payload: TemplateUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    acc = _get_active_account(company_id, db)

    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.company_id == company_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if not tpl.meta_template_id:
        raise HTTPException(
            status_code=400,
            detail="Template has not been confirmed by Meta yet. Try syncing first.",
        )

    components = _build_components(payload)

    meta_body: dict = {"components": components}
    if payload.category:
        meta_body["category"] = payload.category

    try:
        access_token = decrypt_token(acc.access_token_encrypted)
        with httpx.Client(timeout=20) as client:
            res = client.post(
                f"{GRAPH_BASE}/{tpl.meta_template_id}",
                json=meta_body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        res_data = res.json()
        if res.status_code not in (200, 201):
            err = res_data.get("error", {})
            logger.error(
                "Meta template update rejected — status=%s body=%s payload=%s",
                res.status_code, res_data, meta_body,
            )
            _raise_if_meta_auth_error(res_data)
            raise HTTPException(
                status_code=400,
                detail=err.get("error_user_msg") or err.get("message") or "Meta API error while updating template.",
            )

        tpl.components = components
        if payload.category:
            tpl.category = payload.category
        # Meta returns {"success": true} or {"status": "PENDING"} on edit
        new_status = res_data.get("status")
        if new_status:
            tpl.status = new_status
        elif tpl.status == "APPROVED":
            tpl.status = "PENDING"
        tpl.synced_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(tpl)
        return _template_to_dict(tpl)

    except HTTPException:
        raise
    except httpx.ConnectError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Cannot reach Meta API. Check server internet connectivity.",
        )
    except Exception as e:
        db.rollback()
        log_error(
            "Template update failed",
            f"PUT /api/whatsapp/{company_id}/templates/{template_id}",
            e,
            request=request,
        )
        raise HTTPException(status_code=500, detail="Template update failed.")


# ── GET single template ───────────────────────────────────

@router.get("/{company_id}/templates/{template_name}")
def get_template(
    company_id: uuid.UUID,
    template_name: str,
    db: Session = Depends(get_db),
):
    _get_company_or_404(company_id, db)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.company_id == company_id,
        WhatsAppTemplate.name == template_name,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_to_dict(tpl)


# ── DELETE template ───────────────────────────────────────

@router.delete("/{company_id}/templates/{template_name}")
def delete_template(
    company_id: uuid.UUID,
    template_name: str,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    acc = _get_active_account(company_id, db)
    try:
        access_token = decrypt_token(acc.access_token_encrypted)
        with httpx.Client(timeout=15) as client:
            res = client.delete(
                f"{GRAPH_BASE}/{acc.waba_id}/message_templates",
                params={"name": template_name},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        # 200 = deleted, 404 = already gone — both acceptable
        if res.status_code not in (200, 404):
            detail = res.json().get("error", {}).get("message") or "Delete failed"
            raise HTTPException(status_code=400, detail=detail)

        db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.company_id == company_id,
            WhatsAppTemplate.name == template_name,
        ).delete()
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        log_error("Template delete failed", f"DELETE /api/whatsapp/{company_id}/templates/{template_name}", e, request=request)
        raise HTTPException(status_code=500, detail="Delete failed.")


# ── PATCH template mapping ─────────────────────────────────

@router.patch("/{company_id}/templates/{template_id}/mapping")
def save_template_mapping(
    company_id: uuid.UUID,
    template_id: uuid.UUID,
    payload: TemplateMappingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    """Save or update the param_mapping and cta_mapping for a template."""
    _get_company_or_404(company_id, db)
    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.company_id == company_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if payload.param_mapping is not None:
        template.param_mapping = payload.param_mapping
    if payload.cta_mapping is not None:
        template.cta_mapping = payload.cta_mapping
    if payload.mobile_mapping is not None:
        template.mobile_mapping = payload.mobile_mapping or None

    try:
        db.commit()
        db.refresh(template)
    except Exception as exc:
        db.rollback()
        log_error(
            "Template mapping save failed",
            f"PATCH /api/whatsapp/{company_id}/templates/{template_id}/mapping",
            exc,
            request=request,
        )
        raise HTTPException(status_code=500, detail="Failed to save mapping")

    return _template_to_dict(template)
