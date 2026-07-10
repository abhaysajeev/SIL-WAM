"""
Outbound status notification: enqueue logic (notify_queue) and delivery poller
(notify_scheduler). Tests call functions directly — no scheduler thread needed.
"""
from unittest.mock import MagicMock, patch

from app.models.conversation import ServiceResponse
from app.models.outbound_notification import OutboundNotification
from app.services import notify_queue, notify_scheduler
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message, make_service,
)


class TestEnqueueNotification:
    def test_no_api_key_id_is_noop(self, db):
        comp = make_company(db, code="NQ01")
        conv = make_conversation(db, comp.id, "91NQ01")
        svc = make_service(db, conv.id, comp.id, api_key_id=None)

        notify_queue.enqueue_notification(db, svc, "sent")

        assert db.query(OutboundNotification).count() == 0

    def test_api_key_without_notify_url_is_noop(self, db):
        comp = make_company(db, code="NQ02")
        conv = make_conversation(db, comp.id, "91NQ02")
        key = make_api_key(db, comp.id, key="nq02-key", notify_url=None)
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)

        notify_queue.enqueue_notification(db, svc, "sent")

        assert db.query(OutboundNotification).count() == 0

    def test_enqueues_row_with_snapshotted_notify_url(self, db):
        comp = make_company(db, code="NQ03")
        conv = make_conversation(db, comp.id, "91NQ03")
        key = make_api_key(db, comp.id, key="nq03-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id, service_id="ORD-NQ03")
        msg = make_message(db, svc, wamid="wamid.NQ03", content={"field_key": "q1"})

        notify_queue.enqueue_notification(db, svc, "delivered", message=msg)
        db.commit()

        rows = db.query(OutboundNotification).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.notify_url == "https://client.example/hook"
        assert row.status == "pending"
        assert row.message_id == msg.id
        assert row.payload["service_id"] == "ORD-NQ03"
        assert row.payload["reference_id"] == str(svc.id)
        assert row.payload["status"] == "delivered"
        assert row.payload["context"] == {"field_key": "q1"}

    def test_payload_includes_responses_so_far(self, db):
        comp = make_company(db, code="NQ04")
        conv = make_conversation(db, comp.id, "91NQ04")
        key = make_api_key(db, comp.id, key="nq04-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
        db.add(ServiceResponse(
            # Internal storage is 0-indexed (1=rating here); outbound payload
            # translates back to the client's 1-indexed convention (2=rating).
            service_id=svc.id, sequence=1, field_key="q1",
            question="Rate us?", answer_type=1, response_value="4",
        ))
        db.commit()

        notify_queue.enqueue_notification(db, svc, "sent")
        db.commit()

        row = db.query(OutboundNotification).first()
        assert row.payload["responses"] == [
            {"field_key": "q1", "answer_type": 2, "response": "4"},
        ]

    def test_includes_response_added_earlier_in_same_uncommitted_transaction(self, db):
        """
        Regression test: app's session has autoflush=False, so a ServiceResponse
        added moments earlier in the same request (e.g. the answer that triggers
        _complete_service) must still be visible to the payload query even though
        it hasn't been flushed/committed yet.
        """
        comp = make_company(db, code="NQ08")
        conv = make_conversation(db, comp.id, "91NQ08")
        key = make_api_key(db, comp.id, key="nq08-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
        make_message(db, svc, wamid="wamid.tap-nq08", direction="inbound", message_type="button")

        # Added but deliberately NOT flushed/committed before enqueueing —
        # mirrors handle_inbound's db.add(ServiceResponse(...)) immediately
        # followed by _complete_service() in the same transaction.
        db.add(ServiceResponse(
            service_id=svc.id, sequence=1, field_key="q_last",
            question="Last question?", answer_type=2, response_value="Rrrrr",
        ))

        notify_queue.enqueue_notification(db, svc, "completed")
        db.commit()

        row = db.query(OutboundNotification).first()
        assert row.payload["responses"] == [
            {"field_key": "q_last", "answer_type": 3, "response": "Rrrrr"},
        ]
        assert row.payload["respondedOn"] is not None

    def test_responded_on_null_before_any_response(self, db):
        comp = make_company(db, code="NQ06")
        conv = make_conversation(db, comp.id, "91NQ06")
        key = make_api_key(db, comp.id, key="nq06-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)

        notify_queue.enqueue_notification(db, svc, "sent")
        db.commit()

        row = db.query(OutboundNotification).first()
        assert row.payload["respondedOn"] is None
        assert row.payload["message"] is None

    def test_responded_on_stays_fixed_to_first_response(self, db):
        comp = make_company(db, code="NQ07")
        conv = make_conversation(db, comp.id, "91NQ07")
        key = make_api_key(db, comp.id, key="nq07-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
        # respondedOn is driven by the first inbound flow Message, not ServiceResponse directly.
        make_message(db, svc, wamid="wamid.tap-nq07", direction="inbound", message_type="button")
        db.add(ServiceResponse(
            service_id=svc.id, sequence=1, field_key="q1",
            question="Rate us?", answer_type=1, response_value="4",
        ))
        db.commit()

        notify_queue.enqueue_notification(db, svc, "read")
        db.commit()
        first_responded_on = db.query(OutboundNotification).order_by(
            OutboundNotification.created_at
        ).first().payload["respondedOn"]
        assert first_responded_on is not None

        # A second answer arrives later — respondedOn on a NEW notification must
        # still reflect the ORIGINAL first response, not the latest one.
        db.add(ServiceResponse(
            service_id=svc.id, sequence=2, field_key="q2",
            question="Anything else?", answer_type=2, response_value="No",
        ))
        db.commit()

        notify_queue.enqueue_notification(db, svc, "completed")
        db.commit()
        rows = db.query(OutboundNotification).order_by(OutboundNotification.created_at).all()
        assert rows[-1].payload["respondedOn"] == first_responded_on

    def test_answered_fires_repeatedly_not_deduped(self, db):
        comp = make_company(db, code="NQ09")
        conv = make_conversation(db, comp.id, "91NQ09")
        key = make_api_key(db, comp.id, key="nq09-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)

        notify_queue.enqueue_notification(db, svc, "responded")
        notify_queue.enqueue_notification(db, svc, "answered")
        notify_queue.enqueue_notification(db, svc, "answered")
        notify_queue.enqueue_notification(db, svc, "answered")
        db.commit()

        statuses = [
            r.payload["status"] for r in
            db.query(OutboundNotification).order_by(OutboundNotification.created_at).all()
        ]
        assert statuses == ["responded", "answered", "answered", "answered"]

    def test_answered_after_terminal_is_noop(self, db):
        comp = make_company(db, code="NQ10")
        conv = make_conversation(db, comp.id, "91NQ10")
        key = make_api_key(db, comp.id, key="nq10-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id, status="completed")

        notify_queue.enqueue_notification(db, svc, "answered")

        assert db.query(OutboundNotification).count() == 0

    def test_service_level_event_has_no_message_context(self, db):
        comp = make_company(db, code="NQ05")
        conv = make_conversation(db, comp.id, "91NQ05")
        key = make_api_key(db, comp.id, key="nq05-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)

        notify_queue.enqueue_notification(db, svc, "completed", note="")
        db.commit()

        row = db.query(OutboundNotification).first()
        assert row.message_id is None
        assert row.payload["context"] is None
        assert row.payload["status"] == "completed"


class TestNotifyScheduler:
    def _pending_row(self, db):
        comp = make_company(db, code="NS01")
        conv = make_conversation(db, comp.id, "91NS01")
        key = make_api_key(db, comp.id, key="ns01-key", notify_url="https://client.example/hook")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
        notify_queue.enqueue_notification(db, svc, "sent")
        db.commit()
        return db.query(OutboundNotification).first()

    def test_success_marks_delivered(self, db):
        row = self._pending_row(db)
        mock_resp = MagicMock(status_code=200)
        with patch("httpx.Client.post", return_value=mock_resp):
            claimed = notify_scheduler._send_one_pending(db)

        assert claimed is True
        db.refresh(row)
        assert row.status == "delivered"
        assert row.delivered_at is not None

    def test_http_failure_schedules_retry(self, db):
        row = self._pending_row(db)
        mock_resp = MagicMock(status_code=500, text="server error")
        with patch("httpx.Client.post", return_value=mock_resp):
            notify_scheduler._send_one_pending(db)

        db.refresh(row)
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.next_attempt_at > row.created_at

    def test_gives_up_after_max_attempts(self, db):
        row = self._pending_row(db)
        row.attempts = notify_scheduler._MAX_ATTEMPTS - 1
        db.commit()

        mock_resp = MagicMock(status_code=500, text="still failing")
        with patch("httpx.Client.post", return_value=mock_resp):
            notify_scheduler._send_one_pending(db)

        db.refresh(row)
        assert row.status == "failed"
        assert row.attempts == notify_scheduler._MAX_ATTEMPTS

    def test_no_pending_rows_returns_false(self, db):
        assert notify_scheduler._send_one_pending(db) is False
