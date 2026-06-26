"""
Optional Redis client — used as a fast-path dedup layer for wamids.
If REDIS_URL is not configured, all calls return None and the engine
falls back to the DB unique-index check.
"""
import logging

logger = logging.getLogger(__name__)

_client = None
_initialised = False


def get_redis():
    """Return a Redis client or None if Redis is not configured / unreachable."""
    global _client, _initialised
    if _initialised:
        return _client

    _initialised = True
    from app.core.config import settings

    if not settings.REDIS_URL:
        return None

    try:
        import redis as redis_lib
        _client = redis_lib.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        _client.ping()
        logger.info("Redis connected: %s", settings.REDIS_URL)
    except Exception as exc:
        logger.warning("Redis unavailable — wamid dedup will use DB only: %s", exc)
        _client = None

    return _client
