"""
Lizo's order approval — the ApproveOrder call fired when Confirm Order is tapped.

The tests that matter most:

  TestConfirmTap::test_a_tap_produces_both_the_approval_and_the_status
      the approval and the Confirmed status callback are two separate calls to two
      separate SFA endpoints, and a confirmation must send both.

  TestConfirmTap::test_a_second_tap_approves_nothing_further
      template buttons stay tappable forever and every tap carries a fresh wamid, so
      nothing but the lizo_confirmed_at marker stops SFA being told to approve the
      same order twice.

  TestMissingFields::test_the_status_callback_still_goes_out
      an unapprovable order must still report its status — the safety net degrades
      one call, not both.
"""
import json

import pytest

from app.core.config import settings
from app.lizo import approve as lizo_approve
from app.lizo import notify as lizo_notify
from app.lizo.schemas import FLOW_MARKER_KEY, FLOW_MARKER_VALUE
from app.models.outbound_notification import OutboundNotification
from app.services import conversation_engine
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message, make_queue_entry,
    make_service, make_wa_account,
)

_MOBILE = "919876543210"
_TEMPLATE_WAMID = "wamid.lizo.template"
_APPROVE_URL = "http://108.181.62.233:49009/api/Sfa/ApproveOrder"
_STATUS_URL = "http://108.181.62.233:49009/api/Sfa/SaveWhatsAppOrderStatus"

_LIZO_DATA = {
    "customer_mobile": _MOBILE,
    FLOW_MARKER_KEY: FLOW_MARKER_VALUE,
    "customer_name": "Ravi Kumar",
    "order_no": "10234",
    "UserID": "1024",
    "CompanyID": 5,
}


@pytest.fixture
def approve_url(monkeypatch):
    """The URL is global config, not a per-key column — set it for the test."""
    monkeypatch.setattr(settings, "LIZO_APPROVE_ORDER_URL", _APPROVE_URL)
    return _APPROVE_URL


def _setup(db, *, data=None, code="LZAPP", liso=True):
    comp = make_company(db, code=code)
    key = make_api_key(db, comp.id, key=f"key-{code}", notify_url=_STATUS_URL)
    conv = make_conversation(db, comp.id, _MOBILE)
    account = make_wa_account(db, comp.id)
    svc = make_service(db, conv.id, comp.id, api_key_id=key.id)
    if data is not None:
        svc.data = data
    elif liso:
        svc.data = dict(_LIZO_DATA)
    else:
        svc.data = {"customer_mobile": _MOBILE, "order_no": "10234",
                    "UserID": "1024", "CompanyID": 5}
    svc.status = "completed"        # what queue_manager does for a template-only order
    db.commit()
    make_queue_entry(db, svc, mobile_no=_MOBILE, status="completed")
    # The confirmation template, not the order message. Approval is the *second* tap
    # of the two-step flow, and content["template_name"] is how inbound.py tells the
    # two "Confirm Order" buttons apart — see inbound._tapped_template.
    make_message(db, svc, wamid=_TEMPLATE_WAMID,
                 direction="outbound", message_type="template",
                 content={"template_name": settings.LIZO_CONFIRM_TEMPLATE})
    db.commit()
    return comp, account, svc


def _tap(payload="Confirm Order", wamid="wamid.tap1"):
    return {
        "id": wamid,
        "from": _MOBILE,
        "type": "button",
        "button": {"text": payload, "payload": payload},
        "context": {"id": _TEMPLATE_WAMID},
    }


def _rows(db, svc, url=None):
    q = (db.query(OutboundNotification)
         .filter(OutboundNotification.service_id == svc.id))
    if url:
        q = q.filter(OutboundNotification.notify_url == url)
    return q.order_by(OutboundNotification.created_at).all()


def _approval(db, svc):
    rows = _rows(db, svc, url=_APPROVE_URL)
    assert len(rows) == 1, f"expected exactly one approval row, got {len(rows)}"
    return rows[0].payload


