"""
GET  /api/webhook-config/status       — fetch live subscription state from Meta Graph API
POST /api/webhook-config/update       — create or update the webhook subscription on Meta
DELETE /api/webhook-config/subscription — remove the webhook subscription from Meta

All routes are super_admin only. verify_token is always sourced from server settings
(META_WEBHOOK_VERIFY_TOKEN) so the Meta subscription always matches what meta_webhook.py validates.
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.core.deps import require_super_admin
from app.services import meta_graph_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhook-config", tags=["Webhook Config"])


class WebhookUpdateRequest(BaseModel):
    callback_url: str
    fields: list[str]


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status(_user=Depends(require_super_admin)):
    """Fetch the current whatsapp_business_account webhook subscription from Meta."""
    _require_meta_config()
    try:
        raw  = meta_graph_client.get_subscriptions()
        subs = raw.get("data", [])
        wa   = next((s for s in subs if s.get("object") == meta_graph_client.WA_OBJECT), None)
        return {
            "subscription":      wa,
            "all_subscriptions": subs,
        }
    except httpx.HTTPStatusError as exc:
        _raise_meta_error(exc)
    except Exception as exc:
        logger.error("webhook-config status error: %s", exc)
        raise HTTPException(502, f"Meta API unreachable: {exc}")


# ── Update ────────────────────────────────────────────────────────────────────

@router.post("/update")
def update_subscription(
    payload: WebhookUpdateRequest,
    _user=Depends(require_super_admin),
):
    """
    Create or update the whatsapp_business_account webhook subscription on Meta.

    verify_token is always taken from settings.META_WEBHOOK_VERIFY_TOKEN — not from the
    request body — so the Meta subscription always matches what meta_webhook.py validates.
    """
    _require_meta_config()
    if not payload.callback_url.startswith("https://"):
        raise HTTPException(400, "callback_url must use HTTPS.")
    if not settings.META_WEBHOOK_VERIFY_TOKEN:
        raise HTTPException(400, "META_WEBHOOK_VERIFY_TOKEN is not configured in .env.")

    try:
        result = meta_graph_client.update_subscription(
            callback_url  = payload.callback_url,
            verify_token  = settings.META_WEBHOOK_VERIFY_TOKEN,
            fields        = payload.fields,
        )
        return result
    except httpx.HTTPStatusError as exc:
        _raise_meta_error(exc)
    except Exception as exc:
        logger.error("webhook-config update error: %s", exc)
        raise HTTPException(502, f"Meta API unreachable: {exc}")


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/subscription")
def delete_subscription(_user=Depends(require_super_admin)):
    """Remove the whatsapp_business_account webhook subscription from Meta entirely."""
    _require_meta_config()
    try:
        result = meta_graph_client.delete_subscription()
        return result
    except httpx.HTTPStatusError as exc:
        _raise_meta_error(exc)
    except Exception as exc:
        logger.error("webhook-config delete error: %s", exc)
        raise HTTPException(502, f"Meta API unreachable: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_meta_config():
    missing = []
    if not settings.FB_APP_ID:
        missing.append("FB_APP_ID")
    if not settings.META_APP_SECRET:
        missing.append("META_APP_SECRET")
    if missing:
        raise HTTPException(
            503,
            f"Meta App not configured. Missing .env keys: {', '.join(missing)}",
        )


def _raise_meta_error(exc: httpx.HTTPStatusError):
    try:
        detail = exc.response.json()
        msg    = (detail.get("error") or {}).get("message") or str(detail)
    except Exception:
        msg = exc.response.text or str(exc)
    raise HTTPException(502, f"Meta API error: {msg}")
