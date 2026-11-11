"""Bodies as they are shipped: secrets masked, size capped.

A log holding passwords, API keys or live tokens is a breach waiting to happen,
so any key that names one is masked at every depth. JSON is masked structurally;
anything else (a malformed body -- exactly when someone reads the log) falls back
to masking "key": "value" pairs by pattern.
"""
import json
import re

MASK = "***"
MAX_BODY_BYTES = 16 * 1024

SECRET_PARTS = ("password", "passwd", "token", "secret", "authorization", "api_key", "apikey")
# Whole key names only: "pin" and "otp" sit inside harmless words ("mapping").
SECRET_EXACT = {"access", "refresh", "otp", "pin", "key", "x-api-key"}
_TEXT_SECRET = re.compile(
    r'("(?:[^"]*(?:password|passwd|token|secret|authorization|api_?key)[^"]*|access|refresh|otp|pin|key)"\s*:\s*)'
    r'("(?:[^"\\]|\\.)*"|[^,}\]\s]+)',
    re.IGNORECASE,
)


def is_secret(key) -> bool:
    key = str(key).lower()
    return key in SECRET_EXACT or any(part in key for part in SECRET_PARTS)


def masked(value):
    if isinstance(value, dict):
        return {k: (MASK if is_secret(k) else masked(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v) for v in value]
    return value


def body(raw: bytes) -> tuple[dict, bool]:
    """(the body as a dict for the log server, whether it was cut short)."""
    if not raw:
        return {}, False
    truncated = len(raw) > MAX_BODY_BYTES
    if not truncated:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            value = None
        else:
            value = masked(value)
            return (value if isinstance(value, dict) else {"value": value}), False
    text = raw[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
    return {"text": _TEXT_SECRET.sub(lambda m: m.group(1) + f'"{MASK}"', text)}, truncated