class TestPayload:
    """SFA specified this envelope key-for-key; it is a contract, not a convenience."""

    def test_matches_the_agreed_envelope(self, db, approve_url):
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()

        payload = _approval(db, svc)
        assert set(payload) == {"CheckSum", "Operation", "Credentials", "RequestData"}
        assert payload["CheckSum"] == 0 and payload["Operation"] == 0

        assert set(payload["RequestData"]) == {
            "CheckSum", "Operation", "CompanyID", "UserID", "FactoryID", "CustomerID",
            "ColumnIndex", "sortingOrder", "pageNumber", "pageSize", "SearchType",
            "StatusID", "SearchText", "SearchText1", "SearchText2", "SearchText3",
            "SearchText4", "SearchText5", "SearchText6", "SearchWord", "SearchId1",
            "SearchId2", "SearchId3", "SearchId4", "SearchId5", "SearchApprovalDate",
            "SearchFromDate", "SearchToDate", "HierarchyID", "RouteID",
            "DeliveryRouteID", "IsDistributor", "DistributorID",
        }

    def test_search_text_is_the_order_no(self, db, approve_url):
        """SFA finds the order to approve by their own order number, not our UUID."""
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()

        rd = _approval(db, svc)["RequestData"]
        assert rd["SearchText"] == "10234"
        assert rd["SearchText"] != str(svc.id)

    def test_service_name_is_approve_order(self, db, approve_url):
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _approval(db, svc)["Credentials"]["ServiceName"] == "ApproveOrder"

    def test_the_ids_ride_in_credentials_too(self, db, approve_url):
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()

        creds = _approval(db, svc)["Credentials"]
        assert creds["CompanyID"] == 5
        assert creds["LoginUserID"] == "1024"

    def test_unused_fields_keep_their_fixed_defaults(self, db, approve_url):
        """Every key SFA listed is sent, so a missing one can never be the refusal."""
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()

        rd = _approval(db, svc)["RequestData"]
        assert rd["SearchText1"] == "" and rd["DistributorID"] == ""
        assert rd["pageSize"] == 0 and rd["IsDistributor"] == 0

    def test_payload_is_json_serialisable(self, db, approve_url):
        """It goes into JSONB and out over the wire — no UUIDs, no datetimes."""
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()
        json.dumps(_approval(db, svc))

    def test_url_comes_from_config(self, db, approve_url):
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _rows(db, svc, url=_APPROVE_URL)[0].notify_url == _APPROVE_URL

    def test_not_tied_to_a_message(self, db, approve_url):
        """The approval is about the order, not the tap that triggered it."""
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _rows(db, svc, url=_APPROVE_URL)[0].message_id is None


class TestTypes:
    """
    SFA wants CompanyID as a JSON number and UserID as a JSON string. Lizo's `data`
    is opaque and could hold either as either, so both are coerced on the way out.
    """

    def test_company_id_is_an_int_and_user_id_a_string(self, db, approve_url):
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()

        payload = _approval(db, svc)
        for block in (payload["RequestData"], payload["Credentials"]):
            company = block["CompanyID"]
            assert isinstance(company, int) and not isinstance(company, bool)
        assert isinstance(payload["RequestData"]["UserID"], str)
        assert isinstance(payload["Credentials"]["LoginUserID"], str)

    def test_a_quoted_company_id_is_coerced(self, db, approve_url):
        data = dict(_LIZO_DATA, CompanyID="5")
        _c, _a, svc = _setup(db, data=data)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _approval(db, svc)["RequestData"]["CompanyID"] == 5

    def test_a_numeric_user_id_is_coerced(self, db, approve_url):
        data = dict(_LIZO_DATA, UserID=1024)
        _c, _a, svc = _setup(db, data=data)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _approval(db, svc)["RequestData"]["UserID"] == "1024"

    def test_a_numeric_order_no_is_coerced(self, db, approve_url):
        data = dict(_LIZO_DATA, order_no=10234)
        _c, _a, svc = _setup(db, data=data)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _approval(db, svc)["RequestData"]["SearchText"] == "10234"


class TestExtract:
    """The field rule itself — shared with the ingest check so the two cannot drift."""

    def test_all_three_are_returned(self):
        fields, problems = lizo_approve.extract(dict(_LIZO_DATA))
        assert problems == []
        assert fields == {"order_no": "10234", "UserID": "1024", "CompanyID": 5}

    @pytest.mark.parametrize("field", ["order_no", "UserID", "CompanyID"])
    def test_a_missing_field_is_a_problem(self, field):
        data = {k: v for k, v in _LIZO_DATA.items() if k != field}
        fields, problems = lizo_approve.extract(data)
        assert fields is None
        assert any(field in p for p in problems)

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_values_are_problems(self, blank):
        fields, problems = lizo_approve.extract(dict(_LIZO_DATA, order_no=blank))
        assert fields is None and any("order_no" in p for p in problems)

    def test_every_offender_is_reported_at_once(self):
        _fields, problems = lizo_approve.extract({})
        assert len(problems) == 3

    def test_a_non_numeric_company_id_is_a_problem(self):
        fields, problems = lizo_approve.extract(dict(_LIZO_DATA, CompanyID="abc"))
        assert fields is None and any("CompanyID" in p for p in problems)

    def test_a_boolean_company_id_is_refused_not_coerced(self):
        """int(True) is 1 — a silent, wrong company is worse than a rejection."""
        fields, problems = lizo_approve.extract(dict(_LIZO_DATA, CompanyID=True))
        assert fields is None and any("CompanyID" in p for p in problems)

    def test_no_data_at_all(self):
        fields, problems = lizo_approve.extract(None)
        assert fields is None and len(problems) == 3


