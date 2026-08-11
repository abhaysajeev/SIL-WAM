"""client-api/v1/lizo/orders — Lizo's order-shaped ingest wrapper."""
from unittest.mock import patch

import pytest

from app.models.conversation import Service
from app.services import send_scheduler
from app.services.wa_sender import SendResult
from tests.conftest import make_api_key, make_company, make_wa_account, make_wa_template

_MOCK_SEND = SendResult(ok=True, meta_message_id="wamid.lizotest", error=None)
_KEY = "lizo-api-test-key-01"

# Mapping is configuration, not code — these paths are what an admin would type
# into the template mapping UI for the payload in _payload() below.
# {{2}} (store name) has no field in the payload; it rides in as an extra key.
_PARAM_MAPPING = {
    "1": "data.customer_name",     "2": "data.store_name",
    "3": "data.order_no",          "4": "data.order_date",
    "5": "data.order_summary",     "6": "data.summary.subtotal",
    "7": "data.summary.discount",  "8": "data.summary.gst",
    "9": "data.summary.net_amount",
}


# The three fields ApproveOrder needs when the customer taps Confirm. Required on
# every Lizo order since that endpoint was wired up — see
# TestApprovalFieldsRequiredAtIngest for why they are checked at ingest rather than
# discovered missing at tap time.
_APPROVAL_FIELDS = {"UserID": "1024", "CompanyID": 5}


def _headers():
    return {"X-API-Key": _KEY}


def _setup(db, with_mapping=True):
    comp = make_company(db, code="LIZO")
    make_api_key(db, comp.id, key=_KEY)
    tpl = make_wa_template(db, comp.id, name="order_confirm_lizo")
    if with_mapping:
        tpl.param_mapping = _PARAM_MAPPING
        db.commit()
    make_wa_account(db, comp.id)
    return comp, tpl


def _payload(service_id="LIZO-ORD-10234", **overrides):
    p = {
        "service_id": service_id,
        "template_name": "order_confirm_lizo",
        "template_expiry_hours": 24,
        "customer_mobile": "919876543210",
        "data": {
            "customer_name": "Ravi Kumar",
            "order_no": "10234",
            "order_date": "30/07/2026",
            **_APPROVAL_FIELDS,
            "items": {
                "item_1": {"item": "A", "qty": 2},
                "item_2": {"item": "B", "qty": 1},
            },
            "summary": {
                "subtotal": "1499.00", "discount": "150.00",
                "gst": "45.00", "net_amount": "1394.00",
            },
        },
    }
    p.update(overrides)
    return p


class TestAuth:
    def test_missing_api_key_returns_422(self, client, db):
        assert client.post("/client-api/v1/lizo/orders", json=_payload()).status_code == 422

    def test_invalid_api_key_returns_401(self, client, db):
        r = client.post("/client-api/v1/lizo/orders", json=_payload(),
                        headers={"X-API-Key": "bad-key"})
        assert r.status_code == 401


class TestIngest:
    def test_valid_payload_returns_201(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/client-api/v1/lizo/orders", json=_payload(), headers=_headers())
        assert r.status_code == 201
        body = r.json()
        assert body["service_id"] == "LIZO-ORD-10234"
        assert body["status"] == "in_progress"
        assert "reference_id" in body

    def test_mobile_is_moved_into_data(self, client, db):
        """The shared pipeline requires data.customer_mobile; Lizo sends it top-level."""
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=_payload("L-1"), headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "L-1").first()
        assert svc.data["customer_mobile"] == "919876543210"

    def test_nested_data_is_stored_verbatim(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=_payload("L-2"), headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "L-2").first()
        assert svc.data["items"]["item_1"] == {"item": "A", "qty": 2}
        assert svc.data["summary"]["net_amount"] == "1394.00"

    def test_no_questions_so_service_completes(self, client, db):
        """Lizo has no questionnaire — queue_manager completes template-only services."""
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=_payload("L-3"), headers=_headers())
            send_scheduler._send_one_pending(db)
        svc = db.query(Service).filter(Service.service_id == "L-3").first()
        db.refresh(svc)
        assert svc.questions is None
        assert svc.status == "completed"
        assert svc.template_sent is True


