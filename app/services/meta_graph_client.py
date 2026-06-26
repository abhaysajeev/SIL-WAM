"""
app/services/meta_graph_client.py — Meta Graph API client for webhook subscription management.

Uses the app access token (client_credentials flow) — no user login required.
All calls are synchronous httpx, consistent with wa_sender.py.

Raises httpx.HTTPStatusError on non-2xx responses; callers should convert to HTTPException.
"""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"
WA_OBJECT  = "whatsapp_business_account"

FIELDS_ALL = [
    "messages",
    "message_template_status_update",
    "phone_number_name_update",
    "phone_number_quality_update",
    "account_alerts",
    "account_update",
    "business_capability_update",
]

# Fields that cannot be deselected — the system depends on them.
FIELDS_REQUIRED = {"messages"}


def _get_app_token() -> str:
    """Obtain a short-lived app access token via the client_credentials OAuth flow."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            "https://graph.facebook.com/oauth/access_token",
            params={
                "client_id":     settings.FB_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "grant_type":    "client_credentials",
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


def get_subscriptions() -> dict:
    """
    Return all webhook subscriptions for this Meta App.

    Response shape:
      {"data": [{"object": "whatsapp_business_account", "callback_url": "...",
                 "active": true, "fields": [{"name": "messages", "version": "v21.0"}, ...]}]}
    """
    token = _get_app_token()
    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"{GRAPH_BASE}/{settings.FB_APP_ID}/subscriptions",
            params={"access_token": token},
        )
        resp.raise_for_status()
        return resp.json()


def get_wa_subscription() -> dict | None:
    """Return the whatsapp_business_account subscription entry, or None if not set."""
    data = get_subscriptions()
    return next(
        (s for s in data.get("data", []) if s.get("object") == WA_OBJECT),
        None,
    )


def update_subscription(callback_url: str, verify_token: str, fields: list[str]) -> dict:
    """
    Create or update the whatsapp_business_account webhook subscription.

    Sending this to an already-subscribed app updates the existing subscription.
    Meta responds with {"success": true} on success.
    """
    # Always include required fields even if caller omitted them.
    final_fields = list(set(fields) | FIELDS_REQUIRED)
    token = _get_app_token()
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            f"{GRAPH_BASE}/{settings.FB_APP_ID}/subscriptions",
            data={
                "object":       WA_OBJECT,
                "callback_url": callback_url,
                "verify_token": verify_token,
                "fields":       ",".join(final_fields),
                "access_token": token,
            },
        )
        resp.raise_for_status()
        return resp.json()


def delete_subscription() -> dict:
    """
    Remove the whatsapp_business_account webhook subscription entirely.

    Meta responds with {"success": true} on success.
    """
    token = _get_app_token()
    with httpx.Client(timeout=15) as client:
        resp = client.delete(
            f"{GRAPH_BASE}/{settings.FB_APP_ID}/subscriptions",
            params={
                "object":       WA_OBJECT,
                "access_token": token,
            },
        )
        resp.raise_for_status()
        return resp.json()
