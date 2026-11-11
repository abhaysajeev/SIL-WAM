"""Copying API traffic to the central log server (app/monitoring/).

offer() is replaced by a list, so nothing here touches the network or the
shipper thread; the shipper's own batching and retry are tested directly.
"""
import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.monitoring import shipper
from tests.conftest import make_api_key, make_company

API_KEY = "test-api-key-12345"


@pytest.fixture()
def shipped(monkeypatch):
    monkeypatch.setattr(settings, "LOG_INGEST_URL", "http://logs.invalid/ingest/batch")
    monkeypatch.setattr(settings, "LOG_INGEST_KEY", "ingest-key")
    monkeypatch.setattr(settings, "LOG_ENVIRONMENT", "test")
    docs = []
    monkeypatch.setattr(shipper, "offer", docs.append)
    return docs


def _company(db):
    comp = make_company(db, name="Shirin Asal")
    make_api_key(db, comp.id, key=API_KEY)
    return comp


def test_off_by_default(client, db, monkeypatch):
    docs = []
    monkeypatch.setattr(shipper, "offer", docs.append)
    client.post("/client-api/v1/services", json={}, headers={"X-API-Key": API_KEY})
    assert docs == []


def test_client_api_422_is_logged_with_its_reason(client, db, shipped):
    _company(db)
    body = {"service_id": "ORD-1", "template_name": "t1",
            "data": {"customer_mobile": "97158 917 7070"}}
    r = client.post("/client-api/v1/services?x=1", json=body, headers={"X-API-Key": API_KEY})
    assert r.status_code == 422

    [doc] = shipped
    log = doc["log"]
    assert log["api"] == "client"
    assert log["status"] == 422
    assert log["method"] == "POST" and log["path"] == "/client-api/v1/services"
    assert log["environment"] == "test"
    assert "customer_mobile" in log["error"]
    assert log["mobile"] == "97158 917 7070"
    assert doc["form_data"]["service_id"] == "ORD-1"
    assert log["response"]["detail"]
    assert isinstance(log["duration_ms"], int)


def test_company_is_tagged_once_the_key_resolves(client, db, shipped):
    _company(db)
    client.post("/client-api/v1/services",
                json={"service_id": "S", "template_name": "missing",
                      "data": {"customer_mobile": "971589177070"}},
                headers={"X-API-Key": API_KEY})
    [doc] = shipped
    assert doc["log"]["company"] == "Shirin Asal"
    assert doc["device_id"] == "Shirin Asal"
    assert doc["log"]["mobile"] == "971589177070"


def test_secrets_never_reach_the_log(client, db, shipped):
    client.post("/agent-api/v1/auth/login",
                json={"username": "someone", "password": "hunter2-secret",
                      "device_id": "d1", "access_token": "tok-123"},
                headers={"X-API-Key": "should-not-appear"})
    assert shipped, "agent-api call should be logged"
    text = json.dumps(shipped)
    for secret in ("hunter2-secret", "tok-123", "should-not-appear"):
        assert secret not in text
    assert shipped[0]["form_data"]["password"] == "***"


def test_meta_webhook_keeps_signature_and_drops_message_text(client, db, shipped, monkeypatch):
    monkeypatch.setattr(settings, "META_APP_SECRET", "app-secret")
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PN1"},
        "messages": [{"from": "971589177070", "id": "wamid.IN1", "type": "text",
                      "timestamp": "1", "text": {"body": "private customer words"}}],
        "statuses": [{"id": "wamid.OUT1", "status": "failed", "recipient_id": "971500000000",
                      "errors": [{"code": 131047, "title": "Re-engagement message"}]}],
    }}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    with patch("app.api.meta_webhook._process_payload_bg"):
        r = client.post("/webhook/meta", content=raw,
                        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r.status_code == 200          # the handler still saw the exact signed bytes

    [doc] = shipped
    assert "private customer words" not in json.dumps(doc)
    summary = doc["form_data"]
    assert summary["messages"][0]["id"] == "wamid.IN1"
    assert summary["statuses"][0]["errors"][0]["code"] == 131047
    assert doc["log"]["mobile"] == "971589177070"
    assert doc["log"]["api"] == "webhook"


def test_webhook_verify_token_in_query_is_masked(client, db, shipped):
    client.get("/webhook/meta?hub.mode=subscribe&hub.verify_token=very-secret&hub.challenge=1")
    [doc] = shipped
    assert "very-secret" not in json.dumps(doc)


def test_panel_pages_are_not_logged(client, db, shipped):
    client.get("/login")
    client.get("/companies")
    client.get("/api/companies/")
    assert shipped == []


# ── The shipper itself ────────────────────────────────────────────────────────

def test_batches_split_at_100():
    docs = [{"device_id": "x", "form_data": {}, "log": {"n": i}} for i in range(250)]
    assert [len(c) for c in shipper.chunks(docs)] == [100, 100, 50]


def test_a_dead_log_server_drops_after_one_retry(monkeypatch):
    calls = []

    def boom(body):
        calls.append(body)
        raise OSError("connection refused")

    monkeypatch.setattr(shipper, "_post", boom)
    monkeypatch.setattr(shipper, "RETRY_DELAY_SECONDS", 0)
    shipper.ship([{"device_id": "x", "form_data": {}, "log": {}}])   # must not raise
    assert len(calls) == 2


def test_offer_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "LOG_INGEST_URL", "")
    monkeypatch.setattr(shipper, "_ensure_started", lambda: pytest.fail("started while disabled"))
    shipper.offer({"device_id": "x"})
