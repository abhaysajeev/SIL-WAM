"""
Module 6 — Reliability hardening.

6A: _store_failed_webhook writes a FailedWebhook row with correct fields.
6B: meta_webhook exception handler calls _store_failed_webhook.
"""
from unittest.mock import MagicMock, patch

from app.api.meta_webhook import _process_payload_bg, _store_failed_webhook
from app.models.failed_webhook import FailedWebhook


class TestStoreFailedWebhook:
    def test_writes_failed_webhook_row(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook({"key": "value"}, ValueError("boom"))

        mock_session.add.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert isinstance(row, FailedWebhook)

    def test_source_is_meta(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook({}, RuntimeError("err"))
        row = mock_session.add.call_args[0][0]
        assert row.source == "meta"

    def test_error_type_is_exception_class_name(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook({}, TypeError("type problem"))
        row = mock_session.add.call_args[0][0]
        assert row.error_type == "TypeError"

    def test_raw_payload_stored(self, db):
        mock_session = MagicMock()
        payload = {"entry": [{"id": "123"}]}
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook(payload, Exception("x"))
        row = mock_session.add.call_args[0][0]
        assert row.raw_payload == payload

    def test_traceback_populated(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            try:
                raise ValueError("deliberate")
            except ValueError as exc:
                _store_failed_webhook({}, exc)
        row = mock_session.add.call_args[0][0]
        assert row.traceback is not None
        assert "ValueError" in row.traceback

    def test_replayed_defaults_false(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook({}, Exception("x"))
        row = mock_session.add.call_args[0][0]
        assert row.replayed is False

    def test_commit_called(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook({}, Exception("x"))
        mock_session.commit.assert_called_once()

    def test_session_closed_on_success(self, db):
        mock_session = MagicMock()
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            _store_failed_webhook({}, Exception("x"))
        mock_session.close.assert_called_once()

    def test_session_closed_even_if_commit_fails(self, db):
        mock_session = MagicMock()
        mock_session.commit.side_effect = Exception("db gone")
        with patch("app.api.meta_webhook.SessionLocal", return_value=mock_session):
            # Should not raise — errors are swallowed
            _store_failed_webhook({}, Exception("x"))
        mock_session.close.assert_called_once()


class TestProcessPayloadBgExceptionHandler:
    def test_store_failed_webhook_called_on_crash(self, db):
        body = {"entry": []}

        with patch("app.api.meta_webhook._process_payload", side_effect=RuntimeError("crash")):
            with patch("app.api.meta_webhook._store_failed_webhook") as mock_store:
                with patch("app.api.meta_webhook.SessionLocal", return_value=MagicMock()):
                    with patch("app.api.meta_webhook.log_error"):
                        _process_payload_bg(body)

        mock_store.assert_called_once()
        call_args = mock_store.call_args
        assert call_args[0][0] == body
        assert isinstance(call_args[0][1], RuntimeError)

    def test_log_error_also_called_on_crash(self, db):
        with patch("app.api.meta_webhook._process_payload", side_effect=ValueError("oops")):
            with patch("app.api.meta_webhook._store_failed_webhook"):
                with patch("app.api.meta_webhook.SessionLocal", return_value=MagicMock()):
                    with patch("app.api.meta_webhook.log_error") as mock_log:
                        _process_payload_bg({})

        mock_log.assert_called_once()

    def test_no_exception_means_store_not_called(self, db):
        with patch("app.api.meta_webhook._process_payload"):
            with patch("app.api.meta_webhook._store_failed_webhook") as mock_store:
                with patch("app.api.meta_webhook.SessionLocal", return_value=MagicMock()):
                    _process_payload_bg({"entry": []})

        mock_store.assert_not_called()
