"""
Firebase Cloud Messaging (HTTP v1) — waking the agent app.

**A push carries no message, only "sync this thread".** The payload is data-only:
`{"type": "sync", "thread_id": …, "rev": …}`. The app then calls
GET /threads/{id}/messages?changed_after_rev=… for the actual content. So a lost or late
push costs a few seconds, never a message, and no customer text sits on Google's servers.

Priority (Android):
  * a new customer message → "high": delivered through Doze immediately. FCM demotes
    high-priority pushes that never produce a visible notification, so the app MUST show
    one for these.
  * a status change only (✓✓, read) → "normal", collapse_key = thread_id: only the
    latest per thread is kept while the phone is asleep.

Authentication is the service account's own OAuth flow: a JWT signed with its private
key is exchanged for an access token (cached ~55 minutes). Implemented with python-jose,
already a dependency, rather than adding Google's SDK for two HTTP calls.
"""
import json
import logging
import threading
import time

import httpx
from jose import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# Outcomes of one send.
OK, UNREGISTERED, FAILED = "ok", "unregistered", "failed"


class _Fcm:
    def __init__(self, sa: dict):
        self.sa = sa
        self.project_id = sa["project_id"]
        self._token: str | None = None
        self._token_exp = 0.0
        self._lock = threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 300:
                return self._token
            now = int(time.time())
            assertion = jwt.encode(
                {"iss": self.sa["client_email"], "scope": _SCOPE, "aud": _TOKEN_URL,
                 "iat": now, "exp": now + 3600},
                self.sa["private_key"], algorithm="RS256")
            res = httpx.post(_TOKEN_URL, timeout=15, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion})
            res.raise_for_status()
            body = res.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3600))
            return self._token

    def send(self, token: str, data: dict, *, high: bool, collapse_key: str | None) -> str:
        android = {"priority": "HIGH" if high else "NORMAL"}
        if collapse_key and not high:
            android["collapse_key"] = collapse_key[:64]
        body = {"message": {"token": token,
                            "data": {k: str(v) for k, v in data.items()},
                            "android": android}}
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        for attempt in range(3):
            try:
                res = httpx.post(url, json=body, timeout=10,
                                 headers={"Authorization": f"Bearer {self._access_token()}"})
            except Exception as exc:
                logger.warning("FCM send error: %s", exc)
                time.sleep(0.5 * (attempt + 1))
                continue
            if res.status_code == 200:
                return OK
            status = ""
            try:
                err = res.json().get("error", {})
                status = err.get("status", "")
                for d in err.get("details", []) or []:
                    status = d.get("errorCode", status)
            except Exception:
                pass
            # The token is dead (app uninstalled, data cleared, token rotated).
            if res.status_code == 404 or status in ("UNREGISTERED",):
                return UNREGISTERED
            if res.status_code == 400 and status == "INVALID_ARGUMENT":
                return UNREGISTERED
            if res.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.warning("FCM send failed %s: %s", res.status_code, res.text[:200])
            return FAILED
        return FAILED


_client: _Fcm | None = None
_loaded = False


def client() -> _Fcm | None:
    """The FCM client, or None when FCM_SERVICE_ACCOUNT_FILE is not configured."""
    global _client, _loaded
    if not _loaded:
        _loaded = True
        path = settings.FCM_SERVICE_ACCOUNT_FILE
        if path:
            try:
                with open(path) as fh:
                    _client = _Fcm(json.load(fh))
            except Exception as exc:
                logger.error("FCM disabled — cannot load %s: %s", path, exc)
    return _client


def set_client(c) -> None:
    """Tests replace the client with a fake."""
    global _client, _loaded
    _client, _loaded = c, True