class TestConfirmTap:
    """End to end from Meta's inbound webhook."""

    def test_a_tap_produces_both_the_approval_and_the_status(self, db, approve_url):
        _c, account, svc = _setup(db)
        conversation_engine.handle_inbound(db, account, _tap())
        db.commit()

        urls = [r.notify_url for r in _rows(db, svc)]
        assert _APPROVE_URL in urls, "the order was never approved in SFA"
        assert _STATUS_URL in urls, "the Confirmed status was never reported"

    def test_the_approval_is_queued_before_the_status(self, db, approve_url):
        """The action first, the report of it second."""
        _c, account, svc = _setup(db)
        conversation_engine.handle_inbound(db, account, _tap())
        db.commit()
        assert [r.notify_url for r in _rows(db, svc)] == [_APPROVE_URL, _STATUS_URL]

    def test_the_status_says_confirmed(self, db, approve_url):
        _c, account, svc = _setup(db)
        conversation_engine.handle_inbound(db, account, _tap())
        db.commit()

        status_row = _rows(db, svc, url=_STATUS_URL)[0]
        assert status_row.payload["RequestData"]["Status"] == lizo_notify.STATUS_CONFIRMED

    def test_a_second_tap_approves_nothing_further(self, db, approve_url):
        """
        Buttons stay tappable forever and each tap carries a fresh wamid, so the wamid
        dedup does not stop repeats — lizo_confirmed_at is what makes this once-only.
        """
        _c, account, svc = _setup(db)
        conversation_engine.handle_inbound(db, account, _tap(wamid="wamid.tap1"))
        db.commit()
        conversation_engine.handle_inbound(db, account, _tap(wamid="wamid.tap2"))
        db.commit()
        assert len(_rows(db, svc, url=_APPROVE_URL)) == 1

    def test_an_unrelated_button_approves_nothing(self, db, approve_url):
        _c, account, svc = _setup(db)
        conversation_engine.handle_inbound(db, account, _tap(payload="View Order"))
        db.commit()
        assert _rows(db, svc, url=_APPROVE_URL) == []

    def test_the_case_of_the_button_label_does_not_matter(self, db, approve_url):
        _c, account, svc = _setup(db)
        conversation_engine.handle_inbound(db, account, _tap(payload="confirm_order"))
        db.commit()
        assert len(_rows(db, svc, url=_APPROVE_URL)) == 1


class TestNotQueued:

    def test_nothing_without_a_configured_url(self, db, monkeypatch):
        monkeypatch.setattr(settings, "LIZO_APPROVE_ORDER_URL", "")
        _c, _a, svc = _setup(db)
        lizo_approve.emit(db, svc)
        db.commit()
        assert _rows(db, svc) == []

    def test_nothing_for_a_non_liso_service(self, db, approve_url):
        """A Shirin service carries no flow marker and must never reach SFA."""
        _c, _a, svc = _setup(db, liso=False, code="LZAPPSH")
        lizo_approve.emit(db, svc)
        db.commit()
        assert _rows(db, svc) == []


class TestMissingFields:
    """
    The safety net for services created before the ingest check existed. The order
    cannot be approved, but nothing else may break because of it.
    """

    def _data_without(self, field):
        return {k: v for k, v in _LIZO_DATA.items() if k != field}

    @pytest.mark.parametrize("field", ["order_no", "UserID", "CompanyID"])
    def test_nothing_is_queued(self, db, approve_url, field):
        _c, _a, svc = _setup(db, data=self._data_without(field))
        lizo_approve.emit(db, svc)
        db.commit()
        assert _rows(db, svc, url=_APPROVE_URL) == []

    def test_it_does_not_raise(self, db, approve_url):
        """A failure here must not roll back the confirmation that triggered it."""
        _c, _a, svc = _setup(db, data=self._data_without("order_no"))
        lizo_approve.emit(db, svc)        # no exception

    def test_the_status_callback_still_goes_out(self, db, approve_url):
        _c, account, svc = _setup(db, data=self._data_without("UserID"))
        conversation_engine.handle_inbound(db, account, _tap())
        db.commit()

        assert _rows(db, svc, url=_APPROVE_URL) == []
        assert len(_rows(db, svc, url=_STATUS_URL)) == 1

    def test_the_confirmation_is_still_recorded(self, db, approve_url):
        _c, account, svc = _setup(db, data=self._data_without("CompanyID"))
        conversation_engine.handle_inbound(db, account, _tap())
        db.commit()
        db.refresh(svc)
        assert svc.data.get("lizo_confirmed_at")


class TestStatusCallbackUnchanged:
    """
    The Credentials block moved to app/lizo/sfa.py to be shared. The existing status
    payload must be byte-for-byte what it was before the move.
    """

    def test_save_status_credentials_are_untouched(self, db):
        creds = lizo_notify._credentials()
        assert creds["ServiceName"] == "SaveWhatsAppOrderStatus"
        assert creds["CompanyID"] == 0
        assert creds["LoginUserID"] == ""

    def test_credentials_are_rebuilt_per_call(self, db):
        """A caller mutating what it got back must not corrupt it for everyone."""
        first = lizo_notify._credentials()
        first["ServiceName"] = "mutated"
        assert lizo_notify._credentials()["ServiceName"] == "SaveWhatsAppOrderStatus"
