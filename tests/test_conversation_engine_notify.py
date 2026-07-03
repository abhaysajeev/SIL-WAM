"""
conversation_engine.handle_inbound — "responded" notification on first CTA tap.
conversation_engine.handle_status — message-status notification suppression
once a service is terminal.
"""
from unittest.mock import patch

from app.models.outbound_notification import OutboundNotification
from app.services.conversation_engine import handle_inbound, handle_status
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message, make_queue_entry,
    make_service, make_wa_account,
)

_MOCK_SEND = SendResult(ok=True, meta_message_id="wamid.q1sent", error=None)

_TEMPLATE_WAMID = "wamid.template1"

_QUESTIONS = [
    {"sequence": 1, "field_key": "q1", "question": "Happy?", "answer_type": 0,
     "options": ["Yes", "No"], "sent": 0},
    {"sequence": 2, "field_key": "q2", "question": "Rate us", "answer_type": 1,
     "rating_scale": 5, "sent": 0},
]


def _button_tap_msg(wamid="wamid.tap1", context_wamid=_TEMPLATE_WAMID):
    # Real Meta button-tap payloads always carry context.id pointing at the
    # template message that was tapped — required for the concurrency-safe resolver.
    return {
        "id": wamid,
        "from": "919999900001",
        "type": "button",
        "button": {"text": "Feedback", "payload": "Feedback"},
        "context": {"id": context_wamid},
    }


class TestRespondedNotification:
    def _setup(self, db):
        comp = make_company(db, code="CENOTIFY")
        key = make_api_key(db, comp.id, key="ce-notify-key", notify_url="https://client.example/hook")
        conv = make_conversation(db, comp.id, "919999900001")
        account = make_wa_account(db, comp.id)
        svc = make_service(
            db, conv.id, comp.id, api_key_id=key.id,
            questions=[dict(q) for q in _QUESTIONS],
        )
        make_queue_entry(db, svc, mobile_no="919999900001", status="in_progress")
        make_message(db, svc, wamid=_TEMPLATE_WAMID, message_type="template")
        return svc, account

    def test_first_button_tap_fires_responded(self, db):
        svc, account = self._setup(db)
        with patch("app.services.wa_sender.send_interactive_buttons", return_value=_MOCK_SEND):
            handle_inbound(db, account, _button_tap_msg())

        rows = db.query(OutboundNotification).all()
        statuses = [r.payload["status"] for r in rows]
        assert "responded" in statuses
        assert statuses.count("responded") == 1

    def test_responded_payload_has_null_message_and_button_context(self, db):
        svc, account = self._setup(db)
        with patch("app.services.wa_sender.send_interactive_buttons", return_value=_MOCK_SEND):
            handle_inbound(db, account, _button_tap_msg())

        row = next(r for r in db.query(OutboundNotification).all() if r.payload["status"] == "responded")
        assert row.payload["message"] is None
        assert row.payload["context"]["type"] == "button"
        assert row.payload["respondedOn"] is not None

    def test_second_button_tap_after_flow_started_does_not_refire(self, db):
        svc, account = self._setup(db)
        results = [
            SendResult(ok=True, meta_message_id="wamid.q1sent-a", error=None),
            SendResult(ok=True, meta_message_id="wamid.q1sent-b", error=None),
        ]
        with patch("app.services.wa_sender.send_interactive_buttons", side_effect=results):
            handle_inbound(db, account, _button_tap_msg("wamid.tap1"))
            # Customer taps the still-clickable template button again — flow already started.
            handle_inbound(db, account, _button_tap_msg("wamid.tap2"))

        rows = db.query(OutboundNotification).all()
        statuses = [r.payload["status"] for r in rows]
        assert statuses.count("responded") == 1


class TestHandleStatusSuppressionAfterTerminal:
    def _setup(self, db, service_status, unique="1"):
        comp = make_company(db, code=f"CESTATUS{unique}")
        key = make_api_key(db, comp.id, key=f"ce-status-key-{unique}", notify_url="https://client.example/hook")
        conv = make_conversation(db, comp.id, f"91999990000{unique}")
        account = make_wa_account(db, comp.id, phone_number_id=f"phone-{unique}")
        svc = make_service(db, conv.id, comp.id, api_key_id=key.id, status=service_status)
        msg = make_message(db, svc, wamid=f"wamid.status-target-{unique}", message_type="text")
        return svc, account, msg

    def test_in_progress_service_still_notifies(self, db):
        svc, account, msg = self._setup(db, "in_progress")

        handle_status(db, {"id": msg.wamid, "status": "read", "timestamp": "1783077431"}, account)

        rows = db.query(OutboundNotification).all()
        assert len(rows) == 1
        assert rows[0].payload["status"] == "read"

    def test_completed_service_suppresses_trailing_status(self, db):
        svc, account, msg = self._setup(db, "completed")

        handle_status(db, {"id": msg.wamid, "status": "read", "timestamp": "1783077431"}, account)

        # msg.status itself still updates — only the outbound notification is suppressed.
        assert msg.status == "read"
        assert db.query(OutboundNotification).count() == 0

    def test_expired_and_failed_services_also_suppress(self, db):
        for i, status in enumerate(("expired", "failed"), start=2):
            svc, account, msg = self._setup(db, status, unique=str(i))
            handle_status(db, {"id": msg.wamid, "status": "sent", "timestamp": "1783077431"}, account)
            assert db.query(OutboundNotification).count() == 0
