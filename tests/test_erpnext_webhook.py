"""Module 4 — ERPNext inbound webhook: auth, dedup, service creation."""
from unittest.mock import patch
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_wa_account,
    make_wa_template,
)

_MOCK_SEND = SendResult(ok=True, meta_message_id="wamid.erptest", error=None)

_KEY = "erp-test-api-key-99"


def _headers():
    return {"X-API-Key": _KEY}


class TestERPNextNotifyAuth:
    def test_missing_api_key_returns_422(self, client):
        r = client.post("/webhook/erpnext/notify", json={
            "customer_mobile": "919876543210",
            "template_name": "payment_receipt",
        })
        assert r.status_code == 422

    def test_invalid_api_key_returns_401(self, client):
        r = client.post("/webhook/erpnext/notify",
                        json={"customer_mobile": "919876543210", "template_name": "x"},
                        headers={"X-API-Key": "bad-key"})
        assert r.status_code == 401


class TestERPNextNotifyValidation:
    def _setup(self, db):
        comp = make_company(db, code="ERPCO")
        make_api_key(db, comp.id, _KEY)
        return comp

    def test_template_not_found_returns_404(self, client, db):
        self._setup(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/webhook/erpnext/notify",
                            json={"customer_mobile": "919876543210",
                                  "template_name": "nonexistent"},
                            headers=_headers())
        assert r.status_code == 404

    def test_no_wa_account_returns_503(self, client, db):
        comp = self._setup(db)
        make_wa_template(db, comp.id, name="payment_receipt")
        # No WhatsApp account created
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/webhook/erpnext/notify",
                            json={"customer_mobile": "919876543210",
                                  "template_name": "payment_receipt"},
                            headers=_headers())
        assert r.status_code == 503

    def test_no_access_token_returns_503(self, client, db):
        comp = self._setup(db)
        make_wa_template(db, comp.id, name="payment_receipt")
        # Account without access token
        from app.models.whatsapp import WhatsAppAccount
        acc = WhatsAppAccount(company_id=comp.id, phone_number_id="9999",
                              access_token_encrypted=None)
        db.add(acc); db.commit()
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/webhook/erpnext/notify",
                            json={"customer_mobile": "919876543210",
                                  "template_name": "payment_receipt"},
                            headers=_headers())
        assert r.status_code == 503


class TestERPNextNotifySuccess:
    def _setup_full(self, db):
        comp = make_company(db, code="ERPFULL")
        make_api_key(db, comp.id, _KEY)
        make_wa_template(db, comp.id, name="payment_receipt")
        make_wa_account(db, comp.id)
        return comp

    def test_valid_request_returns_200(self, client, db):
        self._setup_full(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/webhook/erpnext/notify",
                            json={"customer_mobile": "919876543210",
                                  "template_name": "payment_receipt"},
                            headers=_headers())
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_service_id_returned_in_response(self, client, db):
        self._setup_full(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r = client.post("/webhook/erpnext/notify",
                            json={"customer_mobile": "919876543210",
                                  "template_name": "payment_receipt",
                                  "reference_id": "REF-2026-001"},
                            headers=_headers())
        assert r.status_code == 200
        assert r.json()["service_id"] == "REF-2026-001"

    def test_service_row_created_in_db(self, client, db):
        self._setup_full(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/webhook/erpnext/notify",
                        json={"customer_mobile": "919876543210",
                              "template_name": "payment_receipt",
                              "reference_id": "SVC-DB-CHECK"},
                        headers=_headers())
        from app.models.conversation import Service
        svc = db.query(Service).filter(Service.service_id == "SVC-DB-CHECK").first()
        assert svc is not None
        assert svc.status == "completed"  # template-only → completes immediately

    def test_invoice_no_stored_in_service_data(self, client, db):
        self._setup_full(db)
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/webhook/erpnext/notify",
                        json={"customer_mobile": "919876543210",
                              "template_name": "payment_receipt",
                              "reference_id": "SVC-INV-CHECK",
                              "invoice_no": "INV-2026-0042"},
                        headers=_headers())
        from app.models.conversation import Service
        svc = db.query(Service).filter(Service.service_id == "SVC-INV-CHECK").first()
        assert svc is not None
        assert svc.data.get("invoice_no") == "INV-2026-0042"


class TestERPNextNotifyDedup:
    def _setup_full(self, db):
        comp = make_company(db, code="ERPDUP")
        make_api_key(db, comp.id, _KEY)
        make_wa_template(db, comp.id, name="payment_receipt")
        make_wa_account(db, comp.id)
        return comp

    def test_duplicate_reference_id_returns_200(self, client, db):
        self._setup_full(db)
        payload = {"customer_mobile": "919876543210",
                   "template_name": "payment_receipt",
                   "reference_id": "DUP-REF-001"}
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            r1 = client.post("/webhook/erpnext/notify", json=payload, headers=_headers())
            r2 = client.post("/webhook/erpnext/notify", json=payload, headers=_headers())
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert "duplicate" in r2.json().get("note", "")

    def test_duplicate_creates_only_one_service(self, client, db):
        self._setup_full(db)
        payload = {"customer_mobile": "919876543210",
                   "template_name": "payment_receipt",
                   "reference_id": "DUP-REF-002"}
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND):
            client.post("/webhook/erpnext/notify", json=payload, headers=_headers())
            client.post("/webhook/erpnext/notify", json=payload, headers=_headers())

        from app.models.conversation import Service
        count = db.query(Service).filter(Service.service_id == "DUP-REF-002").count()
        assert count == 1

    def test_wa_sender_called_exactly_once_on_dedup(self, client, db):
        self._setup_full(db)
        payload = {"customer_mobile": "919876543210",
                   "template_name": "payment_receipt",
                   "reference_id": "DUP-SEND-003"}
        with patch("app.services.wa_sender.send_template", return_value=_MOCK_SEND) as mock_send:
            client.post("/webhook/erpnext/notify", json=payload, headers=_headers())
            client.post("/webhook/erpnext/notify", json=payload, headers=_headers())
        assert mock_send.call_count == 1
