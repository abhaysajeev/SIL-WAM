"""
Concurrent services per mobile number: enqueue_service no longer pre-empts,
context.id-based reply routing, free-text serialization (hold/release), and
order-number footer/body injection.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.models.conversation import MobileQueue, Message, Service, ServiceResponse
from app.services import conversation_engine, expiry_scheduler, queue_manager, wa_sender
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message,
    make_queue_entry, make_service, make_wa_account,
)

_SEND_OK = SendResult(ok=True, meta_message_id="wamid.ok", error=None)


def _button_msg(context_wamid, choice_id="q1_opt0", title="Yes", from_no="919000000001"):
    return {
        "id": f"wamid.reply-{choice_id}",
        "from": from_no,
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": choice_id, "title": title}},
        "context": {"id": context_wamid},
    }


def _text_msg(body="some free text", from_no="919000000001", context_wamid=None):
    msg = {"id": f"wamid.text-{body[:6]}", "from": from_no, "type": "text", "text": {"body": body}}
    if context_wamid:
        msg["context"] = {"id": context_wamid}
    return msg


class TestNoPreemption:
    def test_second_service_does_not_expire_first(self, db):
        comp = make_company(db, code="CS01")
        conv = make_conversation(db, comp.id, "919000000001")
        account = make_wa_account(db, comp.id)
        svc_a = make_service(db, conv.id, comp.id, service_id="A1", status="in_progress")
        make_queue_entry(db, svc_a, mobile_no="919000000001", status="in_progress")
        svc_b = make_service(db, conv.id, comp.id, service_id="B1", status="waiting", template_sent=False)
        svc_b.data = {"customer_mobile": "919000000001"}
        db.commit()

        result = queue_manager.enqueue_service(db, svc_b, account)
        db.commit()

        assert result == "in_progress"
        db.refresh(svc_a)
        db.refresh(svc_b)
        assert svc_a.status == "in_progress"  # NOT expired
        assert svc_b.status == "in_progress"
        rows = db.query(MobileQueue).filter(MobileQueue.mobile_no == "919000000001").all()
        in_progress_rows = [r for r in rows if r.status == "in_progress"]
        assert len(in_progress_rows) == 2


class TestContextRouting:
    def _two_active_services(self, db):
        comp = make_company(db, code="CS02")
        conv = make_conversation(db, comp.id, "919000000002")
        account = make_wa_account(db, comp.id)
        questions = [{"sequence": 1, "field_key": "q1", "question": "Happy?", "answer_type": 0,
                      "options": ["Yes", "No"], "sent": 0, "dispatched": 1}]
        svc_a = make_service(db, conv.id, comp.id, service_id="A2", status="in_progress",
                              questions=[dict(q) for q in questions], mobile_no="919000000002")
        svc_b = make_service(db, conv.id, comp.id, service_id="B2", status="in_progress",
                              questions=[dict(q) for q in questions], mobile_no="919000000002")
        make_queue_entry(db, svc_a, mobile_no="919000000002", status="in_progress")
        make_queue_entry(db, svc_b, mobile_no="919000000002", status="in_progress")
        msg_a = make_message(db, svc_a, wamid="wamid.qa", message_type="interactive",
                              content={"sequence": 1, "field_key": "q1", "answer_type": 0})
        msg_b = make_message(db, svc_b, wamid="wamid.qb", message_type="interactive",
                              content={"sequence": 1, "field_key": "q1", "answer_type": 0})
        return conv, account, svc_a, svc_b, msg_a, msg_b

    def test_button_reply_routes_to_correct_service(self, db):
        conv, account, svc_a, svc_b, msg_a, msg_b = self._two_active_services(db)

        conversation_engine.handle_inbound(db, account, _button_msg(msg_b.wamid))

        resp_b = db.query(ServiceResponse).filter(ServiceResponse.service_id == svc_b.id).all()
        resp_a = db.query(ServiceResponse).filter(ServiceResponse.service_id == svc_a.id).all()
        assert len(resp_b) == 1
        assert len(resp_a) == 0


class TestFreeTextSerialization:
    def _service_with_free_text_outstanding(self, db, conv, comp, service_id, mobile_no):
        questions = [{"sequence": 1, "field_key": "q_ft", "question": "Comments?",
                      "answer_type": 2, "sent": 0, "dispatched": 1}]
        svc = make_service(db, conv.id, comp.id, service_id=service_id, status="in_progress",
                            questions=questions, mobile_no=mobile_no)
        make_queue_entry(db, svc, mobile_no=mobile_no, status="in_progress")
        return svc

    def test_second_free_text_question_is_held(self, db):
        comp = make_company(db, code="CS03")
        conv = make_conversation(db, comp.id, "919000000003")
        account = make_wa_account(db, comp.id)
        svc_a = self._service_with_free_text_outstanding(db, conv, comp, "A3", "919000000003")

        svc_b = make_service(
            db, conv.id, comp.id, service_id="B3", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q_ft2", "question": "Anything else?",
                        "answer_type": 2, "sent": 0, "dispatched": 0}],
            mobile_no="919000000003",
        )
        make_queue_entry(db, svc_b, mobile_no="919000000003", status="in_progress")

        with patch("app.services.wa_sender.send_text") as mock_send:
            conversation_engine._fire_next_question(db, svc_b, account, "919000000003")

        mock_send.assert_not_called()
        db.refresh(svc_b)
        assert svc_b.questions[0]["dispatched"] == 0

    def test_free_text_fires_when_nothing_outstanding(self, db):
        comp = make_company(db, code="CS04")
        conv = make_conversation(db, comp.id, "919000000004")
        account = make_wa_account(db, comp.id)
        svc = make_service(
            db, conv.id, comp.id, service_id="A4", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q1", "question": "Comments?",
                        "answer_type": 2, "sent": 0, "dispatched": 0}],
            mobile_no="919000000004",
        )
        make_queue_entry(db, svc, mobile_no="919000000004", status="in_progress")

        with patch("app.services.wa_sender.send_text", return_value=_SEND_OK) as mock_send:
            conversation_engine._fire_next_question(db, svc, account, "919000000004")

        mock_send.assert_called_once()
        # _fire_next_question doesn't commit (caller's job) — check the in-memory
        # mutation directly rather than db.refresh(), which would discard it.
        assert svc.questions[0]["dispatched"] == 1

    def test_release_on_completion_fires_held_question(self, db):
        comp = make_company(db, code="CS05")
        conv = make_conversation(db, comp.id, "919000000005")
        account = make_wa_account(db, comp.id)
        svc_a = make_service(
            db, conv.id, comp.id, service_id="A5", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q_a", "question": "Comments?",
                        "answer_type": 2, "sent": 0, "dispatched": 1}],
            mobile_no="919000000005",
        )
        qe_a = make_queue_entry(db, svc_a, mobile_no="919000000005", status="in_progress")
        svc_b = make_service(
            db, conv.id, comp.id, service_id="B5", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q_b", "question": "Feedback?",
                        "answer_type": 2, "sent": 0, "dispatched": 0}],
            mobile_no="919000000005",
        )
        make_queue_entry(db, svc_b, mobile_no="919000000005", status="in_progress")

        with patch("app.services.wa_sender.send_text", return_value=_SEND_OK) as mock_send:
            conversation_engine._complete_service(db, svc_a, qe_a, account)

        mock_send.assert_called_once()
        # _complete_service doesn't commit (caller's job) — check in-memory state.
        assert svc_b.questions[0]["dispatched"] == 1

    def test_release_on_expiry_fires_held_question(self, db):
        comp = make_company(db, code="CS06")
        conv = make_conversation(db, comp.id, "919000000006")
        make_wa_account(db, comp.id)
        past = datetime.now(timezone.utc) - timedelta(hours=48)
        svc_a = make_service(
            db, conv.id, comp.id, service_id="A6", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q_a", "question": "Comments?",
                        "answer_type": 2, "sent": 0, "dispatched": 1}],
            mobile_no="919000000006", created_at=past, template_expiry_hours=24,
        )
        svc_a.data = {"customer_mobile": "919000000006"}
        db.commit()
        make_queue_entry(db, svc_a, mobile_no="919000000006", status="in_progress")
        svc_b = make_service(
            db, conv.id, comp.id, service_id="B6", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q_b", "question": "Feedback?",
                        "answer_type": 2, "sent": 0, "dispatched": 0}],
            mobile_no="919000000006",
        )
        make_queue_entry(db, svc_b, mobile_no="919000000006", status="in_progress")

        with patch("app.services.wa_sender.send_text", return_value=_SEND_OK) as mock_send:
            # Use the test's own db session directly — run_once_now() opens a fresh
            # SessionLocal() bound to the real dev DB, not the test DB (matches the
            # convention already established in tests/test_expiry_scheduler.py).
            expiry_scheduler._expire_timed_out_services(db)

        mock_send.assert_called_once()
        assert svc_b.questions[0]["dispatched"] == 1


class TestFooterAndBodyAppend:
    def test_button_question_includes_footer(self, db):
        comp = make_company(db, code="CS07")
        conv = make_conversation(db, comp.id, "919000000007")
        account = make_wa_account(db, comp.id)
        svc = make_service(
            db, conv.id, comp.id, service_id="ORD-FOOTER-1", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q1", "question": "Happy?",
                        "answer_type": 0, "options": ["Yes", "No"], "sent": 0, "dispatched": 0}],
            mobile_no="919000000007",
        )
        make_queue_entry(db, svc, mobile_no="919000000007", status="in_progress")

        with patch("app.services.wa_sender.send_interactive_buttons", return_value=_SEND_OK) as mock_send:
            conversation_engine._fire_next_question(db, svc, account, "919000000007")

        _, kwargs = mock_send.call_args
        assert kwargs.get("footer") == "ORD-FOOTER-1"

    def test_free_text_question_appends_order_to_body(self, db):
        comp = make_company(db, code="CS08")
        conv = make_conversation(db, comp.id, "919000000008")
        account = make_wa_account(db, comp.id)
        svc = make_service(
            db, conv.id, comp.id, service_id="ORD-BODY-1", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q1", "question": "Comments?",
                        "answer_type": 2, "sent": 0, "dispatched": 0}],
            mobile_no="919000000008",
        )
        make_queue_entry(db, svc, mobile_no="919000000008", status="in_progress")

        with patch("app.services.wa_sender.send_text", return_value=_SEND_OK) as mock_send:
            conversation_engine._fire_next_question(db, svc, account, "919000000008")

        args, _ = mock_send.call_args
        assert "ORD-BODY-1" in args[2]


class TestOutOfFlowFallback:
    def test_text_with_no_context_and_nothing_awaiting_is_out_of_flow(self, db):
        comp = make_company(db, code="CS09")
        conv = make_conversation(db, comp.id, "919000000009")
        account = make_wa_account(db, comp.id)
        make_service(
            db, conv.id, comp.id, service_id="A9", status="completed",
            questions=None, mobile_no="919000000009",
        )

        conversation_engine.handle_inbound(db, account, _text_msg("random hello", from_no="919000000009"))

        inbound = db.query(Message).filter(Message.direction == "inbound").order_by(
            Message.created_at.desc()
        ).first()
        assert inbound.service_id is None
        assert db.query(ServiceResponse).count() == 0


class TestDuplicateReplyAfterCompletion:
    """
    Regression test for a production crash: WhatsApp buttons stay clickable
    forever, so a customer re-tapping an already-answered button produces a
    genuinely new wamid (not caught by step-1 dedup) that still resolves via
    context.id to the same, now-completed service. handle_inbound must not
    crash, and must not re-send the completion message or re-fire notifications.
    """

    def test_retap_after_completion_does_not_crash_or_duplicate_side_effects(self, db):
        comp = make_company(db, code="CS10")
        conv = make_conversation(db, comp.id, "919000000010")
        account = make_wa_account(db, comp.id)
        svc = make_service(
            db, conv.id, comp.id, service_id="ORD-DUP-1", status="in_progress",
            questions=[{"sequence": 1, "field_key": "q1", "question": "Happy?",
                        "answer_type": 0, "options": ["Yes", "No"], "sent": 0, "dispatched": 1}],
            mobile_no="919000000010",
        )
        svc.data = dict(svc.data or {}, completion_message="Thanks!")
        db.commit()
        make_queue_entry(db, svc, mobile_no="919000000010", status="in_progress")
        q1_msg = make_message(db, svc, wamid="wamid.q1-orig", message_type="interactive",
                               content={"sequence": 1, "field_key": "q1", "answer_type": 0})

        first_reply = {
            "id": "wamid.reply-first", "from": "919000000010", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "q1_opt0", "title": "Yes"}},
            "context": {"id": q1_msg.wamid},
        }
        with patch("app.services.wa_sender.send_text", return_value=_SEND_OK) as mock_text:
            conversation_engine.handle_inbound(db, account, first_reply)

        db.commit()
        assert svc.status == "completed"
        assert mock_text.call_count == 1  # completion message sent once

        # Customer re-taps the same (now stale) button — different wamid, same context target.
        retap_reply = {
            "id": "wamid.reply-retap", "from": "919000000010", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "q1_opt0", "title": "Yes"}},
            "context": {"id": q1_msg.wamid},
        }
        with patch("app.services.wa_sender.send_text", return_value=_SEND_OK) as mock_text2:
            conversation_engine.handle_inbound(db, account, retap_reply)  # must not raise

        assert svc.status == "completed"
        mock_text2.assert_not_called()  # idempotent — no duplicate completion message
