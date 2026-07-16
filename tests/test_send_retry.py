"""
Automatic retry for send_error template-send failures (queue_manager /
send_scheduler). whatsapp_number_invalid must never auto-retry — only a
client-submitted corrected number can retry that one, via the retry endpoint.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models.conversation import MobileQueue
from app.models.outbound_notification import OutboundNotification
from app.services import queue_manager, send_scheduler
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_queue_entry,
    make_service, make_wa_account, make_wa_template,
)

_FAIL_GENERIC = SendResult(ok=False, meta_message_id=None, error="503 Service Unavailable")
_FAIL_INVALID_NUMBER = SendResult(ok=False, meta_message_id=None, error="(#131026) message undeliverable")
_OK = SendResult(ok=True, meta_message_id="wamid.sent1", error=None)


def _setup(db, code, mobile_no="919876500001", questions=None):
    comp = make_company(db, code=code)
    key = make_api_key(db, comp.id, key=f"{code}-key", notify_url="https://client.example/hook")
    conv = make_conversation(db, comp.id, mobile_no)
    account = make_wa_account(db, comp.id)
    template = make_wa_template(db, comp.id)
    svc = make_service(
        db, conv.id, comp.id, status="in_progress", template_sent=False,
        mobile_no=mobile_no, api_key_id=key.id, questions=questions,
    )
    svc.template_id = template.id
    db.commit()
    qe = make_queue_entry(db, svc, mobile_no=mobile_no, status="in_progress")
    return svc, account, qe


class TestSendErrorSchedulesRetry:
    def test_first_failure_schedules_retry_not_terminal(self, db):
        svc, account, qe = _setup(db, "SR01")

        with patch("app.services.wa_sender.send_template", return_value=_FAIL_GENERIC):
            queue_manager.send_template_for_service(db, svc, account)
        db.commit()

        assert svc.send_attempts == 1
        assert svc.status == "in_progress"          # not terminal yet
        assert svc.template_sent is False            # re-enters the claim pool
        assert svc.failed_reason == "send_error"
        assert svc.next_retry_at is not None
        delay = (svc.next_retry_at - datetime.now(timezone.utc)).total_seconds()
        assert 25 <= delay <= 35                      # ~30s backoff

        db.refresh(qe)
        assert qe.status == "in_progress"             # queue entry untouched
        assert db.query(OutboundNotification).count() == 0   # no client-facing noise yet

    def test_second_failure_uses_longer_backoff(self, db):
        svc, account, qe = _setup(db, "SR02")
        svc.send_attempts = 1  # simulate first attempt already failed
        db.commit()

        with patch("app.services.wa_sender.send_template", return_value=_FAIL_GENERIC):
            queue_manager.send_template_for_service(db, svc, account)
        db.commit()

        assert svc.send_attempts == 2
        assert svc.status == "in_progress"
        delay = (svc.next_retry_at - datetime.now(timezone.utc)).total_seconds()
        assert 115 <= delay <= 125                     # ~2min backoff

    def test_third_failure_is_terminal(self, db):
        svc, account, qe = _setup(db, "SR03")
        svc.send_attempts = 2  # simulate two prior failed attempts
        db.commit()

        with patch("app.services.wa_sender.send_template", return_value=_FAIL_GENERIC):
            queue_manager.send_template_for_service(db, svc, account)
        db.commit()

        assert svc.send_attempts == 3
        assert svc.status == "failed"
        assert svc.failed_reason == "send_error"
        assert svc.next_retry_at is None               # no further retry scheduled

        db.refresh(qe)
        assert qe.status == "completed"

        rows = db.query(OutboundNotification).all()
        assert len(rows) == 1
        assert rows[0].payload["status"] == "failed"


class TestInvalidNumberNeverRetries:
    def test_invalid_number_fails_immediately_on_first_attempt(self, db):
        svc, account, qe = _setup(db, "SR04")

        with patch("app.services.wa_sender.send_template", return_value=_FAIL_INVALID_NUMBER):
            queue_manager.send_template_for_service(db, svc, account)
        db.commit()

        assert svc.send_attempts == 1
        assert svc.status == "failed"                  # terminal on first failure, no retry
        assert svc.failed_reason == "whatsapp_number_invalid"
        assert svc.next_retry_at is None

        db.refresh(qe)
        assert qe.status == "completed"


class TestSendSchedulerClaimQuery:
    def test_service_with_future_retry_not_claimed_yet(self, db):
        svc_future, account, _ = _setup(db, "SR05", mobile_no="919876500005")
        svc_future.next_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()

        with patch("app.services.wa_sender.send_template", return_value=_FAIL_GENERIC):
            claimed = send_scheduler._send_one_pending(db)

        assert claimed is False   # nothing eligible right now
        db.refresh(svc_future)
        assert svc_future.send_attempts == 0
        assert svc_future.template_sent is False

    def test_service_with_elapsed_retry_is_claimed(self, db):
        svc_ready, account, _ = _setup(db, "SR06", mobile_no="919876500006")
        svc_ready.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        svc_ready.send_attempts = 1
        db.commit()

        with patch("app.services.wa_sender.send_template", return_value=_OK):
            claimed = send_scheduler._send_one_pending(db)

        assert claimed is True
        db.refresh(svc_ready)
        assert svc_ready.send_attempts == 2
        assert svc_ready.template_sent is True

    def test_no_account_configured_schedules_retry_not_terminal_failure(self, db):
        comp = make_company(db, code="SR07")
        key = make_api_key(db, comp.id, key="sr07-key")
        conv = make_conversation(db, comp.id, "919876500007")
        template = make_wa_template(db, comp.id)
        svc = make_service(
            db, conv.id, comp.id, status="in_progress", template_sent=False,
            mobile_no="919876500007", api_key_id=key.id,
        )
        svc.template_id = template.id
        db.commit()
        make_queue_entry(db, svc, mobile_no="919876500007", status="in_progress")
        # Deliberately no make_wa_account(...) — simulates the account-not-found path.

        claimed = send_scheduler._send_one_pending(db)

        assert claimed is True
        db.refresh(svc)
        assert svc.send_attempts == 1
        assert svc.status == "in_progress"    # retry scheduled, not immediately terminal
        assert svc.template_sent is False
        assert svc.next_retry_at is not None