class TestDynamicData:
    """
    The contract is that `data` has no fixed shape beyond the fields SFA's own
    endpoints need. These are the tests that would fail if someone declared Lizo's
    order fields on the model.

    order_no/UserID/CompanyID are the exception and are merged into every case
    below: they are not part of the shape contract but the arguments to
    ApproveOrder, required since that call was wired up.
    """

    @pytest.mark.parametrize("data", [
        {},                                                  # nothing but the required fields
        {"anything": "at all"},                              # unrelated keys
        {"a": {"b": {"c": {"d": "deep"}}}},                  # deeply nested
        {"list_field": [1, 2, 3], "num": 42, "flag": True},  # mixed types
        {"customer_name": "Ravi"},                           # a subset of the usual fields
    ], ids=["empty", "unrelated", "deep", "mixed-types", "subset"])
    def test_any_data_shape_is_accepted(self, client, db, data):
        _setup(db)
        data = {"order_no": "1", **_APPROVAL_FIELDS, **data}
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/client-api/v1/lizo/orders",
                            json=_payload("D-" + str(abs(hash(str(data))))[:6], data=data),
                            headers=_headers())
        assert r.status_code == 201, r.text

    def test_unmapped_extra_keys_survive_to_storage(self, client, db):
        _setup(db)
        p = _payload("L-4")
        p["data"]["store_name"] = "Lizo Store"
        p["data"]["billing"] = {"gstin": "29ABC", "addr": {"city": "BLR"}}
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "L-4").first()
        assert svc.data["store_name"] == "Lizo Store"
        assert svc.data["billing"]["addr"]["city"] == "BLR"


class TestReservedKeys:
    @pytest.mark.parametrize("key,value", [
        # Would turn a Lizo order into a questionnaire — it would never complete.
        ("questions", [{"field_key": "q1", "question": "Happy?", "answer_type": 1}]),
        # A non-list crashed the shared ingest with AttributeError → 500.
        ("questions", "yes please"),
        # Two disagreeing sources of truth for the number.
        ("customer_mobile", "911111111111"),
    ], ids=["questions-list", "questions-string", "customer_mobile"])
    def test_rejected_with_422(self, client, db, key, value):
        _setup(db)
        p = _payload("R-1")
        p["data"][key] = value
        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        assert r.status_code == 422

    def test_error_message_names_the_key(self, client, db):
        """Client X must be able to act on the response without asking us."""
        _setup(db)
        p = _payload("R-2")
        p["data"]["questions"] = []
        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        msg = str(r.json())
        assert "reserved key" in msg
        assert "questions" in msg


