"""
Lizo's response envelope.

Every response this client receives — success, validation failure, auth failure,
internal error — has the same four keys, so their parser never branches on shape:

    {"service_id": …, "reference_id": …, "status": …, "message": …}

`status` stays an enum (`in_progress` | `failed`) and `message` carries the prose.
Keeping them apart means a reworded message is never a breaking change, and the HTTP
status code remains the machine-readable signal — 409 is not retryable, 503 is.

This shape is Lizo's alone. Shirin Asal's /client-api/v1/services keeps FastAPI's
default `{"detail": …}`, which its live .NET client already consumes.
"""
import uuid
from typing import Optional

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# `status` carries the outcome as a stable code — never prose. The HTTP status still
# says whether the request succeeded and whether it is worth retrying; this says which
# specific thing happened, which HTTP alone cannot express (four different causes all
# return 422).
#
# These strings are the client's contract. Rewording a `message` is free; changing a
# value here is a breaking change.
STATUS_ACCEPTED = "in_progress"

STATUS_INVALID_API_KEY        = "invalid_api_key"
STATUS_TEMPLATE_NOT_FOUND     = "template_not_found"
STATUS_DUPLICATE_SERVICE_ID   = "duplicate_service_id"
STATUS_VALIDATION_ERROR       = "validation_error"
STATUS_MISSING_PARAMETER      = "missing_parameter"
STATUS_MISSING_MEDIA_URL      = "missing_media_url"
STATUS_TEMPLATE_NOT_CONFIGURED = "template_not_configured"
STATUS_WHATSAPP_NOT_CONFIGURED = "whatsapp_not_configured"
STATUS_INTERNAL_ERROR         = "internal_error"

# Fallback mapping for HTTPExceptions raised by the shared pipeline, which only hands
# us an HTTP status and a sentence. Liso's own checks pass their code explicitly and
# never fall through to this.
_STATUS_BY_HTTP = {
    400: STATUS_VALIDATION_ERROR,
    401: STATUS_INVALID_API_KEY,
    404: STATUS_TEMPLATE_NOT_FOUND,
    409: STATUS_DUPLICATE_SERVICE_ID,
    422: STATUS_VALIDATION_ERROR,
    503: STATUS_WHATSAPP_NOT_CONFIGURED,
}

# Pydantic prefixes messages raised by a field_validator with "Value error, ".
# Useful inside a stack trace, noise in a client-facing message.
_PYDANTIC_NOISE = "Value error, "


def status_for(http_status: int) -> str:
    """The code a bare HTTP status maps to. Anything 5xx is ours, not the client's."""
    if http_status in _STATUS_BY_HTTP:
        return _STATUS_BY_HTTP[http_status]
    return STATUS_INTERNAL_ERROR if http_status >= 500 else STATUS_VALIDATION_ERROR


class LizoOrderResponse(BaseModel):
    """The envelope. Declared as the route's response_model so it appears in /docs."""
    service_id:   Optional[str]       = None
    reference_id: Optional[uuid.UUID] = None
    status:       str
    message:      Optional[str]       = None


def success(service_id: str, reference_id: uuid.UUID) -> JSONResponse:
    """201 — the order was accepted and queued. Not delivered; see the callback."""
    return _render(201, LizoOrderResponse(
        service_id   = service_id,
        reference_id = reference_id,
        status       = STATUS_ACCEPTED,
    ))


def failure(
    http_status:  int,
    message:      str,
    status:       Optional[str]       = None,
    service_id:   Optional[str]       = None,
    reference_id: Optional[uuid.UUID] = None,
) -> JSONResponse:
    """
    Any non-2xx. The HTTP status is preserved as-is.

    `status` is the code the client branches on. Pass it wherever the caller knows
    exactly what went wrong; omit it and it is derived from the HTTP status, which is
    all we have for exceptions raised inside the shared pipeline.

    reference_id is normally None because no Service was created — the exception is
    a duplicate service_id, where it points at the order that already exists so the
    client can reconcile instead of being stuck.
    """
    return _render(http_status, LizoOrderResponse(
        service_id   = service_id,
        reference_id = reference_id,
        status       = status or status_for(http_status),
        message      = message,
    ))


def flatten_validation_errors(exc: RequestValidationError) -> str:
    """
    Render pydantic's error list as one human-readable line.

    FastAPI returns a list of per-field objects by default. Lizo's contract is that
    `message` is always a string, so several failures join with "; " rather than one
    being picked and the rest dropped.
    """
    parts = []
    for err in exc.errors():
        msg = str(err.get("msg", "")).removeprefix(_PYDANTIC_NOISE)
        # loc is ("body", "field", ...) — drop the "body" prefix, keep the path.
        loc = [str(p) for p in err.get("loc", ()) if p != "body"]
        parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid request"


def service_id_from_body(body) -> Optional[str]:
    """
    Best-effort echo of the client's own reference when the request failed to parse.

    A validation error still carries the raw body, so a missing template_name can be
    reported against the service_id it belongs to. Returns None only when the body is
    not a dict or carries no usable service_id — malformed JSON, say.
    """
    if isinstance(body, dict):
        value = body.get("service_id")
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _render(status_code: int, payload: LizoOrderResponse) -> JSONResponse:
    # jsonable_encoder turns UUID into str; JSONResponse alone would not.
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))
