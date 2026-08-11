"""
POST /client-api/v1/lizo/payments — Liso's payment confirmation.

A sibling of the order endpoint over the same pipeline. The tests that matter most
are in TestIsolatedFromTheOrderFlow: a payment must never reach the two-step confirm
or SFA's ApproveOrder, and must never produce a callback in Shirin Asal's envelope.
"""
from unittest.mock import patch

import pytest

from app.lizo import notify as lizo_notify
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_PAYMENT_VALUE
from app.models.conversation import Service
from app.models.outbound_notification import OutboundNotification
from app.services import notify_queue, send_scheduler
from app.services.wa_sender import SendResult
from tests.conftest import make_api_key, make_company, make_wa_account, make_wa_template

_MOCK_SEND = SendResult(ok=True, meta_message_id="wamid.pay", error=None)
_KEY = "liso-payment-key-01"
_TEMPLATE = "liso_payment_confirm"

# What an admin would type into the mapping UI for the payload below. The three named
# fields are addressed through `data` like everything else, because to_ingest_request
# copies them there.
_PARAM_MAPPING = {
    "1": "data.customer_name",
    "2": "data.order_no",
    "3": "data.net_amount",
}


def _headers():
    return {"X-API-Key": _KEY}


def _setup(db, with_mapping=True, code="LISOPAY"):
    comp = make_company(db, code=code)
    make_api_key(db, comp.id, key=_KEY)
    tpl = make_wa_template(db, comp.id, name=_TEMPLATE)
    tpl.components = [{"type": "BODY", "text": "Hi {{1}}, order {{2}} paid {{3}}"}]
    if with_mapping:
        tpl.param_mapping = _PARAM_MAPPING
    db.commit()
    make_wa_account(db, comp.id)
    return comp, tpl


def _payload(service_id="PAY-00123", **overrides):
    p = {
        "service_id": service_id,
        "template_name": _TEMPLATE,
        "template_expiry_hours": 24,
        "customer_mobile": "917025985366",
        "customer_name": "GIANT BAZAAR, Pattom",
        "order_no": "26OS02LC00007",
        "net_amount": "222.000",
        "data": {"payment_mode": "UPI", "paid_on": "10/08/2026"},
    }
    p.update(overrides)
    return p


def _post(client, payload):
    return client.post("/client-api/v1/lizo/payments", json=payload, headers=_headers())


