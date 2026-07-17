"""
Full-page (redirect) variant of the Meta / WhatsApp Embedded Signup flow.

Why this exists: the JS-SDK popup (FB.login) binds its fallback OAuth code to
Facebook's internal per-session xd_arbiter redirect target, which our server
can never present at exchange time — those codes always fail with OAuth
subcode 36008. The only usable popup artifact is the WA_EMBEDDED_SIGNUP
message event, which Facebook does not reliably emit. This flow avoids the
SDK entirely: the browser is sent to the OAuth dialog with OUR redirect_uri,
the code comes back here, and the exchange presents that identical
redirect_uri. Any refusal by Meta to render the signup wizard is visible
full-page instead of silently degrading to a plain login inside a popup.

Requires <public-base>/meta/oauth/callback to be listed verbatim in the Meta
app's Facebook Login → Valid OAuth Redirect URIs.

Usage: the "Connect via Meta" button opens /meta/oauth/start/<company_id> in a
popup window; the callback notifies the opener via postMessage and closes the
popup. Direct full-page navigation to the same URL also works.
"""
import html
import json
import logging
import secrets
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.whatsapp_api import GRAPH_BASE, _finalize_meta_connection
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_page_user
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta-oauth-pages"])


def _public_base(request: Request) -> str:
    """Base URL as the browser sees it — honours ngrok/proxy forwarding headers."""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"{scheme}://{host}"


def _redirect_uri(request: Request) -> str:
    return f"{_public_base(request)}/meta/oauth/callback"


def _result_page(title: str, detail: str, company_id: str = None) -> HTMLResponse:
    """Error page shown inside the signup popup. Notifies the opener page (so it
    can reset its button) but stays open so the user can read the detail."""
    back = f"/companies/{company_id}#whatsapp" if company_id else "/companies"
    msg = json.dumps({"type": "META_OAUTH_RESULT", "ok": False, "title": title})
    return HTMLResponse(
        "<div style='font-family:sans-serif;max-width:720px;margin:60px auto'>"
        f"<h2>{html.escape(title)}</h2>"
        "<pre style='white-space:pre-wrap;background:#f6f6f6;padding:16px;"
        f"border-radius:8px'>{html.escape(detail)}</pre>"
        "<p><a href='#' onclick='window.close();return false'>Close window</a>"
        f" &nbsp;|&nbsp; <a href='{back}'>&larr; Back to company page</a></p></div>"
        "<script>if (window.opener) {"
        f"try {{ window.opener.postMessage({msg}, window.location.origin); }} catch (e) {{}}"
        "}</script>"
    )


def _success_close_page(company_id: str) -> HTMLResponse:
    """Success page: tell the opener the connection is done, then close the popup.
    Falls back to a full-page redirect when opened outside a popup."""
    back = f"/companies/{company_id}#whatsapp"
    msg = json.dumps({"type": "META_OAUTH_RESULT", "ok": True, "company_id": company_id})
    return HTMLResponse(
        "<div style='font-family:sans-serif;max-width:720px;margin:60px auto'>"
        "<h2>WhatsApp account connected</h2>"
        "<p>You can close this window.</p></div>"
        "<script>"
        "if (window.opener) {"
        f"  try {{ window.opener.postMessage({msg}, window.location.origin); }} catch (e) {{}}"
        "  window.close();"
        "} else {"
        f"  window.location.href = {json.dumps(back)};"
        "}"
        "</script>"
    )


def _discover_waba_id(access_token: str) -> str:
    """
    WABA id via /debug_token granular_scopes — the reliable discovery path for
    tokens minted by a Business Login config (where /me/whatsapp_business_accounts
    is not available). Returns None if nothing was granted.
    """
    app_token = f"{settings.FB_APP_ID}|{settings.META_APP_SECRET}"
    with httpx.Client(timeout=15) as client:
        dbg = client.get(
            f"{GRAPH_BASE}/debug_token",
            params={"input_token": access_token, "access_token": app_token},
        )
    if dbg.status_code != 200:
        logger.warning("debug_token failed: %s", dbg.text)
        return None
    scopes = dbg.json().get("data", {}).get("granular_scopes")
    logger.info("Meta oauth token granular_scopes: %s", scopes)
    for gs in scopes or []:
        if gs.get("scope") in ("whatsapp_business_management", "whatsapp_business_messaging"):
            ids = gs.get("target_ids") or []
            if ids:
                return ids[0]
    return None


