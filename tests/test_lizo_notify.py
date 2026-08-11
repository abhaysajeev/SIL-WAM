"""
Liso's status callback to SFA.

The test that justifies the whole module is
TestSuppressedByNotifyQueue::test_completed_then_failed_produces_both — a Liso
order is "completed" the instant its template sends, so notify_queue drops every
later receipt. Without a separate notifier, a broken order image would be reported
to the client as a success.
"""
import json
from datetime import datetime, timezone

import pytest

from app.lizo import notify as lizo_notify
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE
from app.models.outbound_notification import OutboundNotification
from app.services import conversation_engine, notify_queue, queue_manager
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message, make_queue_entry,
    make_service, make_wa_account,
)

_MOBILE = "919876543210"
_WAMID = "wamid.liso.template"
_URL = "http://108.181.62.233:49009/api/Sfa/SaveWhatsAppOrderStatus"

_LISO_DATA = {"customer_mobile": _MOBILE, FLOW_MARKER_KEY: FLOW_MARKER_VALUE,
              "customer_name": "GIANT BAZAAR, Pattom"}


def _setup(db, *, code="LSNOT", notify_url=_URL, liso=True, status="completed"):
    comp = make_company(db, code=code)
    key = make_api_key(db, comp.id, key=f"key-{code}", notify_url=notify_url)
    conv = make_conversation(db, comp.id, _MOBILE)
    account = make_wa_account(db, comp.id)
    svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
    svc.data = dict(_LISO_DATA) if liso else {"customer_mobile": _MOBILE}
    svc.status = status
    db.commit()
    make_queue_entry(db, svc, mobile_no=_MOBILE, status="completed")
    msg = make_message(db, svc, wamid=_WAMID, direction="outbound", message_type="template")
    db.commit()
    return comp, account, svc, msg


def _receipt(state, wamid=_WAMID, errors=None, timestamp="1786500123"):
    r = {"id": wamid, "status": state, "timestamp": timestamp, "recipient_id": _MOBILE}
    if errors:
        r["errors"] = errors
    return r


def _payloads(db, svc):
    rows = (db.query(OutboundNotification)
            .filter(OutboundNotification.service_id == svc.id)
            .order_by(OutboundNotification.created_at).all())
    return [r.payload for r in rows]


class TestPayloadShape:
    def test_matches_the_agreed_envelope(self, db):
        _c, _a, svc, _m = _setup(db)
        lizo_notify.emit(db, svc, lizo_notify.STATUS_DELIVERED,
                         event_at=datetime(2026, 8, 11, 5, 32, 15, 761000, tzinfo=timezone.utc))
        db.commit()

        p = _payloads(db, svc)[0]
        assert set(p) == {"Credentials", "RequestData"}
        assert p["Credentials"] == {
            "CheckSum": 0, "Operation": 0, "Latitude": "", "Longitude": "", "Altitude": "",
            "DeviceID": "", "IMEI": "", "LoginUserID": "",
            "ServiceName": "SaveWhatsAppOrderStatus",
            "TokenID": "", "BluetoothID": "", "IsZipped": 0, "CompanyID": 0,
            "SendStatus": 0, "ApkType": "", "DeviceNotificationID": "",
            "HierarchyTypeID": "", "HierarchyID": "",
        }
        assert p["RequestData"] == {
            "ReferenceId": str(svc.id),
            "Status": "Delivered",
            "Reason": "",
            "Timestamp": "2026-08-11T05:32:15.761Z",
        }

    def test_reference_id_is_our_uuid_not_their_service_id(self, db):
        # This is what Liso stored from our 201 response. Sending their own
        # service_id instead would land on nothing in their database.
        _c, _a, svc, _m = _setup(db)
        lizo_notify.emit(db, svc, lizo_notify.STATUS_SENT)
        db.commit()
        p = _payloads(db, svc)[0]
        assert p["RequestData"]["ReferenceId"] == str(svc.id)
        assert p["RequestData"]["ReferenceId"] != svc.service_id

    def test_payload_is_json_serialisable(self, db):
        # It is stored as JSONB and POSTed verbatim — a stray UUID or datetime here
        # would only surface in the scheduler, long after the event.
        _c, _a, svc, _m = _setup(db)
        lizo_notify.emit(db, svc, lizo_notify.STATUS_CONFIRMED)
        db.commit()
        json.dumps(_payloads(db, svc)[0])

    def test_notify_url_is_snapshotted_on_the_row(self, db):
        _c, _a, svc, _m = _setup(db)
        lizo_notify.emit(db, svc, lizo_notify.STATUS_SENT)
        db.commit()
        row = db.query(OutboundNotification).filter(
            OutboundNotification.service_id == svc.id).first()
        assert row.notify_url == _URL