class TestTemplateParams:
    """
    Ingest never calls Meta — it only writes rows. send_scheduler claims the
    service afterwards, so these drive that step explicitly.
    """

    def test_params_resolved_from_mapping_in_order(self, client, db):
        """Lizo sends no template_params; the UI mapping supplies all nine."""
        _setup(db)
        p = _payload("L-5")
        p["data"]["store_name"] = "Lizo Store"
        p["data"]["order_summary"] = "2x A\n1x B"   # see TestKnownGap
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
            assert send_scheduler._send_one_pending(db) is True

        assert send.call_args.args[2] == [
            "Ravi Kumar", "Lizo Store", "10234", "30/07/2026",
            "2x A\n1x B", "1499.00", "150.00", "45.00", "1394.00",
        ]

    def test_sent_to_the_normalised_number(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            client.post("/client-api/v1/lizo/orders",
                        json=_payload("L-6", customer_mobile="+919876543210"),
                        headers=_headers())
            send_scheduler._send_one_pending(db)
        assert send.call_args.args[3] == "919876543210"


class TestMediaHeader:
    """
    A template with an IMAGE header gets its URL per send from header_mapping, the
    same dot-path machinery as the body params. Meta fetches the URL itself, so the
    only thing we can usefully check at ingest is that it is not blank.
    """

    _IMAGE_COMPONENTS = [
        {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4:x=="]}},
        {"type": "BODY", "text": "Hello {{1}}"},
    ]

    def _setup_image_template(self, db, mapping="data.receipt_image_url"):
        comp, tpl = _setup(db)
        tpl.components = self._IMAGE_COMPONENTS
        tpl.header_mapping = mapping
        db.commit()
        return comp, tpl

    def test_url_is_resolved_and_frozen_on_the_service(self, client, db):
        self._setup_image_template(db)
        p = _payload("L-IMG-1")
        p["data"]["receipt_image_url"] = "https://cdn.example.com/r/10234.png"

        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        assert r.status_code == 201

        svc = db.query(Service).filter(Service.service_id == "L-IMG-1").first()
        assert svc.header_media == {
            "format": "image",
            "link": "https://cdn.example.com/r/10234.png",
        }

    def test_url_reaches_meta_as_a_header_component(self, client, db):
        self._setup_image_template(db)
        p = _payload("L-IMG-2")
        p["data"]["receipt_image_url"] = "https://cdn.example.com/r/2.png"

        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
            assert send_scheduler._send_one_pending(db) is True

        # header_media is the 6th positional — appended after cta_urls so the
        # existing assertions on args[2] and args[3] keep working.
        assert send.call_args.args[5]["link"] == "https://cdn.example.com/r/2.png"

    def test_missing_url_is_rejected_at_ingest_not_at_send(self, client, db):
        # A 201 followed by a silent send failure is the outcome worth avoiding:
        # the client would only find out from a delivery callback minutes later.
        self._setup_image_template(db)
        r = client.post("/client-api/v1/lizo/orders", json=_payload("L-IMG-3"), headers=_headers())

        assert r.status_code == 422
        assert r.json()["status"] == "missing_media_url"
        assert "data.receipt_image_url" in r.json()["message"]
        assert db.query(Service).filter(Service.service_id == "L-IMG-3").first() is None

    def test_blank_url_is_rejected_too(self, client, db):
        self._setup_image_template(db)
        p = _payload("L-IMG-4")
        p["data"]["receipt_image_url"] = "   "
        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        assert r.status_code == 422

    def test_unconfigured_mapping_names_the_template(self, client, db):
        # An image template that nobody mapped would otherwise send a header-less
        # message that Meta rejects with a parameter-mismatch error.
        self._setup_image_template(db, mapping=None)
        p = _payload("L-IMG-5")
        p["data"]["receipt_image_url"] = "https://cdn.example.com/r/5.png"
        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())

        assert r.status_code == 422
        assert r.json()["status"] == "template_not_configured"
        assert "order_confirm_lizo" in r.json()["message"]

    def test_text_header_templates_are_untouched(self, client, db):
        # The regression guard for every existing client: no header_media, and
        # send_template still called with the same arguments as before.
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            r = client.post("/client-api/v1/lizo/orders", json=_payload("L-IMG-6"),
                            headers=_headers())
            assert r.status_code == 201
            send_scheduler._send_one_pending(db)

        svc = db.query(Service).filter(Service.service_id == "L-IMG-6").first()
        assert svc.header_media is None
        assert send.call_args.args[5] is None


class TestKnownGap:
    """
    A variable-length collection cannot be mapped into one placeholder. Nesting
    items under item_1/item_2 makes each *scalar* addressable, but {{5}} needs
    every item joined into a single string, which no dot-path can express.

    These tests pin the current behaviour so the day it changes is deliberate.
    """

    def test_mapping_a_whole_dict_yields_a_python_repr(self, client, db):
        _setup(db, with_mapping=False)
        _comp, tpl = None, None
        from app.models.whatsapp import WhatsAppTemplate
        tpl = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.name == "order_confirm_lizo").first()
        tpl.param_mapping = {"1": "data.items"}
        db.commit()
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            client.post("/client-api/v1/lizo/orders", json=_payload("G-1"), headers=_headers())
            send_scheduler._send_one_pending(db)
        rendered = send.call_args.args[2][0]
        # Not something any customer should receive — hence order_summary is
        # expected as a pre-rendered string from Client X.
        assert rendered.startswith("{'item_1'")

    def test_mapping_sized_for_two_items_misfires_on_other_sizes(self, client, db):
        _setup(db, with_mapping=False)
        from app.models.whatsapp import WhatsAppTemplate
        tpl = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.name == "order_confirm_lizo").first()
        tpl.param_mapping = {"1": "data.items.item_1.item", "2": "data.items.item_2.item"}
        db.commit()
        p = _payload("G-2")
        p["data"]["items"] = {"item_1": {"item": "A", "qty": 1}}   # only one item
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
            send_scheduler._send_one_pending(db)
        # Missing path resolves to "" — a blank in the customer's message, no error.
        assert send.call_args.args[2] == ["A", ""]


