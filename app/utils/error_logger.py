"""Crash-safe error logger. Never raises — silently swallows its own failures."""
import logging
import traceback as tb_module
from typing import Any, Optional

from fastapi import Request

from app.core.database import SessionLocal
from app.models.error_log import ErrorLog

_logger = logging.getLogger(__name__)

_SENSITIVE = frozenset([
    "password", "token", "secret", "card", "cvv", "ssn",
    "auth", "authorization", "access_token", "refresh_token", "api_key",
])


def _sanitize(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            k: "[REDACTED]" if any(s in k.lower() for s in _SENSITIVE) else _sanitize(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_sanitize(item) for item in data]
    return data


def _get_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def log_error(
    title: str,
    method: str,
    error: BaseException,
    request: Optional[Request] = None,
    request_data: Optional[dict] = None,
    user: Optional[str] = None,
) -> Optional[int]:
    """
    Log an error to the error_log table. Never raises.

    Args:
        title:        Short description (e.g. "User create failed").
        method:       HTTP method + path (e.g. "POST /api/users/").
        error:        The exception instance.
        request:      FastAPI Request, used to extract IP address.
        request_data: Pre-extracted and sanitized request body dict (caller provides this).
        user:         Username or user ID string for attribution.

    Returns:
        The new error_log.id on success, None on failure.
    """
    try:
        traceback_str = tb_module.format_exc()
        # format_exc() returns "NoneType: None" if no active exception — use repr instead
        if traceback_str.strip() == "NoneType: None":
            traceback_str = "".join(tb_module.format_exception(type(error), error, error.__traceback__))

        sanitized_data = _sanitize(request_data) if request_data else None
        ip = _get_ip(request) if request else None

        db = SessionLocal()
        try:
            log = ErrorLog(
                title=title,
                method=method,
                error_type=type(error).__name__,
                traceback=traceback_str,
                request_data=sanitized_data,
                user=user,
                ip_address=ip,
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            return log.id
        finally:
            db.close()

    except Exception:
        _logger.exception("log_error() failed — could not write to error_log table")
        return None
