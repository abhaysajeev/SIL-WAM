"""client-api/v1/services — ingest/get/retry endpoints, X-API-Key auth."""
import uuid
from unittest.mock import patch

from app.models.conversation import Service
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_queue_entry,
    make_service, make_wa_account, make_wa_template,
)

_MOCK_SEND = SendResult(ok=True, meta_message_id="wamid.clienttest", error=None)

_KEY = "client-api-test-key-01"


def _headers():
    return {"X-API-Key": _KEY}


def _setup(db, notify_url=None):
    comp = make_company(db, code="CLIAPI")
    key = make_api_key(db, comp.id, key=_KEY, notify_url=notify_url)
    make_wa_template(db, comp.id, name="order_confirm")
    make_wa_account(db, comp.id)
    return comp, key


def _payload(service_id="ORD-001"):
    return {
        "service_id": service_id,
        "template_name": "order_confirm",
        "data": {"customer_mobile": "919876543210"},
    }


class TestIngestAuth:
    def test_missing_api_key_returns_422(self, client, db):
        r = client.post("/client-api/v1/services", json=_payload())
        assert r.status_code == 422

    def test_invalid_api_key_returns_401(self, client, db):
        r = client.post("/client-api/v1/services", json=_payload(),
                         headers={"X-API-Key": "bad-key"})
        assert r.status_code == 401