class TestValidation:
    def test_bad_mobile_is_422_not_500(self, client, db):
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json=_payload("V-1", customer_mobile="12345"), headers=_headers())
        assert r.status_code == 422

    def test_plus_prefix_is_stripped(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders",
                        json=_payload("V-2", customer_mobile="+919876543210"),
                        headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "V-2").first()
        assert svc.data["customer_mobile"] == "919876543210"

    def test_duplicate_service_id_returns_409(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=_payload("V-3"), headers=_headers())
            r = client.post("/client-api/v1/lizo/orders", json=_payload("V-3"), headers=_headers())
        assert r.status_code == 409

    def test_validation_message_is_a_string_not_a_list(self, client, db):
        """
        Lizo's contract is "message is always a string", so a client never has to
        branch on its type. FastAPI returns a list of per-field objects by default;
        LizoRoute flattens it for this router only.
        """
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json=_payload("V-5", customer_mobile="12"), headers=_headers())
        assert r.status_code == 422
        message = r.json()["message"]
        assert isinstance(message, str)
        assert "customer_mobile" in message

    def test_multiple_validation_errors_are_joined(self, client, db):
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json={"customer_mobile": "12", "data": {}}, headers=_headers())
        message = r.json()["message"]
        assert isinstance(message, str)
        # Nothing is dropped when several fields fail at once.
        assert "service_id" in message and "template_name" in message

    def test_sfa_path_keeps_fastapis_default_shape(self, client, db):
        """
        The envelope belongs to Lizo's router alone. The live SFA/Shirin client has
        consumed FastAPI's default `{"detail": [...]}` since launch, so this locks
        the boundary: LizoRoute must never leak onto /client-api/v1/services.
        """
        _setup(db)
        r = client.post("/client-api/v1/services",
                        json={"service_id": "S-1", "data": {}}, headers=_headers())
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], list)
        assert "message" not in r.json()

    def test_unapproved_template_returns_404(self, client, db):
        comp = make_company(db, code="LIZO2")
        make_api_key(db, comp.id, key="lizo-key-2")
        make_wa_template(db, comp.id, name="order_confirm_lizo", status="PENDING")
        make_wa_account(db, comp.id)
        r = client.post("/client-api/v1/lizo/orders", json=_payload("V-4"),
                        headers={"X-API-Key": "lizo-key-2"})
        assert r.status_code == 404


class TestResponseEnvelope:
    """
    Every reply from this endpoint has the same four keys, so Lizo never branches
    on response shape. `status` stays an enum and `message` carries the prose —
    which means rewording a message is not a breaking change, and the HTTP status
    remains the machine-readable signal.
    """

    _KEYS = {"service_id", "reference_id", "status", "message"}

    def test_success_shape(self, client, db):
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders", json=_payload("E-1"), headers=_headers())

        assert r.status_code == 201
        body = r.json()
        assert set(body) == self._KEYS
        assert body["service_id"] == "E-1"
        assert body["reference_id"] is not None
        assert body["status"] == "in_progress"
        assert body["message"] is None

    def test_reference_id_matches_the_created_service(self, client, db):
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders", json=_payload("E-2"), headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "E-2").first()
        assert r.json()["reference_id"] == str(svc.id)

    @pytest.mark.parametrize("case,expected_status", [
        ("bad_key",       "invalid_api_key"),
        ("bad_mobile",    "validation_error"),
        ("no_template",   "template_not_found"),
        ("missing_field", "validation_error"),
    ])
    def test_every_error_uses_the_same_four_keys(self, client, db, case, expected_status):
        _setup(db)
        if case == "bad_key":
            r = client.post("/client-api/v1/lizo/orders", json=_payload("E-3"),
                            headers={"X-API-Key": "nope"})
        elif case == "bad_mobile":
            r = client.post("/client-api/v1/lizo/orders",
                            json=_payload("E-3", customer_mobile="12"), headers=_headers())
        elif case == "no_template":
            p = _payload("E-3"); p["template_name"] = "__missing__"
            r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        else:
            r = client.post("/client-api/v1/lizo/orders",
                            json={"customer_mobile": "919876543210", "data": {}},
                            headers=_headers())

        body = r.json()
        assert r.status_code >= 400
        assert set(body) == self._KEYS, f"{case} broke the envelope: {body}"
        # status carries the code the client branches on — never prose, and never a
        # generic "failed" that would force them to parse the message.
        assert body["status"] == expected_status
        assert isinstance(body["message"], str) and body["message"]
        assert body["reference_id"] is None

    def test_bad_api_key_is_caught_by_the_route_class(self, client, db):
        """
        A 401 comes from Depends(get_api_key_and_company), which resolves before the
        endpoint body runs — proof the envelope is applied by LizoRoute and not by a
        try/except inside the handler, which could never see this.
        """
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders", json=_payload("E-4"),
                        headers={"X-API-Key": "not-a-real-key"})

        assert r.status_code == 401
        body = r.json()
        assert set(body) == self._KEYS
        assert body["status"] == "invalid_api_key"
        # The endpoint never ran, so the body stream was never consumed and the
        # client's own reference is still readable — worth echoing.
        assert body["service_id"] == "E-4"
        assert body["message"] == "Invalid or inactive API key"
        assert body["reference_id"] is None

    def test_service_id_is_null_when_the_body_is_not_readable(self, client, db):
        # The only case that genuinely cannot echo: nothing parseable to echo from.
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        content=b"{not json", headers={**_headers(), "Content-Type": "application/json"})

        assert r.status_code == 422
        assert set(r.json()) == self._KEYS
        assert r.json()["service_id"] is None

    def test_service_id_is_echoed_on_a_validation_error(self, client, db):
        # The raw body survives on the exception, so the client's own reference can
        # still be reported back even though the request never parsed cleanly.
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json=_payload("E-5", customer_mobile="12"), headers=_headers())
        assert r.json()["service_id"] == "E-5"

    def test_unhandled_exception_stays_in_the_envelope(self, client, db):
        """
        Without LizoRoute's catch-all this falls through to main.py's global handler
        and returns {"success", "message", "error_id"} — a third shape.
        """
        _setup(db)
        with patch("app.lizo.api.ingest_service", side_effect=RuntimeError("boom")):
            r = client.post("/client-api/v1/lizo/orders", json=_payload("E-6"),
                            headers=_headers())

        assert r.status_code == 500
        body = r.json()
        assert set(body) == self._KEYS
        assert body["status"] == "internal_error"
        assert body["service_id"] == "E-6"
        assert "error_id" not in body


