"""
Phase 6 — account-level failures and Meta's account webhooks.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api import meta_webhook
from app.core.config import settings
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.services import account_health
from tests.conftest import make_company, make_wa_account, make_wa_template


def _campaign(db, comp, failures: int, code="131042", delivered: int = 0):
    c = BroadcastCampaign(company_id=comp.id, name="c", status="sending",
                          dispatched_at=datetime.now(timezone.utc))
    db.add(c)
    db.flush()
    for i in range(failures):
        db.add(BroadcastRecipient(campaign_id=c.id, mobile_no=f"91900000{i:04d}",
                                  status="failed", error_code=code))
    for i in range(delivered):
        db.add(BroadcastRecipient(campaign_id=c.id, mobile_no=f"91800000{i:04d}",
                                  status="delivered",
                                  delivered_at=datetime.now(timezone.utc)))
    db.commit()
    return c


@pytest.fixture()
def enforce(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_BREAKER_MODE", "enforce")


def test_billing_failures_trip_the_breaker_alert_mode(db, monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_BREAKER_MODE", "alert")
    comp = make_company(db, code="AH1")
    acc = make_wa_account(db, comp.id, phone_number_id="PN-AH1")
    c = _campaign(db, comp, failures=5)
    assert account_health.check(db)["tripped"] == 1
    db.refresh(acc)
    db.refresh(c)
    assert acc.blocked_reason.startswith("131042")
    assert c.status == "sending"                      # alert mode keeps sending
    assert account_health.block_for(db, comp.id) is None
    assert account_health.status_for(db, comp.id)["state"] == "warning"


def test_enforce_pauses_campaign_and_blocks_sends(db, enforce):
    comp = make_company(db, code="AH2")
    make_wa_account(db, comp.id, phone_number_id="PN-AH2")
    c = _campaign(db, comp, failures=6)
    account_health.check(db)
    db.refresh(c)
    assert c.status == "paused"
    assert account_health.block_for(db, comp.id)
    assert account_health.status_for(db, comp.id)["state"] == "blocked"


def test_below_threshold_or_with_deliveries_does_not_trip(db, enforce):
    comp = make_company(db, code="AH3")
    make_wa_account(db, comp.id, phone_number_id="PN-AH3")
    _campaign(db, comp, failures=4)
    comp2 = make_company(db, code="AH4")
    make_wa_account(db, comp2.id, phone_number_id="PN-AH4")
    _campaign(db, comp2, failures=10, delivered=1)
    assert account_health.check(db)["tripped"] == 0


def test_ordinary_failures_do_not_trip(db, enforce):
    comp = make_company(db, code="AH5")
    make_wa_account(db, comp.id, phone_number_id="PN-AH5")
    _campaign(db, comp, failures=50, code="131026")     # bad numbers, not the account
    assert account_health.check(db)["tripped"] == 0


def test_block_clears_after_a_delivery(db, enforce):
    comp = make_company(db, code="AH6")
    acc = make_wa_account(db, comp.id, phone_number_id="PN-AH6")
    acc.blocked_reason, acc.blocked_at = "131042: x", datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    _campaign(db, comp, failures=0, delivered=1)
    assert account_health.check(db)["cleared"] == 1
    db.refresh(acc)
    assert acc.blocked_at is None


def test_agent_send_refused_when_blocked(client, db, enforce):
    from tests.test_agent_send import BROADCAST, _agent_headers, _setup
    comp, acc, tpl, pb, *_ = _setup(client, db, "AH7")
    acc.blocked_reason, acc.blocked_at = "131042: billing", datetime.now(timezone.utc)
    db.commit()
    r = client.post(BROADCAST, headers=_agent_headers(client), json={
        "phonebook_ids": [str(pb.id)], "template_id": str(tpl.id), "params": ["a", "b"]})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "account_unavailable"


# ── Webhook fields ───────────────────────────────────────────────────────────

def _field_payload(field, value, waba="WABA-1"):
    return {"entry": [{"id": waba, "changes": [{"field": field, "value": value}]}]}


def test_template_paused_by_meta_updates_our_copy(db):
    comp = make_company(db, code="AH8")
    tpl = make_wa_template(db, comp.id, name="promo_x")
    tpl.meta_template_id = "998877"
    db.commit()
    meta_webhook._process_payload(_field_payload("message_template_status_update", {
        "event": "PAUSED", "message_template_id": 998877,
        "message_template_name": "promo_x", "message_template_language": "en_US"}), db)
    db.refresh(tpl)
    assert tpl.status == "PAUSED"
    meta_webhook._process_payload(_field_payload("message_template_status_update", {
        "event": "REINSTATED", "message_template_id": 998877}), db)
    db.refresh(tpl)
    assert tpl.status == "APPROVED"


def test_quality_update_records_tier(db):
    comp = make_company(db, code="AH9")
    acc = make_wa_account(db, comp.id, phone_number_id="PN-AH9")
    acc.waba_id = "WABA-Q"
    db.commit()
    meta_webhook._process_payload(_field_payload("phone_number_quality_update", {
        "event": "UPGRADE", "current_limit": "TIER_10K"}, waba="WABA-Q"), db)
    db.refresh(acc)
    assert (acc.messaging_limit_tier, acc.quality_event) == ("TIER_10K", "UPGRADE")


def test_limits_shows_account_state(client, db):
    from tests.test_agent_send import _agent_headers, _setup
    _setup(client, db, "AH10")
    b = client.get("/agent-api/v1/limits", headers=_agent_headers(client)).json()
    assert b["account"]["state"] == "ok"
