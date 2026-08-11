"""
Liso's two-step confirmation.

    liso_with_image    [Confirm Order]           → send order_confirm_liso, nothing else
    order_confirm_liso [Confirm Order] [Cancel]  → approve, or cancel

Both templates carry a button labelled "Confirm Order", and both taps resolve to the
same Service, so the only thing separating them is which template the tap replies to.

The test that matters most is
TestFirstTap::test_nothing_is_reported_to_sfa — if the routing ever regresses, the
first tap silently approves the order again and the confirmation step is a fiction.
"""
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE
from app.models.conversation import Message
from app.models.outbound_notification import OutboundNotification
from app.models.whatsapp import WhatsAppTemplate
from app.services import conversation_engine
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message, make_queue_entry,
    make_service, make_wa_account, make_wa_template,
)

_MOBILE = "919876543210"
_ORDER_WAMID   = "wamid.liso.order"      # the liso_with_image message
_CONFIRM_WAMID = "wamid.liso.confirm"    # the order_confirm_liso message
_APPROVE_URL = "http://sfa.example/api/Sfa/ApproveOrder"
_STATUS_URL  = "http://sfa.example/api/Sfa/SaveWhatsAppOrderStatus"

_ORDER_TEMPLATE = "liso_with_image"

_DATA = {
    "customer_mobile": _MOBILE,
    FLOW_MARKER_KEY: FLOW_MARKER_VALUE,
    "customer_name": "GIANT BAZAAR, Pattom",
    "order_no": "26OS02LC00007",
    "UserID": "1024",
    "CompanyID": 5,
}

_SENT = SendResult(ok=True, meta_message_id=_CONFIRM_WAMID, error=None)
_TEXT_SENT = SendResult(ok=True, meta_message_id="wamid.liso.ack", error=None)
_FAILED = SendResult(ok=False, meta_message_id=None, error="503 Service Unavailable")


@pytest.fixture(autouse=True)
def approve_url(monkeypatch):
    monkeypatch.setattr(settings, "LIZO_APPROVE_ORDER_URL", _APPROVE_URL)


def _setup(db, *, code="LSCONF", with_confirm_template=True):
    comp = make_company(db, code=code)
    key = make_api_key(db, comp.id, key=f"key-{code}", notify_url=_STATUS_URL)
    conv = make_conversation(db, comp.id, _MOBILE)
    account = make_wa_account(db, comp.id)
    if with_confirm_template:
        make_wa_template(db, comp.id, name=settings.LIZO_CONFIRM_TEMPLATE)
    svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
    svc.data = dict(_DATA)
    svc.status = "completed"          # template-only orders complete at send
    db.commit()
    make_queue_entry(db, svc, mobile_no=_MOBILE, status="completed")
    make_message(db, svc, wamid=_ORDER_WAMID, direction="outbound",
                 message_type="template", content={"template_name": _ORDER_TEMPLATE})
    db.commit()
    return comp, account, svc


def _confirm_message(db, svc):
    """Stand in for a confirmation template that has already been sent."""
    make_message(db, svc, wamid=_CONFIRM_WAMID, direction="outbound",
                 message_type="template",
                 content={"template_name": settings.LIZO_CONFIRM_TEMPLATE})
    db.commit()


def _tap(payload, context_wamid, wamid="wamid.tap1"):
    return {
        "id": wamid, "from": _MOBILE, "type": "button",
        "button": {"text": payload, "payload": payload},
        "context": {"id": context_wamid},
    }


def _rows(db, svc, url=None):
    q = db.query(OutboundNotification).filter(OutboundNotification.service_id == svc.id)
    if url:
        q = q.filter(OutboundNotification.notify_url == url)
    return q.order_by(OutboundNotification.created_at).all()


def _texts(db, svc):
    return [m.content.get("body") for m in db.query(Message).filter(
        Message.service_id == svc.id, Message.message_type == "text").all()]


class TestFirstTap:
    """Confirm on the order message sends the confirmation template — and only that."""

    def test_the_confirmation_template_is_sent(self, db):
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_SENT) as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID))

        assert send.called
        assert send.call_args.args[1].name == settings.LIZO_CONFIRM_TEMPLATE
        assert send.call_args.args[3] == _MOBILE

    def test_nothing_is_reported_to_sfa(self, db):
        # The whole point of the step: the first tap decides nothing. A regression
        # here re-approves on tap one and the confirmation becomes decorative.
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID))

        assert _rows(db, svc) == []
        db.refresh(svc)
        assert svc.data.get("lizo_confirmed_at") is None

    def test_the_outbound_message_records_the_template_name(self, db):
        # Without it the next tap cannot be attributed and the order can never be
        # approved — this row is load-bearing, not bookkeeping.
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID))

        msg = db.query(Message).filter(Message.wamid == _CONFIRM_WAMID).first()
        assert msg is not None
        assert msg.content["template_name"] == settings.LIZO_CONFIRM_TEMPLATE

    def test_a_re_tap_does_not_send_it_twice(self, db):
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_SENT) as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID, "t1"))
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID, "t2"))
        assert send.call_count == 1

    def test_a_failed_send_leaves_it_retryable(self, db):
        # No marker stamped, so tapping again tries again rather than stranding the
        # order with no route to confirmation.
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_FAILED):
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID))
        db.refresh(svc)
        assert svc.data.get("liso_confirm_sent_at") is None

    def test_a_missing_confirmation_template_is_not_fatal(self, db):
        _c, account, svc = _setup(db, with_confirm_template=False)
        with patch("app.services.wa_sender.send_template") as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID))
        assert not send.called
        assert _rows(db, svc) == []