class TestDuplicateReturnsTheOriginalReference:
    """
    The failure that actually happens is Lizo's POST succeeding and the response
    never arriving — timeout, reset, restart. Handing back the original
    reference_id on the retry turns a dead end into a reconciliation path.
    """

    def test_second_post_returns_the_first_reference_id(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            first  = client.post("/client-api/v1/lizo/orders", json=_payload("D-1"),
                                 headers=_headers())
            second = client.post("/client-api/v1/lizo/orders", json=_payload("D-1"),
                                 headers=_headers())

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["reference_id"] == first.json()["reference_id"]
        assert second.json()["status"] == "duplicate_service_id"
        assert "already exists" in second.json()["message"]

    def test_no_second_service_row_is_created(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=_payload("D-2"), headers=_headers())
            client.post("/client-api/v1/lizo/orders", json=_payload("D-2"), headers=_headers())

        assert db.query(Service).filter(Service.service_id == "D-2").count() == 1

    def test_the_same_id_under_a_different_company_is_not_a_duplicate(self, client, db):
        # service_id is unique per company, not globally — the dedup query must stay
        # company-scoped or one client could probe another's references.
        _setup(db)
        other = make_company(db, code="LIZO3")
        make_api_key(db, other.id, key="lizo-key-3")
        make_wa_template(db, other.id, name="order_confirm_lizo")
        make_wa_account(db, other.id)

        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/lizo/orders", json=_payload("D-3"), headers=_headers())
            r = client.post("/client-api/v1/lizo/orders", json=_payload("D-3"),
                            headers={"X-API-Key": "lizo-key-3"})

        assert r.status_code == 201


class TestLisoImageOrder:
    """
    The production shape: totals flat in `data`, an ImageURL for the media header,
    one Confirm Order button, and no order summary anywhere.
    """

    _IMAGE_URL = "http://108.181.62.233/FSSLisoFileServer/Sfa/Orders/2026/08/26OS02LC00029.jpg"

    _COMPONENTS = [
        {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4:x=="]}},
        {"type": "BODY", "text":
            "Dear {{1}},\n\nThank you for your order with {{2}}.\n\n"
            "Order No: {{3}}\nOrder Date: {{4}}\n\n"
            "*Subtotal:* {{5}}\n*Discount:* {{6}}\n*GST:* {{7}}\n*Net Amount:* {{8}}"},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Confirm Order"}]},
    ]

    _MAPPING = {
        "1": "data.customer_name", "2": "data.store_name",
        "3": "data.order_no",      "4": "data.order_date",
        "5": "data.subtotal",      "6": "data.discount",
        "7": "data.gst",           "8": "data.net_amount",
    }

    def _setup_image_template(self, db, **overrides):
        comp, tpl = _setup(db, with_mapping=False)
        tpl.components     = self._COMPONENTS
        tpl.param_mapping  = overrides.get("param_mapping", self._MAPPING)
        tpl.header_mapping = overrides.get("header_mapping", "data.ImageURL")
        db.commit()
        return comp, tpl

    def _order(self, service_id="26OS02LC00007", **data_overrides):
        data = {
            "customer_name": "Test", "store_name": "Liso",
            "order_no": service_id, "order_date": "17/07/2026",
            "ImageURL": self._IMAGE_URL,
            "items": [{"item": "Almond Spread 190gm", "qty": 1.0},
                      {"item": "Liso Pebbles 2 Pcs Colour Sachet", "qty": 1.0}],
            "subtotal": "211.230", "discount": "0.000",
            "gst": "10.561", "net_amount": "222.000",
            **_APPROVAL_FIELDS,
        }
        data.update(data_overrides)
        return {"service_id": service_id, "template_name": "order_confirm_lizo",
                "template_expiry_hours": 24, "customer_mobile": "917025985366",
                "data": data}

    def test_the_real_payload_is_accepted(self, client, db):
        self._setup_image_template(db)
        r = client.post("/client-api/v1/lizo/orders", json=self._order(), headers=_headers())
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "in_progress"

    def test_header_media_carries_the_http_url(self, client, db):
        self._setup_image_template(db)
        client.post("/client-api/v1/lizo/orders", json=self._order("I-1"), headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "I-1").first()
        assert svc.header_media == {"format": "image", "link": self._IMAGE_URL}

    def test_all_eight_params_resolve_from_the_flat_fields(self, client, db):
        # The totals moved out of data.summary.* to the top level of data; this is
        # what would break silently if the mapping still pointed at the old paths.
        self._setup_image_template(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as send:
            client.post("/client-api/v1/lizo/orders", json=self._order("I-2"), headers=_headers())
            assert send_scheduler._send_one_pending(db) is True

        assert send.call_args.args[2] == [
            "Test", "Liso", "I-2", "17/07/2026",
            "211.230", "0.000", "10.561", "222.000",
        ]
        assert send.call_args.args[5]["link"] == self._IMAGE_URL

    def test_items_are_stored_but_never_rendered(self, client, db):
        # With the summary gone, `items` reaches nothing but storage — the itemised
        # list now lives only inside the image.
        self._setup_image_template(db)
        client.post("/client-api/v1/lizo/orders", json=self._order("I-3"), headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "I-3").first()
        assert len(svc.data["items"]) == 2


class TestEmptyParamsRejectedAtIngest:
    """
    Meta refuses a template send with any blank parameter (#132000), on the
    background scheduler, long after the client had its 201. Two real orders died
    that way with five empty params. These pin the check that makes it a 422.
    """

    def _setup(self, db, **overrides):
        return TestLisoImageOrder()._setup_image_template(db, **overrides)

    def _order(self, sid, **kw):
        return TestLisoImageOrder()._order(sid, **kw)

    def test_missing_store_name_is_rejected(self, client, db):
        self._setup(db)
        payload = self._order("E-STORE")
        del payload["data"]["store_name"]
        r = client.post("/client-api/v1/lizo/orders", json=payload, headers=_headers())

        assert r.status_code == 422
        assert r.json()["status"] == "missing_parameter"
        assert "{{2}}" in r.json()["message"]
        assert "data.store_name" in r.json()["message"]

    def test_no_service_row_is_created(self, client, db):
        self._setup(db)
        payload = self._order("E-NOROW")
        del payload["data"]["store_name"]
        client.post("/client-api/v1/lizo/orders", json=payload, headers=_headers())
        assert db.query(Service).filter(Service.service_id == "E-NOROW").first() is None

    def test_blank_string_counts_as_missing(self, client, db):
        self._setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json=self._order("E-BLANK", net_amount="   "), headers=_headers())
        assert r.status_code == 422
        assert "{{8}}" in r.json()["message"]

    def test_every_offender_is_named_at_once(self, client, db):
        # One send fails for one blank param, so reporting them one at a time would
        # cost the client a round trip per field.
        self._setup(db)
        payload = self._order("E-MANY")
        del payload["data"]["store_name"]
        del payload["data"]["gst"]
        msg = client.post("/client-api/v1/lizo/orders",
                          json=payload, headers=_headers()).json()["message"]
        assert "{{2}}" in msg and "{{7}}" in msg

    def test_unmapped_template_is_rejected_not_sent_empty(self, client, db):
        self._setup(db, param_mapping=None)
        r = client.post("/client-api/v1/lizo/orders", json=self._order("E-UNMAPPED"),
                        headers=_headers())
        # Our configuration gap, not the client's payload — a distinct code so their
        # dashboard can route it to us rather than to their own developer.
        assert r.status_code == 422
        assert r.json()["status"] == "template_not_configured"
        assert "no parameter mapping" in r.json()["message"]

    def test_a_complete_payload_still_passes(self, client, db):
        self._setup(db)
        r = client.post("/client-api/v1/lizo/orders", json=self._order("E-OK"),
                        headers=_headers())
        assert r.status_code == 201


class TestStatusVocabulary:
    """
    `status` is the client's contract: they branch on it, so a value changing is a
    breaking change while a `message` reword is free. This locks the set.
    """

    _ALLOWED = {
        "in_progress", "invalid_api_key", "template_not_found", "duplicate_service_id",
        "validation_error", "missing_parameter", "missing_media_url",
        "template_not_configured", "whatsapp_not_configured", "internal_error",
        "missing_approval_field",
    }

    def test_the_published_set_matches_the_code(self):
        from app.lizo import responses
        declared = {
            v for k, v in vars(responses).items()
            if k.startswith("STATUS_") and isinstance(v, str)
        }
        assert declared == self._ALLOWED

    def test_status_is_never_prose(self, client, db):
        # A sentence here would force the client to parse it, which is exactly what
        # moving the code into `status` was meant to avoid.
        _setup(db)
        cases = [
            ({"X-API-Key": "nope"}, _payload("V-1")),
            (_headers(), _payload("V-2", customer_mobile="12")),
            (_headers(), {"customer_mobile": "919876543210", "data": {}}),
        ]
        for headers, body in cases:
            status = client.post("/client-api/v1/lizo/orders", json=body,
                                 headers=headers).json()["status"]
            assert status in self._ALLOWED, status
            assert " " not in status

    def test_no_whatsapp_account_maps_to_its_own_code(self, client, db):
        comp = make_company(db, code="NOACC")
        make_api_key(db, comp.id, key="noacc-key")
        make_wa_template(db, comp.id, name="order_confirm_lizo")
        # deliberately no make_wa_account
        r = client.post("/client-api/v1/lizo/orders", json=_payload("V-3"),
                        headers={"X-API-Key": "noacc-key"})
        assert r.status_code == 503
        assert r.json()["status"] == "whatsapp_not_configured"


class TestCountryCodeRequired:
    """
    The shared validator accepts 7–15 digits, so a bare 10-digit Indian mobile
    passed, returned 201, and then failed at Meta with 131026 — invisible to the
    client, who has no callback configured. Liso's own boundary is stricter.
    """

    @pytest.mark.parametrize("mobile", [
        "7025985366",        # bare Indian mobile — the case that prompted this
        "9170259853",        # 10 digits, looks close but is short
        "12",                # far too short
        "07025985366",       # leading zero, not E.164
        "91 7025 985366",    # spaces
        "abc",               # not a number
        "9170259853661234",  # 16 digits, too long
    ])
    def test_rejected_with_one_consistent_message(self, client, db, mobile):
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json=_payload("CC-1", customer_mobile=mobile), headers=_headers())

        assert r.status_code == 422
        assert r.json()["status"] == "validation_error"
        # Every rejection reads identically — the client never has to handle two
        # different sentences for "this number is unusable".
        assert "11–15 digits including the country code" in r.json()["message"]

    @pytest.mark.parametrize("mobile", ["917025985366", "+917025985366"])
    def test_country_code_forms_are_accepted(self, client, db, mobile):
        _setup(db)
        r = client.post("/client-api/v1/lizo/orders",
                        json=_payload(f"CC-OK-{mobile[-4:]}", customer_mobile=mobile),
                        headers=_headers())
        assert r.status_code == 201

    def test_the_plus_is_stripped_before_storage(self, client, db):
        # Meta's inbound webhook never sends a '+', so Conversation.mobile_no must
        # match digits-only or replies cannot route back to the service.
        _setup(db)
        client.post("/client-api/v1/lizo/orders",
                    json=_payload("CC-PLUS", customer_mobile="+917025985366"),
                    headers=_headers())
        svc = db.query(Service).filter(Service.service_id == "CC-PLUS").first()
        assert svc.data["customer_mobile"] == "917025985366"

    def test_no_service_row_is_created_for_a_bad_number(self, client, db):
        _setup(db)
        client.post("/client-api/v1/lizo/orders",
                    json=_payload("CC-NOROW", customer_mobile="7025985366"),
                    headers=_headers())
        assert db.query(Service).filter(Service.service_id == "CC-NOROW").first() is None

    def test_shirin_still_accepts_a_short_number(self, client, db):
        """
        The stricter rule is Liso's alone. /client-api/v1/services keeps the shared
        7–15 digit rule its live .NET client was built against.
        """
        _setup(db)
        r = client.post("/client-api/v1/services",
                        json={"service_id": "SFA-CC", "template_name": "order_confirm_lizo",
                              "data": {"customer_mobile": "7025985366"}},
                        headers=_headers())
        # Accepted by validation — it gets past the mobile check into the pipeline.
        assert r.status_code != 422, r.text


class TestApprovalFieldsRequiredAtIngest:
    """
    order_no, UserID and CompanyID are only read when the customer taps Confirm Order,
    which may be days after this response was returned as a success. A payload missing
    one of them would look perfectly accepted and then silently fail to approve, with
    nobody watching. So they are rejected at the door instead.
    """

    def _post(self, client, data_overrides, service_id="LIZO-APPROVE"):
        p = _payload(service_id)
        p["data"].update(data_overrides)
        return client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())

    @pytest.mark.parametrize("field", ["order_no", "UserID", "CompanyID"])
    def test_a_missing_field_is_422(self, client, db, field):
        _setup(db)
        p = _payload("LIZO-MISSING")
        del p["data"][field]
        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        assert r.status_code == 422, r.text
        assert r.json()["status"] == "missing_approval_field"
        assert field in r.json()["message"]

    @pytest.mark.parametrize("field", ["order_no", "UserID", "CompanyID"])
    def test_a_blank_field_is_422(self, client, db, field):
        _setup(db)
        r = self._post(client, {field: ""}, service_id=f"LIZO-BLANK-{field}")
        assert r.status_code == 422, r.text
        assert r.json()["status"] == "missing_approval_field"

    def test_a_non_numeric_company_id_is_422(self, client, db):
        _setup(db)
        r = self._post(client, {"CompanyID": "not-a-number"})
        assert r.status_code == 422, r.text
        assert r.json()["status"] == "missing_approval_field"

    def test_every_offending_field_is_named_at_once(self, client, db):
        """One response the client can fix from, not one 422 per attempt."""
        _setup(db)
        p = _payload("LIZO-ALLBAD")
        for field in ("order_no", "UserID", "CompanyID"):
            del p["data"][field]
        msg = client.post("/client-api/v1/lizo/orders", json=p,
                          headers=_headers()).json()["message"]
        assert "order_no" in msg and "UserID" in msg and "CompanyID" in msg

    def test_no_service_row_is_created(self, client, db):
        _setup(db)
        p = _payload("LIZO-NOROW")
        del p["data"]["UserID"]
        client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        assert db.query(Service).filter(Service.service_id == "LIZO-NOROW").first() is None

    def test_checked_even_when_the_template_is_missing(self, client, db):
        """
        Runs before the template lookup, so an incomplete payload is reported even
        when the template name is the other thing that is wrong.
        """
        _setup(db)
        p = _payload("LIZO-NOTPL", template_name="does-not-exist")
        del p["data"]["CompanyID"]
        r = client.post("/client-api/v1/lizo/orders", json=p, headers=_headers())
        assert r.status_code == 422, r.text
        assert r.json()["status"] == "missing_approval_field"

    def test_a_valid_payload_still_succeeds(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/client-api/v1/lizo/orders", json=_payload("LIZO-OK-APPROVE"),
                            headers=_headers())
        assert r.status_code == 201, r.text

    def test_shirin_is_unaffected(self, client, db):
        """The requirement is Liso's alone — /client-api/v1/services never checks it."""
        _setup(db)
        r = client.post("/client-api/v1/services",
                        json={"service_id": "SFA-NO-APPROVAL-FIELDS",
                              "template_name": "order_confirm_lizo",
                              "data": {"customer_mobile": "919876543210"}},
                        headers=_headers())
        assert r.status_code != 422, r.text