class TestReasonWording:
    @pytest.mark.parametrize("errors,expected", [
        ([{"code": 131026}], "Invalid WhatsApp number"),
        ([{"code": 131053, "title": "Media upload error"}],
         "Order image could not be downloaded from the supplied URL"),
        ([{"code": 131052}], "Order image could not be downloaded from the supplied URL"),
        ([{"code": 132000}], "Template parameter mismatch"),
    ])
    def test_meta_codes_map_to_plain_english(self, errors, expected):
        assert lizo_notify.reason_for(meta_errors=errors) == expected

    def test_unmapped_code_falls_back_to_metas_title(self):
        # A code we have never seen should still say something useful rather than
        # a generic line that hides what happened.
        assert lizo_notify.reason_for(
            meta_errors=[{"code": 999999, "title": "Some new Meta failure"}]
        ) == "Some new Meta failure"

    def test_our_own_failed_reason_is_used_when_meta_said_nothing(self):
        assert lizo_notify.reason_for(failed_reason="media_error") == \
            "Order image could not be downloaded from the supplied URL"

    def test_last_resort(self):
        assert lizo_notify.reason_for() == "Message could not be delivered"

    def test_reason_is_empty_on_every_success_status(self, db):
        _c, _a, svc, _m = _setup(db)
        for s in (lizo_notify.STATUS_SENT, lizo_notify.STATUS_DELIVERED,
                  lizo_notify.STATUS_READ, lizo_notify.STATUS_CONFIRMED):
            lizo_notify.emit(db, svc, s)
        db.commit()
        assert all(p["RequestData"]["Reason"] == "" for p in _payloads(db, svc))


