"""
The route class that gives Lizo's endpoint its own error rendering.

Two of Lizo's responses are produced *before* the route function body runs, so a
try/except inside the endpoint cannot see them:

  * 401 — raised by Depends(get_api_key_and_company)
  * 422 — raised while pydantic parses LizoOrderRequest

FastAPI resolves dependencies and parses the body inside the handler returned by
APIRoute.get_route_handler(), so wrapping that one call catches everything this
router can emit. Scoping it to the router — rather than registering a global
exception handler that tests request.url.path — keeps Lizo's contract inside
app/lizo/ and leaves app/main.py as stock FastAPI for every other route.
"""
import logging

from fastapi import Request, Response
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.routing import APIRoute

from app.lizo.responses import (
    STATUS_INTERNAL_ERROR, STATUS_VALIDATION_ERROR,
    failure, flatten_validation_errors, service_id_from_body,
)
from app.utils.error_logger import log_error

logger = logging.getLogger(__name__)


class LizoRoute(APIRoute):
    """Renders every outcome of a Lizo route as the four-key envelope."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def envelope_handler(request: Request) -> Response:
            try:
                return await original_handler(request)

            except RequestValidationError as exc:
                # A malformed payload — bad customer_mobile, a reserved key, a missing
                # required field. exc.body is the raw payload, so the client's own
                # reference can still be echoed even though it never parsed cleanly.
                return failure(
                    422,
                    flatten_validation_errors(exc),
                    status=STATUS_VALIDATION_ERROR,
                    service_id=service_id_from_body(exc.body),
                )

            except HTTPException as exc:
                # Everything the shared pipeline raises — 401/404/409/503/500. It hands
                # us an HTTP status and a sentence, so the code is derived from the
                # status; Liso's own checks pass theirs explicitly and never land here.
                return failure(
                    exc.status_code,
                    str(exc.detail),
                    service_id=await _service_id_from_request(request),
                )

            except Exception as exc:
                # Without this branch an unhandled error falls through to
                # main.py's global handler, which returns {"success", "message",
                # "error_id"} — a third shape this endpoint must never emit.
                error_id = log_error(
                    "Unhandled error on Lizo ingest",
                    f"{request.method} {request.url.path}",
                    exc,
                    request=request,
                )
                logger.exception("Lizo ingest failed unexpectedly (error_id=%s)", error_id)
                return failure(
                    500,
                    "Internal error during service creation",
                    status=STATUS_INTERNAL_ERROR,
                    service_id=await _service_id_from_request(request),
                )

        return envelope_handler


async def _service_id_from_request(request: Request) -> str | None:
    """
    Read service_id back off the request for errors raised outside body validation.

    Starlette caches the body once read, so this neither consumes the stream the
    endpoint already used nor fails when the endpoint never ran — a 401 rejected by
    the API-key dependency still echoes the client's reference. Returns None only
    when there is nothing parseable to echo, e.g. malformed JSON.
    """
    try:
        return service_id_from_body(await request.json())
    except Exception:
        return None
