"""
Module 8 — SSE real-time stream.

Tests cover:
  - Auth gate: missing/invalid token rejected
  - Valid token: endpoint responds with text/event-stream
  - Company scoping wired correctly
"""
from unittest.mock import AsyncMock, MagicMock, patch


async def _one_heartbeat(request, company_id, cursor):
    yield ": heartbeat\n\n"


class TestSSEAuthGate:
    def test_missing_token_returns_422(self, client):
        r = client.get("/api/stream")
        assert r.status_code == 422

    def test_invalid_token_returns_401(self, client):
        with patch("app.api.sse_api._authenticate", return_value=None):
            r = client.get("/api/stream?token=bad-jwt-token")
        assert r.status_code == 401

    def test_invalid_token_error_message(self, client):
        with patch("app.api.sse_api._authenticate", return_value=None):
            r = client.get("/api/stream?token=garbage")
        assert r.status_code == 401
        assert "Unauthorized" in r.json().get("error", "")


class TestSSEValidStream:
    def _mock_user(self, company_id=None):
        u = MagicMock()
        u.company_id = company_id
        u.is_active = True
        return u

    def test_valid_token_returns_200(self, client):
        with patch("app.api.sse_api._authenticate", return_value=self._mock_user()):
            with patch("app.api.sse_api._stream", _one_heartbeat):
                r = client.get("/api/stream?token=valid-mock-token")
        assert r.status_code == 200

    def test_response_content_type_is_event_stream(self, client):
        with patch("app.api.sse_api._authenticate", return_value=self._mock_user()):
            with patch("app.api.sse_api._stream", _one_heartbeat):
                r = client.get("/api/stream?token=valid-mock-token")
        assert "text/event-stream" in r.headers.get("content-type", "")

    def test_response_contains_heartbeat(self, client):
        with patch("app.api.sse_api._authenticate", return_value=self._mock_user()):
            with patch("app.api.sse_api._stream", _one_heartbeat):
                r = client.get("/api/stream?token=valid-mock-token")
        assert "heartbeat" in r.text

    def test_no_cache_header_set(self, client):
        with patch("app.api.sse_api._authenticate", return_value=self._mock_user()):
            with patch("app.api.sse_api._stream", _one_heartbeat):
                r = client.get("/api/stream?token=valid-mock-token")
        assert r.headers.get("cache-control") == "no-cache"

    def test_company_scoped_user_company_id_passed(self, client):
        import uuid
        cid = uuid.uuid4()
        user = self._mock_user(company_id=cid)

        captured = {}

        async def capture_stream(request, company_id, cursor):
            captured["company_id"] = company_id
            yield ": heartbeat\n\n"

        with patch("app.api.sse_api._authenticate", return_value=user):
            with patch("app.api.sse_api._stream", capture_stream):
                client.get("/api/stream?token=valid-mock-token")

        assert captured.get("company_id") == str(cid)

    def test_admin_user_company_id_is_none(self, client):
        user = self._mock_user(company_id=None)  # admin — sees all

        captured = {}

        async def capture_stream(request, company_id, cursor):
            captured["company_id"] = company_id
            yield ": heartbeat\n\n"

        with patch("app.api.sse_api._authenticate", return_value=user):
            with patch("app.api.sse_api._stream", capture_stream):
                client.get("/api/stream?token=valid-mock-token")

        assert captured.get("company_id") is None

    def test_cursor_param_forwarded_to_stream(self, client):
        user = self._mock_user()
        captured = {}

        async def capture_stream(request, company_id, cursor):
            captured["cursor"] = cursor
            yield ": heartbeat\n\n"

        with patch("app.api.sse_api._authenticate", return_value=user):
            with patch("app.api.sse_api._stream", capture_stream):
                client.get("/api/stream?token=valid&cursor=2026-06-01T00:00:00+00:00")

        assert captured.get("cursor") is not None


class TestSSEAuthenticate:
    def test_empty_token_returns_none(self):
        from app.api.sse_api import _authenticate
        result = _authenticate("")
        assert result is None

    def test_garbage_token_returns_none(self):
        from app.api.sse_api import _authenticate
        result = _authenticate("not-a-jwt-at-all")
        assert result is None