class TestIngest:
    def test_the_sample_payload_is_accepted(self, client, db):
        _setup(db)
        r = _post(client, _payload())
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "in_progress"
        assert r.json()["service_id"] == "PAY-00123"

    def test_named_fields_are_copied_into_data(self, client, db):
        # The mapping UI addresses everything through `data`, so the top-level fields
        # have to land there or no dot-path could reach them.
        _setup(db)
        _post(client, _payload("PAY-1"))
        svc = db.query(Service).filter(Service.service_id == "PAY-1").first()
        assert svc.data["customer_name"] == "GIANT BAZAAR, Pattom"
        assert svc.data["order_no"] == "26OS02LC00007"
        assert svc.data["net_amount"] == "222.000"
        assert svc.data["customer_mobile"] == "917025985366"

    def test_extra_data_survives_untouched(self, client, db):
        # The whole point of keeping `data` open: SFA adds a field, nothing breaks.
        _setup(db)
        p = _payload("PAY-2")
        p["data"]["reference_no"] = "TXN-99"
        p["data"]["nested"] = {"bank": {"name": "HDFC"}}
        _post(client, p)
        svc = db.query(Service).filter(Service.service_id == "PAY-2").first()
        assert svc.data["reference_no"] == "TXN-99"
        assert svc.data["nested"]["bank"]["name"] == "HDFC"

    def test_params_resolve_in_order(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            _post(client, _payload("PAY-3"))
            assert send_scheduler._send_one_pending(db) is True
        assert send.call_args.args[2] == ["GIANT BAZAAR, Pattom", "26OS02LC00007", "222.000"]

    def test_it_is_stamped_as_a_payment_not_an_order(self, client, db):
        _setup(db)
        _post(client, _payload("PAY-4"))
        svc = db.query(Service).filter(Service.service_id == "PAY-4").first()
        assert svc.data[FLOW_MARKER_KEY] == FLOW_PAYMENT_VALUE

    def test_no_approval_fields_required(self, client, db):
        """
        The reason this endpoint exists. The order endpoint rejects a payload with no
        UserID/CompanyID because Confirm Order needs them to call SFA's ApproveOrder.
        A payment has nothing to approve and must not be asked for them.
        """
        _setup(db)
        p = _payload("PAY-NOAPPROVE")
        assert "UserID" not in p["data"] and "CompanyID" not in p["data"]
        assert _post(client, p).status_code == 201


class TestValidation:
    @pytest.mark.parametrize("field", ["customer_name", "order_no", "net_amount",
                                       "customer_mobile", "service_id", "template_name"])
    def test_each_required_field_is_required(self, client, db, field):
        _setup(db)
        p = _payload("V-1")
        del p[field]
        r = _post(client, p)
        assert r.status_code == 422
        assert r.json()["status"] == "validation_error"
        assert field in r.json()["message"]

    def test_a_numeric_net_amount_is_rejected(self, client, db):
        # The trap this endpoint closes. Inside opaque `data` on the order endpoint,
        # 222.0 is accepted and reaches the customer as "₹222.0" with no error.
        _setup(db)
        r = _post(client, _payload("V-2", net_amount=222.0))
        assert r.status_code == 422
        assert "net_amount" in r.json()["message"]

    @pytest.mark.parametrize("field", ["customer_name", "order_no", "net_amount"])
    def test_blank_is_treated_as_missing(self, client, db, field):
        # Meta refuses the whole message for one blank parameter, so whitespace must
        # not slip through as a "present" value.
        _setup(db)
        r = _post(client, _payload("V-3", **{field: "   "}))
        assert r.status_code == 422
        assert field in r.json()["message"]

    def test_country_code_is_required_same_as_orders(self, client, db):
        _setup(db)
        r = _post(client, _payload("V-4", customer_mobile="7025985366"))
        assert r.status_code == 422
        assert "11–15 digits including the country code" in r.json()["message"]

    @pytest.mark.parametrize("key", ["customer_name", "order_no", "net_amount",
                                     "customer_mobile", "questions", "_flow"])
    def test_reserved_keys_inside_data_are_refused(self, client, db, key):
        # Two sources of truth that could disagree is ambiguous — refuse rather than
        # silently pick one.
        _setup(db)
        p = _payload("V-5")
        p["data"][key] = "anything"
        r = _post(client, p)
        assert r.status_code == 422
        assert "reserved key" in r.json()["message"]

    def test_duplicate_returns_the_original_reference(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            first = _post(client, _payload("V-DUP"))
            second = _post(client, _payload("V-DUP"))
        assert second.status_code == 409
        assert second.json()["status"] == "duplicate_service_id"
        assert second.json()["reference_id"] == first.json()["reference_id"]

    def test_unmapped_template_is_rejected_before_sending(self, client, db):
        _setup(db, with_mapping=False)
        r = _post(client, _payload("V-6"))
        assert r.status_code == 422
        assert r.json()["status"] == "template_not_configured"

    def test_unknown_template_is_404(self, client, db):
        _setup(db)
        r = _post(client, _payload("V-7", template_name="__missing__"))
        assert r.status_code == 404
        assert r.json()["status"] == "template_not_found"


class TestIsolatedFromTheOrderFlow:
    """
    A payment shares the send pipeline but must reach none of the order machinery.
    These are the tests that would catch the flow marker being reused.
    """

    def _service(self, client, db, sid="ISO-1"):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            _post(client, _payload(sid))
            send_scheduler._send_one_pending(db)
        return db.query(Service).filter(Service.service_id == sid).first()

    def test_a_confirm_order_tap_does_nothing(self, client, db):
        """
        The dangerous case. inbound.handles() claims payments so they cannot fall
        through to the questionnaire path — but handle_tap must then refuse to act,
        or a "confirm_order" tap would send the *order* confirmation template and
        open a route into SFA's ApproveOrder.
        """
        from app.lizo import inbound as lizo_inbound
        svc = self._service(db=db, client=client, sid="ISO-TAP")

        with patch("app.lizo.confirm.send_confirm_template") as send_confirm, \
             patch("app.lizo.approve.emit") as approve_emit, \
             patch("app.lizo.inbound._post_confirmation") as post_conf:
            lizo_inbound.handle_tap(db, svc, None, "917025985366", "Confirm Order", None)

        send_confirm.assert_not_called()
        approve_emit.assert_not_called()
        post_conf.assert_not_called()
        assert svc.data.get("liso_confirm_sent_at") is None
        assert svc.data.get("lizo_confirmed_at") is None

    def test_inbound_still_claims_it(self, client, db):
        # If this were False, a reply would fall into _fire_next_question and an
        # enqueue_notification("responded") in the wrong envelope.
        from app.lizo import inbound as lizo_inbound
        svc = self._service(db=db, client=client, sid="ISO-CLAIM")
        assert lizo_inbound.handles(svc) is True

    def test_notify_queue_is_still_excluded(self, client, db):
        # handles() has to claim payments, or the moment Liso's key gets a notify_url
        # a payment would POST Shirin Asal's envelope to their endpoint.
        svc = self._service(db=db, client=client, sid="ISO-NQ")
        assert lizo_notify.handles(svc) is True
        notify_queue.enqueue_notification(db, svc, "completed")
        db.commit()
        assert db.query(OutboundNotification).filter(
            OutboundNotification.service_id == svc.id).count() == 0

    def test_no_status_callback_is_queued(self, client, db):
        # Payments are silent by choice — SFA has no endpoint for them yet.
        svc = self._service(db=db, client=client, sid="ISO-CB")
        assert lizo_notify.reports_on(svc) is False
        lizo_notify.emit(db, svc, lizo_notify.STATUS_DELIVERED)
        db.commit()
        assert db.query(OutboundNotification).filter(
            OutboundNotification.service_id == svc.id).count() == 0