@router.get("/meta/oauth/start/{company_id}")
def meta_oauth_start(company_id: uuid.UUID, request: Request, ctx=Depends(get_page_user)):
    if not ctx["perms"].get("companies", {}).get("write"):
        return HTMLResponse("<h2>Access denied</h2>", status_code=403)
    if not (settings.FB_APP_ID and settings.META_APP_SECRET and settings.META_CONFIG_ID):
        return _result_page(
            "Meta credentials not configured",
            "FB_APP_ID, META_APP_SECRET and META_CONFIG_ID must all be set in .env.",
            str(company_id),
        )

    nonce = secrets.token_urlsafe(16)
    request.session["meta_oauth"] = {"company_id": str(company_id), "nonce": nonce}

    params = {
        "client_id":                      settings.FB_APP_ID,
        "config_id":                      settings.META_CONFIG_ID,
        "response_type":                  "code",
        "override_default_response_type": "true",
        "redirect_uri":                   _redirect_uri(request),
        "state":                          nonce,
        "extras":                         json.dumps({"setup": {}, "sessionInfoVersion": "3"}),
    }
    return RedirectResponse(
        "https://www.facebook.com/v22.0/dialog/oauth?" + urllib.parse.urlencode(params)
    )


@router.get("/meta/oauth/callback")
def meta_oauth_callback(
    request: Request,
    ctx=Depends(get_page_user),
    db: Session = Depends(get_db),
):
    saved = request.session.pop("meta_oauth", None) or {}
    company_id = saved.get("company_id")
    q = request.query_params

    if q.get("error"):
        # Meta states its refusal reason here — the visibility the popup flow lacks.
        return _result_page(
            "Meta returned an error",
            f"{q.get('error')}\n{q.get('error_reason', '')}\n{q.get('error_description', '')}",
            company_id,
        )
    if not company_id or not q.get("code") or q.get("state") != saved.get("nonce"):
        return _result_page(
            "Invalid callback",
            "Missing code or state mismatch — start again from /meta/oauth/start/<company_id>.",
            company_id,
        )

    waba_id = None
    try:
        with httpx.Client(timeout=15) as client:
            token_res = client.get(
                f"{GRAPH_BASE}/oauth/access_token",
                # Must be byte-identical to the redirect_uri sent at dialog time.
                params={
                    "client_id":     settings.FB_APP_ID,
                    "client_secret": settings.META_APP_SECRET,
                    "redirect_uri":  _redirect_uri(request),
                    "code":          q["code"],
                },
            )
        if token_res.status_code != 200:
            return _result_page("Token exchange failed", token_res.text, company_id)
        access_token = token_res.json().get("access_token")
        if not access_token:
            return _result_page("Token exchange failed", "No access_token in Meta response.", company_id)

        waba_id = _discover_waba_id(access_token)
        token_expiry = datetime.now(timezone.utc) + timedelta(days=60)
        _finalize_meta_connection(
            db, uuid.UUID(company_id), access_token, token_expiry, waba_id, None
        )
    except HTTPException as exc:
        return _result_page(
            "Connection failed",
            f"{exc.detail}\n\nWABA granted by this login: {waba_id or 'none resolved'}",
            company_id,
        )
    except Exception as exc:
        db.rollback()
        log_error(
            "Meta redirect-flow connection failed",
            "GET /meta/oauth/callback",
            exc,
            request=request,
        )
        return _result_page("Connection failed", repr(exc), company_id)

    return _success_close_page(company_id)
