"""Capturing /client-api, /agent-api and /webhook traffic for the log server.

A pure ASGI middleware, not BaseHTTPMiddleware: that one buffers streaming
responses and re-wraps the request, and the Meta webhook's signature check needs
the request body byte for byte. Here `receive` is only wrapped to *copy* chunks
as they pass, and `send` to copy the status and the start of the response body.
The handler sees exactly what it would have seen without this.

Auth dependencies call tag() to add who made the call (company, agent). They run
in a worker thread with a copy of this request's context, so the dict they add to
is the same one built here.

Never raises into a request: any failure building the log only loses the log.
"""
import contextvars
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from app.core.config import settings
from app.monitoring import redact, shipper

log = logging.getLogger("app.monitoring")

# Path prefix -> the `api` value the dashboard filters on. Order matters: the
# first match wins, and every /webhook/* other than Meta's is logged in full.
CAPTURED = (
    ("/client-api/", "client"),
    ("/agent-api/", "agent"),
    ("/webhook/meta", "webhook"),
    ("/webhook/", "webhook"),
)
MOBILE_KEYS = ("customer_mobile", "mobile_no", "mobile", "to", "phone")

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("log_capture", default=None)


def tag(**fields) -> None:
    """Add caller details to the request being captured. No-op otherwise."""
    current = _current.get()
    if current is not None:
        current.update({k: v for k, v in fields.items() if v})


def _api_for(path: str) -> str | None:
    for prefix, api in CAPTURED:
        if path.startswith(prefix):
            return api
    return None


class LogCaptureMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not shipper.enabled():
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        api = _api_for(path)
        if api is None:
            return await self.app(scope, receive, send)

        started = time.perf_counter()
        request_body = bytearray()
        response_body = bytearray()
        response = {"status": 500, "content_type": ""}
        tags: dict = {}
        token = _current.set(tags)

        async def receive_copy():
            message = await receive()
            if message["type"] == "http.request" and len(request_body) <= redact.MAX_BODY_BYTES:
                request_body.extend(message.get("body", b""))
            return message

        async def send_copy(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        response["content_type"] = value.decode("latin-1")
            elif message["type"] == "http.response.body":
                ctype = response["content_type"]
                if ("json" in ctype or ctype.startswith("text/")) and len(response_body) <= redact.MAX_BODY_BYTES:
                    response_body.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive_copy, send_copy)
        finally:
            _current.reset(token)
            try:
                duration_ms = int((time.perf_counter() - started) * 1000)
                shipper.offer(build_document(scope, api, path, bytes(request_body), response,
                                             bytes(response_body), duration_ms, tags))
            except Exception:                   # noqa: BLE001 -- losing a log must never fail a request
                log.debug("could not build a log document", exc_info=True)


def build_document(scope, api, path, request_raw, response, response_raw, duration_ms, tags) -> dict:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    ip = (headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (scope.get("client") or ("",))[0] or "")

    if path.startswith("/webhook/meta"):
        form_data, request_truncated = meta_summary(request_raw), len(request_raw) > redact.MAX_BODY_BYTES
    else:
        form_data, request_truncated = redact.body(request_raw)
    response_json, response_truncated = redact.body(response_raw)

    query = redact.masked(dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"))))
    company = tags.get("company", "")
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "environment": settings.LOG_ENVIRONMENT or None,
        "api": api,
        "method": scope.get("method", ""),
        "path": path,
        "query": query or None,
        "status": response["status"],
        "duration_ms": duration_ms,
        "ip": ip,
        "user_agent": headers.get("user-agent", "")[:300],
        "company": company,
        "agent": tags.get("agent", ""),
        "mobile": find_mobile(form_data),
        "error": error_text(response["status"], response_json),
        "response": response_json,
        "request_truncated": request_truncated,
        "response_truncated": response_truncated,
    }
    return {
        "device_id": company or (f"ip:{ip}" if ip else "unknown"),
        "form_data": form_data,
        "log": {k: v for k, v in record.items() if v is not None},
    }


def find_mobile(value, depth: int = 0) -> str:
    """The first customer number in a body, up to three levels deep."""
    if depth > 3:
        return ""
    if isinstance(value, dict):
        for key in MOBILE_KEYS:
            found = value.get(key)
            if isinstance(found, (str, int)) and str(found).strip():
                return str(found).strip().lstrip("+")
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return ""
    for child in children:
        found = find_mobile(child, depth + 1)
        if found:
            return found
    return ""


def error_text(status: int, response_json: dict) -> str:
    """A one-line reason for a failed call, from FastAPI's `detail`."""
    if status < 400:
        return ""
    detail = response_json.get("detail") or response_json.get("error") or response_json.get("message")
    if isinstance(detail, list):        # a 422: one entry per bad field
        parts = []
        for item in detail:
            if isinstance(item, dict):
                where = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                parts.append(f"{where}: {item.get('msg', '')}".strip(": "))
        detail = "; ".join(parts)
    elif isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code") or json.dumps(detail, default=str)
    return str(detail or "")[:500]


def meta_summary(raw: bytes) -> dict:
    """What a Meta webhook carried, without any message content."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"unparsed_bytes": len(raw)}
    messages, statuses, numbers = [], [], []
    for entry in payload.get("entry", []) if isinstance(payload, dict) else []:
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}
            number = (value.get("metadata") or {}).get("phone_number_id")
            if number and number not in numbers:
                numbers.append(number)
            for m in value.get("messages", []) or []:
                messages.append({"from": m.get("from"), "id": m.get("id"),
                                 "type": m.get("type"), "timestamp": m.get("timestamp")})
            for s in value.get("statuses", []) or []:
                statuses.append({
                    "id": s.get("id"), "status": s.get("status"),
                    "recipient_id": s.get("recipient_id"), "timestamp": s.get("timestamp"),
                    "errors": [{"code": e.get("code"), "title": e.get("title")}
                               for e in s.get("errors", []) or []] or None,
                })
    summary = {"object": payload.get("object") if isinstance(payload, dict) else None,
               "phone_number_ids": numbers, "messages": messages, "statuses": statuses}
    first = (messages or statuses or [{}])[0]
    summary["mobile"] = first.get("from") or first.get("recipient_id")
    return summary