class TestIngestSuccess:
    def test_valid_request_returns_201_with_reference_id(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/client-api/v1/services", json=_payload(), headers=_headers())
        assert r.status_code == 201
        body = r.json()
        assert body["service_id"] == "ORD-001"
        assert "reference_id" in body
        assert body["status"] in ("in_progress", "waiting")
        assert "message" not in body

    def test_api_key_id_recorded_on_service(self, client, db):
        _comp, key = _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/services", json=_payload("ORD-002"), headers=_headers())

        svc = db.query(Service).filter(Service.service_id == "ORD-002").first()
        assert svc is not None
        assert svc.api_key_id == key.id

    def test_duplicate_service_id_returns_409(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/services", json=_payload("ORD-DUP"), headers=_headers())
            r = client.post("/client-api/v1/services", json=_payload("ORD-DUP"), headers=_headers())
        assert r.status_code == 409

    def test_unknown_template_returns_404(self, client, db):
        _setup(db)
        payload = _payload("ORD-003")
        payload["template_name"] = "does_not_exist"
        r = client.post("/client-api/v1/services", json=payload, headers=_headers())
        assert r.status_code == 404

    def test_no_wa_account_returns_503(self, client, db):
        comp = make_company(db, code="NOACCT")
        make_api_key(db, comp.id, key="noacct-key")
        make_wa_template(db, comp.id, name="order_confirm")
        r = client.post("/client-api/v1/services", json=_payload("ORD-004"),
                         headers={"X-API-Key": "noacct-key"})
        assert r.status_code == 503


class TestAnswerTypeTranslation:
    """Client sends 1-indexed answer_type (1=yes/no, 2=rating, 3=free text);
    internally we store 0-indexed (0/1/2) — see ingest_service's translation."""

    def _payload_with_questions(self, service_id, answer_types):
        p = _payload(service_id)
        p["data"]["questions"] = [
            {"sequence": i + 1, "field_key": f"q{i+1}", "question": "Q?",
             "answer_type": at, "sent": 0}
            for i, at in enumerate(answer_types)
        ]
        return p

    def test_client_1_2_3_stored_internally_as_0_1_2(self, client, db):
        _setup(db)
        payload = self._payload_with_questions("ORD-AT01", [1, 2, 3])
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/client-api/v1/services", json=payload, headers=_headers())
        assert r.status_code == 201

        svc = db.query(Service).filter(Service.service_id == "ORD-AT01").first()
        stored = [q["answer_type"] for q in svc.questions]
        assert stored == [0, 1, 2]

    def test_invalid_answer_type_rejected(self, client, db):
        _setup(db)
        payload = self._payload_with_questions("ORD-AT02", [1, 2, 4])  # 4 is out of range
        r = client.post("/client-api/v1/services", json=payload, headers=_headers())
        assert r.status_code == 400

    def test_zero_answer_type_rejected(self, client, db):
        _setup(db)
        # 0 was the OLD (pre-migration) convention — must now be rejected, not silently accepted.
        payload = self._payload_with_questions("ORD-AT03", [0, 2, 3])
        r = client.post("/client-api/v1/services", json=payload, headers=_headers())
        assert r.status_code == 400


class TestGetService:
    def test_get_returns_service_status(self, client, db):
        _setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/client-api/v1/services", json=_payload("ORD-005"), headers=_headers())
        r = client.get("/client-api/v1/services/ORD-005", headers=_headers())
        assert r.status_code == 200
        assert r.json()["service_id"] == "ORD-005"

    def test_get_unknown_service_returns_404(self, client, db):
        _setup(db)
        r = client.get("/client-api/v1/services/does-not-exist", headers=_headers())
        assert r.status_code == 404


class TestRetryEndpoint:
    """PATCH /services/{reference_id}/retry — keyed by the internal reference_id
    (Service.id UUID), not the client's own service_id string."""

    def _failed_invalid_number_service(self, db, comp, key, service_id, mobile_no):
        conv = make_conversation(db, comp.id, mobile_no)
        template = make_wa_template(db, comp.id, name="order_confirm")
        svc = make_service(
            db, conv.id, comp.id, service_id=service_id, status="failed",
            mobile_no=mobile_no, api_key_id=key.id, template_sent=True,
        )
        svc.template_id = template.id
        svc.failed_reason = "whatsapp_number_invalid"
        svc.send_attempts = 1
        db.commit()
        make_queue_entry(db, svc, mobile_no=mobile_no, status="completed")
        return svc

    def test_retry_by_reference_id_succeeds_and_resets_attempts(self, client, db):
        comp, key = _setup(db)
        svc = self._failed_invalid_number_service(db, comp, key, "ORD-RETRY-1", "919000000099")

        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.patch(
                f"/client-api/v1/services/{svc.id}/retry",
                json={"customer_mobile": "919000000098"},
                headers=_headers(),
            )

        assert r.status_code == 200
        body = r.json()
        assert body["reference_id"] == str(svc.id)
        assert body["service_id"] == svc.service_id

        db.refresh(svc)
        assert svc.status in ("in_progress", "waiting")
        assert svc.failed_reason is None
        assert svc.send_attempts == 0          # fresh retry budget after correction
        assert svc.next_retry_at is None

    def test_old_service_id_style_url_no_longer_matches(self, client, db):
        comp, key = _setup(db)
        svc = self._failed_invalid_number_service(db, comp, key, "ORD-RETRY-2", "919000000097")

        # svc.service_id is a plain string ("ORD-RETRY-2"), not a UUID — the path
        # param is now typed uuid.UUID, so this must fail validation, not 200.
        r = client.patch(
            f"/client-api/v1/services/{svc.service_id}/retry",
            json={"customer_mobile": "919000000096"},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_unknown_reference_id_returns_404(self, client, db):
        _setup(db)
        r = client.patch(
            f"/client-api/v1/services/{uuid.uuid4()}/retry",
            json={"customer_mobile": "919000000095"},
            headers=_headers(),
        )
        assert r.status_code == 404

    def test_send_error_reason_is_rejected(self, client, db):
        comp, key = _setup(db)
        conv = make_conversation(db, comp.id, "919000000094")
        svc = make_service(
            db, conv.id, comp.id, service_id="ORD-RETRY-3", status="failed",
            mobile_no="919000000094", api_key_id=key.id, template_sent=True,
        )
        svc.failed_reason = "send_error"
        db.commit()

        r = client.patch(
            f"/client-api/v1/services/{svc.id}/retry",
            json={"customer_mobile": "919000000093"},
            headers=_headers(),
        )
        assert r.status_code == 409
