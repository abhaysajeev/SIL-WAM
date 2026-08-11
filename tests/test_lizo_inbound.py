"""
Lizo template-button handling — View Order / Confirm Order.

The most important test here is TestIsolation: it drives an SFA/Shirin-shaped
service through the same entry point and asserts the existing behaviour is
untouched. Everything else can be rewritten; that one is the reason the guard
exists.
"""
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.lizo import inbound as lizo_inbound
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE
from app.models.conversation import Message
from app.models.outbound_notification import OutboundNotification
from app.services import conversation_engine
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message,
    make_queue_entry, make_service, make_wa_account,
)

_MOCK_SEND = SendResult(ok=True, meta_message_id="wamid.lizo.reply", error=None)
_TEMPLATE_WAMID = "wamid.lizo.template"
_MOBILE = "919876543210"

_LIZO_DATA = {
    "customer_mobile": _MOBILE,
    FLOW_MARKER_KEY: FLOW_MARKER_VALUE,
    "customer_name": "Ravi Kumar",
    "order_no": "10234",
    "order_date": "30/07/2026",
    "items": {"item_1": {"item": "A", "qty": 2}, "item_2": {"item": "B", "qty": 1}},
    "summary": {"subtotal": "1499.00", "discount": "150.00",
                "gst": "45.00", "net_amount": "1394.00"},
}

_QUESTIONS = [
    {"sequence": 1, "field_key": "q1", "question": "Happy?", "answer_type": 0, "sent": 0},
]


def _tap(payload, wamid="wamid.tap1", context_wamid=_TEMPLATE_WAMID):
    """A real Meta template quick-reply tap always carries context.id."""
    return {
        "id": wamid,
        "from": _MOBILE,
        "type": "button",
        "button": {"text": payload, "payload": payload},
        "context": {"id": context_wamid},
    }


def _setup(db, *, data=None, questions=None, code="LIZOIN"):
    comp = make_company(db, code=code)
    key = make_api_key(db, comp.id, key=f"key-{code}", notify_url="https://client.example/hook")
    conv = make_conversation(db, comp.id, _MOBILE)
    account = make_wa_account(db, comp.id)
    svc = make_service(db, conv.id, comp.id, api_key_id=key.id, questions=questions)
    svc.data = data if data is not None else dict(_LIZO_DATA)
    if questions is None:
        svc.status = "completed"        # what queue_manager does for template-only
    db.commit()
    make_queue_entry(db, svc, mobile_no=_MOBILE,
                     status="completed" if questions is None else "in_progress")
    # The outbound template the tap replies to — this is what context.id resolves
    # through. Marked as the confirmation template: confirming is the second tap of
    # the two-step flow, and content["template_name"] is how inbound.py tells the two
    # "Confirm Order" buttons apart. See test_lizo_confirm.py for the first step.
    make_message(db, svc, wamid=_TEMPLATE_WAMID,
                 direction="outbound", message_type="template",
                 content={"template_name": settings.LIZO_CONFIRM_TEMPLATE})
    db.commit()
    return comp, account, svc


# ── The regression guard ──────────────────────────────────────────────────────

class TestIsolation:
    """An SFA/Shirin-shaped service must behave exactly as it did before."""

    def test_handles_is_false_without_the_marker(self, db):
        _comp, _account, svc = _setup(db, data={"customer_mobile": _MOBILE},
                                      questions=[dict(q) for q in _QUESTIONS], code="SFACTL")
        assert lizo_inbound.handles(svc) is False

    def test_handles_is_false_for_empty_data(self, db):
        _comp, _account, svc = _setup(db, data={}, questions=[dict(q) for q in _QUESTIONS],
                                      code="SFAEMP")
        assert lizo_inbound.handles(svc) is False

    def test_shirin_tap_still_fires_responded_and_next_question(self, db):
        """The existing path: responded notification + next question dispatched."""
        _comp, account, svc = _setup(db, data={"customer_mobile": _MOBILE},
                                     questions=[dict(q) for q in _QUESTIONS], code="SFAFLOW")
        with patch("app.services.wa_sender.send_interactive_buttons",
                   return_value=_MOCK_SEND) as send_q, \
             patch("app.lizo.inbound.handle_tap") as lizo_tap:
            conversation_engine.handle_inbound(db, account, _tap("Feedback"))

        lizo_tap.assert_not_called()
        assert send_q.called, "next question was not dispatched"
        statuses = [n.payload["status"] for n in
                    db.query(OutboundNotification).filter(
                        OutboundNotification.service_id == svc.id).all()]
        assert "responded" in statuses


# ── Confirm Order ─────────────────────────────────────────────────────────────

class TestConfirmOrder:
    def test_marks_confirmed_and_posts_once(self, db):
        _comp, account, svc = _setup(db)
        with patch("app.lizo.inbound._post_confirmation") as post:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order"))
        db.refresh(svc)
        assert svc.data.get("lizo_confirmed_at")
        assert post.call_count == 1

    def test_retap_does_not_repost(self, db):
        """Buttons stay tappable forever and each tap has a fresh wamid."""
        _comp, account, svc = _setup(db)
        with patch("app.lizo.inbound._post_confirmation") as post:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", wamid="c1"))
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", wamid="c2"))
        assert post.call_count == 1

    def test_confirmed_at_survives_the_commit(self, db):
        """Guards the flag_modified call — JSONB mutations are otherwise dropped."""
        _comp, account, svc = _setup(db)
        with patch("app.lizo.inbound._post_confirmation"):
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order"))
        db.expire_all()
        reloaded = db.query(type(svc)).filter_by(id=svc.id).first()
        assert reloaded.data.get("lizo_confirmed_at")


# ── Dispatch ──────────────────────────────────────────────────────────────────

class TestDispatch:
    def test_unknown_button_sends_nothing(self, db):
        _comp, account, _svc = _setup(db)
        with patch("app.services.wa_sender.send_text", return_value=_MOCK_SEND) as send, \
             patch("app.lizo.inbound._post_confirmation") as post:
            conversation_engine.handle_inbound(db, account, _tap("Track Shipment"))
        assert not send.called
        assert not post.called

    @pytest.mark.parametrize("payload", ["View Order", "view_order", "VIEW ORDER"])
    def test_view_order_is_now_inert(self, db, payload):
        """
        The button is gone from the template, but messages sent before that change
        still carry it and stay tappable forever. A tap must be a no-op, not a crash
        and not a stale summary.
        """
        _comp, account, _svc = _setup(db)
        with patch("app.services.wa_sender.send_text", return_value=_MOCK_SEND) as send, \
             patch("app.lizo.inbound._post_confirmation") as post:
            conversation_engine.handle_inbound(db, account, _tap(payload))
        assert not send.called
        assert not post.called
