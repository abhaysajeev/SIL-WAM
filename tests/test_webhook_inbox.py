"""
Webhook inbox — store first, then process (Phase 1a).

The properties that matter:
  * a delivery is on disk before Meta gets its 200;
  * the same delivery stored twice is one row;
  * a failed attempt is retried, and a crashed worker's row is reclaimed;
  * after MAX_ATTEMPTS the row goes dead and lands in failed_webhooks for replay;
  * any configured app secret is accepted, an unknown one is not;
  * the Redis dedup key is only written after processing succeeded;
  * a number that sends but hears nothing back raises the silence alarm.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.api import meta_webhook
from app.core.config import settings
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.failed_webhook import FailedWebhook
from app.models.webhook_inbox import WebhookInbox
from app.services import conversation_engine, webhook_inbox, webhook_inbox_scheduler
from tests.conftest import TestingSessionLocal, make_company, make_wa_account


def _signed(payload: dict, secret: str = "app-secret") -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"Content-Type": "application/json", "X-Hub-Signature-256": sig}


def _payload(n: int = 1) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "field": "messages",
        "value": {"metadata": {"phone_number_id": "PN-INBOX"},
                  "statuses": [{"id": f"wamid.S{n}", "status": "delivered",
                                "recipient_id": "918075571893"}]}}]}]}


@pytest.fixture()
def inbox_on(monkeypatch):
    """Inbox mode on, with every session factory pointed at the TEST database."""
    monkeypatch.setattr(settings, "WEBHOOK_INBOX_MODE", "on")
    monkeypatch.setattr(settings, "META_APP_SECRET", "app-secret")
    monkeypatch.setattr(settings, "META_APP_SECRETS", "")
    monkeypatch.setattr(meta_webhook, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(webhook_inbox, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(webhook_inbox_scheduler, "SessionLocal", TestingSessionLocal)


def _rows(db):
    db.expire_all()
    return db.query(WebhookInbox).order_by(WebhookInbox.id).all()


# ── Signature ────────────────────────────────────────────────────────────────

class TestSignature:
    def test_primary_secret_accepted(self, monkeypatch):
        monkeypatch.setattr(settings, "META_APP_SECRET", "one")
        monkeypatch.setattr(settings, "META_APP_SECRETS", "")
        raw, h = _signed({"a": 1}, "one")
        assert meta_webhook._verify_signature(raw, h["X-Hub-Signature-256"])

    def test_extra_secret_accepted(self, monkeypatch):
        monkeypatch.setattr(settings, "META_APP_SECRET", "one")
        monkeypatch.setattr(settings, "META_APP_SECRETS", " two , three ")
        raw, h = _signed({"a": 1}, "three")
        assert meta_webhook._verify_signature(raw, h["X-Hub-Signature-256"])

    def test_unknown_secret_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "META_APP_SECRET", "one")
        monkeypatch.setattr(settings, "META_APP_SECRETS", "two")
        raw, h = _signed({"a": 1}, "intruder")
        assert not meta_webhook._verify_signature(raw, h["X-Hub-Signature-256"])

    def test_trailing_whitespace_in_secret_is_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "META_APP_SECRET", "one\n")
        monkeypatch.setattr(settings, "META_APP_SECRETS", "")
        raw, h = _signed({"a": 1}, "one")
        assert meta_webhook._verify_signature(raw, h["X-Hub-Signature-256"])

    def test_bad_signature_is_403_and_not_stored(self, client, db, inbox_on):
        raw, h = _signed(_payload(), "intruder")
        r = client.post("/webhook/meta", content=raw, headers=h)
        assert r.status_code == 403
        assert _rows(db) == []


# ── Store, then process ──────────────────────────────────────────────────────

class TestStoreAndProcess:
    def test_stored_and_processed(self, client, db, inbox_on):
        seen = []
        with patch.object(meta_webhook, "_process_payload",
                          side_effect=lambda body, _db: seen.append(body)):
            raw, h = _signed(_payload())
            r = client.post("/webhook/meta", content=raw, headers=h)
        assert r.status_code == 200
        [row] = _rows(db)
        assert row.status == "done"
        assert row.attempts == 1
        assert seen == [_payload()]

    def test_same_delivery_five_times_is_one_row_processed_once(self, client, db, inbox_on):
        seen = []
        with patch.object(meta_webhook, "_process_payload",
                          side_effect=lambda body, _db: seen.append(body)):
            raw, h = _signed(_payload())
            for _ in range(5):
                assert client.post("/webhook/meta", content=raw, headers=h).status_code == 200
        assert len(_rows(db)) == 1
        assert len(seen) == 1

    def test_distinct_deliveries_are_distinct_rows(self, client, db, inbox_on):
        with patch.object(meta_webhook, "_process_payload"):
            for n in range(3):
                raw, h = _signed(_payload(n))
                client.post("/webhook/meta", content=raw, headers=h)
        assert len(_rows(db)) == 3

    def test_shadow_mode_stores_but_old_path_processes(self, client, db, inbox_on, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_INBOX_MODE", "shadow")
        with patch.object(meta_webhook, "_process_payload_bg") as old_path:
            raw, h = _signed(_payload())
            client.post("/webhook/meta", content=raw, headers=h)
        old_path.assert_called_once()
        [row] = _rows(db)
        assert row.status == "shadow"

    def test_off_mode_stores_nothing(self, client, db, inbox_on, monkeypatch):
        monkeypatch.setattr(settings, "WEBHOOK_INBOX_MODE", "off")
        with patch.object(meta_webhook, "_process_payload_bg"):
            raw, h = _signed(_payload())
            client.post("/webhook/meta", content=raw, headers=h)
        assert _rows(db) == []

    def test_store_failure_is_not_acknowledged(self, client, db, inbox_on):
        """Answering 200 without the row stored would lose the event for good."""
        with patch.object(webhook_inbox, "store", side_effect=RuntimeError("db down")):
            raw, h = _signed(_payload())
            r = client.post("/webhook/meta", content=raw, headers=h)
        assert r.status_code == 503


# ── Failure, retry, reclaim, dead ────────────────────────────────────────────

class TestRetry:
    def test_failure_schedules_a_retry_then_sweep_succeeds(self, client, db, inbox_on):
        with patch.object(meta_webhook, "_process_payload", side_effect=RuntimeError("boom")):
            raw, h = _signed(_payload())
            client.post("/webhook/meta", content=raw, headers=h)
        [row] = _rows(db)
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.next_attempt_at is not None
        assert "boom" in row.last_error

        # Not due yet: the sweep leaves it alone.
        with patch.object(meta_webhook, "_process_payload") as proc:
            assert webhook_inbox.sweep() == 0
            proc.assert_not_called()

        db.execute(text("UPDATE webhook_inbox SET next_attempt_at = now() - interval '1 s'"))
        db.commit()
        with patch.object(meta_webhook, "_process_payload") as proc:
            assert webhook_inbox.sweep() == 1
            proc.assert_called_once()
        [row] = _rows(db)
        assert row.status == "done"
        assert row.attempts == 2

    def test_crashed_worker_lease_is_reclaimed(self, db, inbox_on):
        """A row left in 'processing' (worker died mid-row) is picked up once its lease
        expires — and not before."""
        rid = webhook_inbox.store(db, b'{"x":1}', {"x": 1})
        db.execute(text("""UPDATE webhook_inbox SET status='processing',
                                  lease_until = now() + interval '1 hour' WHERE id=:id"""),
                   {"id": rid})
        db.commit()
        with patch.object(meta_webhook, "_process_payload") as proc:
            assert webhook_inbox.sweep() == 0
            proc.assert_not_called()

        db.execute(text("UPDATE webhook_inbox SET lease_until = now() - interval '1 s'"))
        db.commit()
        with patch.object(meta_webhook, "_process_payload") as proc:
            assert webhook_inbox.sweep() == 1
        assert _rows(db)[0].status == "done"

    def test_dead_after_max_attempts_and_copied_to_failed_webhooks(self, db, inbox_on):
        rid = webhook_inbox.store(db, b'{"x":2}', {"x": 2})
        db.execute(text("UPDATE webhook_inbox SET attempts=:n WHERE id=:id"),
                   {"n": webhook_inbox.MAX_ATTEMPTS - 1, "id": rid})
        db.commit()
        with patch.object(meta_webhook, "_process_payload", side_effect=ValueError("bad")):
            webhook_inbox.sweep()
        [row] = _rows(db)
        assert row.status == "dead"
        [fw] = db.query(FailedWebhook).all()
        assert fw.raw_payload == {"x": 2}
        assert fw.error_type == "ValueError"

    def test_prune_keeps_recent_and_dead_rows(self, db, inbox_on):
        old = datetime.now(timezone.utc) - timedelta(days=webhook_inbox.KEEP_DONE_DAYS + 1)
        for i, status in enumerate(("done", "shadow", "dead")):
            db.add(WebhookInbox(payload={"i": i}, payload_sha256=f"{i:064d}",
                                status=status, received_at=old))
        db.add(WebhookInbox(payload={"i": 9}, payload_sha256="9" * 64, status="done"))
        db.commit()
        assert webhook_inbox.prune() == 2
        assert sorted(r.status for r in _rows(db)) == ["dead", "done"]


# ── Redis fast path ──────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self.keys = {}

    def get(self, k):
        return self.keys.get(k)

    def setex(self, k, _ttl, v):
        self.keys[k] = v


class TestRedisDedupOrdering:
    def test_key_not_written_when_processing_fails(self, db):
        fake = _FakeRedis()
        with patch.object(conversation_engine, "get_redis", return_value=fake), \
             patch.object(conversation_engine, "handle_inbound", side_effect=RuntimeError("x")):
            with pytest.raises(RuntimeError):
                meta_webhook._handle_message(db, {"id": "wamid.R1"}, account=None)
        assert fake.keys == {}

    def test_key_written_after_success(self, db):
        fake = _FakeRedis()
        with patch.object(conversation_engine, "get_redis", return_value=fake), \
             patch.object(conversation_engine, "handle_inbound"):
            meta_webhook._handle_message(db, {"id": "wamid.R2"}, account=None)
        assert "wamid:wamid.R2" in fake.keys


# ── Silence alarm ────────────────────────────────────────────────────────────

class TestSilenceAlarm:
    def _sent(self, db, company_id, minutes_ago: int):
        c = BroadcastCampaign(company_id=company_id, name="c", status="sending")
        db.add(c)
        db.flush()
        db.add(BroadcastRecipient(
            campaign_id=c.id, mobile_no="918075571893", status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
        db.commit()

    def test_sending_number_with_no_webhooks_is_flagged(self, db):
        comp = make_company(db, code="SIL1")
        make_wa_account(db, comp.id, phone_number_id="PN-SILENT")
        self._sent(db, comp.id, minutes_ago=40)
        rows = webhook_inbox_scheduler.silent_numbers(db)
        assert [r.phone_number_id for r in rows] == ["PN-SILENT"]

    def test_number_hearing_back_is_not_flagged(self, db):
        comp = make_company(db, code="SIL2")
        acc = make_wa_account(db, comp.id, phone_number_id="PN-OK")
        acc.last_webhook_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        db.commit()
        self._sent(db, comp.id, minutes_ago=40)
        assert webhook_inbox_scheduler.silent_numbers(db) == []

    def test_just_sent_is_not_flagged_yet(self, db):
        """A send 5 minutes ago with no receipt yet is normal, not silence."""
        comp = make_company(db, code="SIL3")
        make_wa_account(db, comp.id, phone_number_id="PN-NEW")
        self._sent(db, comp.id, minutes_ago=5)
        assert webhook_inbox_scheduler.silent_numbers(db) == []

    def test_processing_records_last_webhook_at(self, db, monkeypatch):
        comp = make_company(db, code="SIL4")
        acc = make_wa_account(db, comp.id, phone_number_id="PN-INBOX")
        with patch.object(meta_webhook, "_handle_status"):
            meta_webhook._process_payload(_payload(), db)
        db.refresh(acc)
        assert acc.last_webhook_at is not None