class TestFromMetaReceipts:
    @pytest.mark.parametrize("state,expected", [
        ("sent", "Sent"), ("delivered", "Delivered"), ("read", "Read"), ("failed", "Failed"),
    ])
    def test_each_receipt_produces_a_callback(self, db, state, expected):
        _c, account, svc, _m = _setup(db)
        conversation_engine.handle_status(db, _receipt(state), account)
        assert [p["RequestData"]["Status"] for p in _payloads(db, svc)] == [expected]

    def test_timestamp_comes_from_metas_event_time(self, db):
        # Meta's receipt can be processed seconds after the event; the client should
        # see when it happened, not when we got round to it.
        _c, account, svc, _m = _setup(db)
        conversation_engine.handle_status(db, _receipt("delivered", timestamp="1786500123"), account)
        ts = _payloads(db, svc)[0]["RequestData"]["Timestamp"]
        assert ts == datetime.fromtimestamp(1786500123, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def test_failed_receipt_carries_the_reason(self, db):
        _c, account, svc, _m = _setup(db)
        conversation_engine.handle_status(
            db, _receipt("failed", errors=[{"code": 131053, "title": "Media upload error"}]), account)
        p = _payloads(db, svc)[0]["RequestData"]
        assert p["Status"] == "Failed"
        assert p["Reason"] == "Order image could not be downloaded from the supplied URL"

    def test_a_status_meta_sends_that_we_do_not_report_is_ignored(self, db):
        _c, account, svc, _m = _setup(db)
        conversation_engine.handle_status(db, _receipt("deleted"), account)
        assert _payloads(db, svc) == []


class TestSuppressedByNotifyQueue:
    """The reason app/lizo/notify.py exists at all."""

    def test_completed_then_failed_produces_both(self, db):
        # A Liso service is already "completed" when the failure arrives, so
        # notify_queue drops it — rank(failed) <= rank(completed). The client would
        # otherwise be told the order succeeded and never corrected.
        _c, account, svc, _m = _setup(db, status="completed")
        conversation_engine.handle_status(db, _receipt("delivered"), account)
        conversation_engine.handle_status(
            db, _receipt("failed", errors=[{"code": 131053}]), account)

        assert [p["RequestData"]["Status"] for p in _payloads(db, svc)] == ["Delivered", "Failed"]

    def test_async_failure_marks_a_completed_service_failed(self, db):
        # conversation_engine's failure branch guards on status == "in_progress",
        # which a Liso service never is. Without the widening it stays "completed".
        _c, account, svc, _m = _setup(db, status="completed")
        conversation_engine.handle_status(
            db, _receipt("failed", errors=[{"code": 131026}]), account)
        db.refresh(svc)
        assert svc.status == "failed"
        assert svc.failed_reason == "whatsapp_number_invalid"

    def test_notify_queue_writes_nothing_for_a_liso_service(self, db):
        # Its payload is Shirin's envelope; posting it to the SFA endpoint would be
        # unparseable there.
        _c, _a, svc, msg = _setup(db)
        notify_queue.enqueue_notification(db, svc, "completed")
        db.commit()
        assert _payloads(db, svc) == []


class TestTerminalSendFailure:
    """
    A send that never got a wamid produces no Meta receipt ever, so handle_status
    cannot report it. queue_manager has to.
    """

    def test_invalid_number_at_send_time(self, db):
        _c, _a, svc, _m = _setup(db, status="in_progress")
        svc.failed_reason = "whatsapp_number_invalid"
        queue_manager._notify_lizo_failure(db, svc)
        db.commit()
        p = _payloads(db, svc)[0]["RequestData"]
        assert p["Status"] == "Failed"
        assert p["Reason"] == "Invalid WhatsApp number"

    def test_retries_exhausted(self, db):
        _c, _a, svc, _m = _setup(db, status="in_progress")
        svc.send_attempts = 3
        db.commit()
        queue_manager._fail_or_schedule_retry(db, svc, "media_error")
        db.commit()
        statuses = [p["RequestData"]["Status"] for p in _payloads(db, svc)]
        assert statuses == ["Failed"]

    def test_a_scheduled_retry_does_not_notify(self, db):
        # Three attempts must not become three "Failed" callbacks.
        _c, _a, svc, _m = _setup(db, status="in_progress")
        svc.send_attempts = 1
        db.commit()
        queue_manager._fail_or_schedule_retry(db, svc, "send_error")
        db.commit()
        assert _payloads(db, svc) == []


class TestGuards:
    def test_nothing_queued_without_a_notify_url(self, db):
        _c, _a, svc, _m = _setup(db, notify_url=None)
        lizo_notify.emit(db, svc, lizo_notify.STATUS_DELIVERED)
        db.commit()
        assert _payloads(db, svc) == []

    def test_nothing_queued_for_a_non_liso_service(self, db):
        _c, account, svc, _m = _setup(db, liso=False, code="SHIRIN1")
        lizo_notify.emit(db, svc, lizo_notify.STATUS_DELIVERED)
        db.commit()
        assert _payloads(db, svc) == []

    def test_an_unknown_status_is_refused(self, db):
        # Guards against a typo silently shipping a status their dashboard cannot
        # interpret.
        _c, _a, svc, _m = _setup(db)
        lizo_notify.emit(db, svc, "delivered")   # lowercase — not our vocabulary
        db.commit()
        assert _payloads(db, svc) == []

    def test_handles_is_false_without_the_flow_marker(self, db):
        _c, _a, svc, _m = _setup(db, liso=False, code="SHIRIN2")
        assert lizo_notify.handles(svc) is False


class TestShirinUnaffected:
    def test_a_shirin_service_still_gets_its_own_payload(self, db):
        # The notify_queue guard must key on the Liso marker, not on "has a
        # notify_url" — otherwise it would silence every client.
        _c, _a, svc, msg = _setup(db, liso=False, code="SHIRIN3", status="in_progress")
        notify_queue.enqueue_notification(db, svc, "delivered", message=msg)
        db.commit()
        payloads = _payloads(db, svc)
        assert len(payloads) == 1
        assert payloads[0]["status"] == "delivered"          # Shirin's flat shape
        assert "RequestData" not in payloads[0]
