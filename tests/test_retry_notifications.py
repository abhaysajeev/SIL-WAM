"""
Regression: a retried service must still notify the client.

A service that fails with whatsapp_number_invalid enqueues a "failed" notification
(rank 6). The client then retries with a corrected number and the service runs to
completion — but notify_queue's monotonic-progression guard compares every new
status against the highest rank *ever* enqueued for that service, and the old
"failed" row is still there. Everything at rank <= 6 is therefore suppressed, which
is every status except "answered".

Net effect: after a successful retry the client is told nothing at all — no sent,
no delivered, no completed. They are left believing the service is still failed.
"""
from app.models.outbound_notification import OutboundNotification
from app.services import notify_queue

from .conftest import make_api_key, make_company, make_conversation, make_service


def _statuses(db, service_id):
    rows = (
        db.query(OutboundNotification.payload)
        .filter(OutboundNotification.service_id == service_id)
        .all()
    )
    return [p["status"] for (p,) in rows]


def _setup(db, code, key_str, mobile, sid):
    comp = make_company(db, name="Retry Co", code=code)
    key = make_api_key(db, comp.id, key=key_str, notify_url="http://example.test/cb")
    conv = make_conversation(db, comp.id, mobile_no=mobile)
    svc = make_service(
        db, conv.id, comp.id,
        service_id=sid,
        status="in_progress",
        api_key_id=key.id,
    )
    return comp, svc


class TestRetryNotifications:
    def test_failed_then_retried_service_still_notifies_completion(self, db):
        """The client must learn the retry succeeded."""
        _, svc = _setup(db, "RETRY1", "retry-key-1", "919000000001", "RETRY-001")

        # 1. First attempt fails on an invalid number.
        svc.status = "failed"
        svc.failed_reason = "whatsapp_number_invalid"
        notify_queue.enqueue_notification(db, svc, "failed", note="whatsapp_number_invalid")
        db.flush()
        assert _statuses(db, svc.id) == ["failed"]

        # 2. Client retries with a corrected number. Mirrors the reset in
        #    client_services_api.retry_service, including the attempt_no bump
        #    that opens a new notification generation.
        svc.status = "waiting"
        svc.failed_reason = None
        svc.template_sent = False
        svc.send_attempts = 0
        svc.attempt_no = (svc.attempt_no or 0) + 1
        db.flush()

        # 3. Second attempt runs through to completion.
        svc.status = "in_progress"
        notify_queue.enqueue_notification(db, svc, "sent")
        svc.status = "completed"
        notify_queue.enqueue_notification(db, svc, "completed")
        db.flush()

        got = _statuses(db, svc.id)
        assert "completed" in got, (
            "Client was never told the retry succeeded. "
            f"Notifications enqueued: {got}"
        )

    def test_retry_reports_progress_not_just_terminal_state(self, db):
        """'sent' after a retry is new information, not a backwards step."""
        _, svc = _setup(db, "RETRY2", "retry-key-2", "919000000002", "RETRY-002")

        svc.status = "failed"
        svc.failed_reason = "whatsapp_number_invalid"
        notify_queue.enqueue_notification(db, svc, "failed", note="whatsapp_number_invalid")
        db.flush()

        svc.status = "in_progress"
        svc.failed_reason = None
        svc.attempt_no = (svc.attempt_no or 0) + 1
        notify_queue.enqueue_notification(db, svc, "sent")
        db.flush()

        assert "sent" in _statuses(db, svc.id), (
            "Retry produced no 'sent' notification — the client cannot tell the "
            "resend even happened."
        )