class TestSecondTapConfirm:
    def test_approval_and_status_both_go_out(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _CONFIRM_WAMID))

        assert len(_rows(db, svc, url=_APPROVE_URL)) == 1
        assert len(_rows(db, svc, url=_STATUS_URL)) == 1
        db.refresh(svc)
        assert svc.data.get("lizo_confirmed_at")

    def test_the_customer_is_thanked(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT) as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _CONFIRM_WAMID))

        assert send.call_args.args[2] == (
            "Thank you for confirming your order. We appreciate your business."
        )
        assert _texts(db, svc) == [
            "Thank you for confirming your order. We appreciate your business."
        ]

    def test_button_label_case_does_not_matter(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT):
            conversation_engine.handle_inbound(db, account, _tap("CONFIRM ORDER", _CONFIRM_WAMID))
        assert len(_rows(db, svc, url=_APPROVE_URL)) == 1


class TestSecondTapCancel:
    def test_nothing_reaches_sfa(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Cancel", _CONFIRM_WAMID))

        assert _rows(db, svc) == []
        db.refresh(svc)
        assert svc.data.get("liso_cancelled_at")
        assert svc.data.get("lizo_confirmed_at") is None

    def test_the_customer_is_told_with_their_order_number(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Cancel", _CONFIRM_WAMID))

        assert _texts(db, svc) == [
            "Your Order #26OS02LC00007 has been cancelled as requested.\n"
            "If this was not intended, please contact us for assistance."
        ]


class TestFirstDecisionWins:
    """Buttons stay tappable forever, so both are still live after a decision."""

    def test_cancel_after_confirm_is_ignored(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _CONFIRM_WAMID, "t1"))
            conversation_engine.handle_inbound(db, account, _tap("Cancel", _CONFIRM_WAMID, "t2"))

        db.refresh(svc)
        assert svc.data.get("liso_cancelled_at") is None
        assert len(_rows(db, svc, url=_APPROVE_URL)) == 1

    def test_confirm_after_cancel_never_approves(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT):
            conversation_engine.handle_inbound(db, account, _tap("Cancel", _CONFIRM_WAMID, "t1"))
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _CONFIRM_WAMID, "t2"))

        assert _rows(db, svc, url=_APPROVE_URL) == []
        db.refresh(svc)
        assert svc.data.get("lizo_confirmed_at") is None

    def test_the_order_message_is_inert_once_decided(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT), \
             patch("app.services.wa_sender.send_template", return_value=_SENT) as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _CONFIRM_WAMID, "t1"))
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", _ORDER_WAMID, "t2"))
        assert not send.called


class TestRouting:
    def test_a_tap_on_a_message_with_no_recorded_template_degrades_to_step_one(self, db):
        # Real case: an order sent before outbound rows stamped content.template_name.
        # The template cannot be identified, so the tap is treated as the order
        # message — idempotent, and incapable of approving anything. The safe
        # direction to fail in.
        #
        # A context.id that resolves to nothing at all never reaches here:
        # conversation_engine cannot find the Service and drops the tap first.
        _c, account, svc = _setup(db)
        make_message(db, svc, wamid="wamid.legacy", direction="outbound",
                     message_type="template", content=None)
        db.commit()

        with patch("app.services.wa_sender.send_template", return_value=_SENT) as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", "wamid.legacy"))
        assert send.called
        assert _rows(db, svc, url=_APPROVE_URL) == []

    def test_a_tap_whose_context_resolves_to_nothing_is_dropped_upstream(self, db):
        # Documents where the boundary actually sits: conversation_engine resolves the
        # Service from context.id, so an unknown wamid never reaches Liso's handler.
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_SENT) as send:
            conversation_engine.handle_inbound(db, account, _tap("Confirm Order", "wamid.unknown"))
        assert not send.called
        assert _rows(db, svc) == []

    def test_an_unknown_button_on_the_confirmation_template_does_nothing(self, db):
        _c, account, svc = _setup(db)
        _confirm_message(db, svc)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT) as text, \
             patch("app.services.wa_sender.send_template", return_value=_SENT) as tpl:
            conversation_engine.handle_inbound(db, account, _tap("Track Shipment", _CONFIRM_WAMID))
        assert not text.called and not tpl.called
        assert _rows(db, svc) == []

    def test_cancel_on_the_order_message_does_nothing(self, db):
        # The order template has no Cancel button, but a template could gain one.
        _c, account, svc = _setup(db)
        with patch("app.services.wa_sender.send_text", return_value=_TEXT_SENT) as text, \
             patch("app.services.wa_sender.send_template", return_value=_SENT) as tpl:
            conversation_engine.handle_inbound(db, account, _tap("Cancel", _ORDER_WAMID))
        assert not text.called and not tpl.called
        db.refresh(svc)
        assert svc.data.get("liso_cancelled_at") is None
