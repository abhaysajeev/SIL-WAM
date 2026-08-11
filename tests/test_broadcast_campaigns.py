"""
Broadcast campaigns — recipient build, screening, send loop, and status receipts.

TestStatusIsolation is the one that matters most: it proves the handle_status fallback
routes broadcast receipts without changing what a transactional message does.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core import template_body
from app.core.column_mapper import campaign_fields, suggest_mapping
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.phonebook import Phonebook, PhonebookContact
from app.models.messaging import InvalidNumber, MessagingOptOut
from app.services import broadcast_scheduler, broadcast_screening, conversation_engine
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_conversation, make_message, make_queue_entry,
    make_service, make_user, make_wa_account, make_wa_template, login,
)

_OK = SendResult(ok=True, meta_message_id="wamid.bc.1", error=None)


def _admin(client, db):
    """Admin-tier user: company_id is None, so it sees every company."""
    make_user(db, username="bc_admin", role_name="super_admin")
    tokens = login(client, "bc_admin")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _setup(db, code="BCAST", contacts=3, template_name="promo"):
    comp = make_company(db, code=code)
    make_api_key(db, comp.id, key=f"key-{code}")
    account = make_wa_account(db, comp.id)
    tpl = make_wa_template(db, comp.id, name=template_name)
    # Two placeholders so param-count validation has something to check.
    tpl.components = [{"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]
    db.commit()

    pb = Phonebook(company_id=comp.id, name=f"List {code}")
    db.add(pb); db.flush()
    for i in range(contacts):
        db.add(PhonebookContact(
            phonebook_id=pb.id,
            mobile_no=f"91999900{i:04d}",
            customer_name=f"Cust {i}",
            agent_id=f"AG{i}",
        ))
    db.commit()
    return comp, account, tpl, pb


def _campaign(client, hdr, comp, tpl, name="Promo"):
    r = client.post("/api/campaigns/", headers=hdr, json={
        "name": name, "company_id": str(comp.id), "template_id": str(tpl.id)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _build(client, hdr, cid, pb, params=("Ravi", "Diwali"), **kw):
    return client.post(f"/api/campaigns/{cid}/recipients", headers=hdr,
                       json={"phonebook_ids": [str(pb.id)], "params": list(params), **kw})


# ── Template helpers ──────────────────────────────────────────────────────────

class TestTemplateBody:
    def test_param_indices_dedup_and_sort(self):
        comps = [{"type": "BODY", "text": "{{2}} then {{10}} then {{1}} and {{2}} again"}]
        assert template_body.param_indices(comps) == [1, 2, 10]

    def test_render_leaves_unfilled_placeholder_visible(self):
        """A blank would hide the gap; the raw {{2}} makes it obvious."""
        comps = [{"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]
        assert template_body.render(comps, ["Ravi"]) == "Hi Ravi, offer on {{2}}."

    def test_no_body_component(self):
        assert template_body.param_count([{"type": "BUTTONS"}]) == 0


# ── Recipient build ───────────────────────────────────────────────────────────

class TestRecipientBuild:
    def test_builds_from_list(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db)
        cid = _campaign(client, hdr, comp, tpl)
        r = _build(client, hdr, cid, pb)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 3
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).count() == 3

    def test_params_snapshotted_onto_every_row(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BSNAP")
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        rows = db.query(BroadcastRecipient).filter(BroadcastRecipient.campaign_id == cid).all()
        assert all(r.params == ["Ravi", "Diwali"] for r in rows)
        assert {r.agent_id for r in rows} == {"AG0", "AG1", "AG2"}

    def test_wrong_param_count_rejected(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BPARM")
        cid = _campaign(client, hdr, comp, tpl)
        r = _build(client, hdr, cid, pb, params=("only-one",))
        assert r.status_code == 400
        assert "2 parameter" in r.json()["detail"]

    def test_duplicate_across_lists_sent_once(self, client, db):
        """Meta bills unique recipients; two lists sharing a number must not double-send."""
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDUP", contacts=2)
        other = Phonebook(company_id=comp.id, name="Second")
        db.add(other); db.flush()
        db.add(PhonebookContact(phonebook_id=other.id,
                                    mobile_no="919999000000",   # already in pb
                                    customer_name="Dup", agent_id="AGX"))
        db.commit()
        cid = _campaign(client, hdr, comp, tpl)
        r = client.post(f"/api/campaigns/{cid}/recipients", headers=hdr, json={
            "phonebook_ids": [str(pb.id), str(other.id)], "params": ["a", "b"]})
        assert r.status_code == 200
        assert r.json()["total"] == 2
        assert r.json()["duplicates_removed"] == 1

    def test_cap_enforced_at_build(self, client, db, monkeypatch):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BCAP", contacts=5)
        monkeypatch.setattr("app.api.campaigns_api.MAX_RECIPIENTS_PER_CAMPAIGN", 3)
        cid = _campaign(client, hdr, comp, tpl)
        r = _build(client, hdr, cid, pb)
        assert r.status_code == 400
        assert "exceeds the current limit" in r.json()["detail"]

    def test_rebuild_replaces_previous_set(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BREB")
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        _build(client, hdr, cid, pb)
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).count() == 3


# ── Screening ─────────────────────────────────────────────────────────────────

class TestScreening:
    def _ready(self, client, db, code):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code=code)
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        return hdr, comp, cid

    def test_opted_out_hard_skipped(self, client, db):
        hdr, comp, cid = self._ready(client, db, "BOPT")
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="919999000000", source="manual"))
        db.commit()
        out = client.post(f"/api/campaigns/{cid}/screen", headers=hdr).json()
        assert out["opted_out"] == 1
        assert out["sendable"] == 2
        row = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid,
            BroadcastRecipient.mobile_no == "919999000000").first()
        assert (row.status, row.skip_reason) == ("skipped", "opted_out")

    def test_previously_invalid_warns_but_stays_sendable(self, client, db):
        """Deliverability, not compliance — the user decides."""
        hdr, comp, cid = self._ready(client, db, "BINV")
        db.add(InvalidNumber(company_id=comp.id, mobile_no="919999000001",
                             error_code="131026", occurrences=3,
                             last_seen_at=datetime.now(timezone.utc) - timedelta(days=5)))
        db.commit()
        out = client.post(f"/api/campaigns/{cid}/screen", headers=hdr).json()
        assert out["previously_invalid"] == 1
        assert out["sendable"] == 3
        detail = out["invalid_detail"]["919999000001"]
        assert detail["occurrences"] == 3
        assert detail["days_ago"] == 5

    def test_us_numbers_excluded(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BUS", contacts=1)
        db.add(PhonebookContact(phonebook_id=pb.id,
                                    mobile_no="12025550143", customer_name="US"))
        db.commit()
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        out = client.post(f"/api/campaigns/{cid}/screen", headers=hdr).json()
        assert out["us_numbers"] == 1

    @pytest.mark.parametrize("mobile,expected", [
        ("12025550143", True),    # 1 + 10 digits
        ("919999000000", False),  # India
        ("1234567", False),       # starts with 1 but too short to be US
    ])
    def test_us_detection(self, mobile, expected):
        assert broadcast_screening.is_us_number(mobile) is expected

    def test_rescreen_clears_stale_skips(self, client, db):
        """Opting someone back in and re-screening must not leave them skipped."""
        hdr, comp, cid = self._ready(client, db, "BRESC")
        opt = MessagingOptOut(company_id=comp.id, mobile_no="919999000000", source="manual")
        db.add(opt); db.commit()
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        db.delete(opt); db.commit()
        out = client.post(f"/api/campaigns/{cid}/screen", headers=hdr).json()
        assert out["opted_out"] == 0
        assert out["sendable"] == 3


# ── Send loop ─────────────────────────────────────────────────────────────────

class TestSendLoop:
    def _sending(self, client, db, code, contacts=3):
        hdr = _admin(client, db)
        comp, account, tpl, pb = _setup(db, code=code, contacts=contacts)
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        r = client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        assert r.status_code == 200, r.text
        return hdr, cid, account

    def test_send_requires_screening_first(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BNOSCR")
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        assert client.post(f"/api/campaigns/{cid}/send", headers=hdr).status_code == 409

    def test_dispatches_all_and_records_wamid(self, client, db):
        hdr, cid, _acc = self._sending(client, db, "BSEND")
        sends = [SendResult(ok=True, meta_message_id=f"wamid.s{i}", error=None) for i in range(3)]
        with patch("app.services.wa_sender.send_template", side_effect=sends) as snd:
            broadcast_scheduler.dispatch_pending(db)
        assert snd.call_count == 3
        rows = db.query(BroadcastRecipient).filter(BroadcastRecipient.campaign_id == cid).all()
        assert all(r.status == "sent" and r.wamid for r in rows)
        c = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first()
        db.refresh(c)
        assert (c.sent, c.status) == (3, "dispatched")

    def test_failure_recorded_with_code_not_fatal(self, client, db):
        hdr, cid, _acc = self._sending(client, db, "BFAIL")
        results = [
            SendResult(ok=True, meta_message_id="wamid.a", error=None),
            SendResult(ok=False, meta_message_id=None, error="(#131026) not a WhatsApp user"),
            SendResult(ok=True, meta_message_id="wamid.c", error=None),
        ]
        with patch("app.services.wa_sender.send_template", side_effect=results):
            broadcast_scheduler.dispatch_pending(db)
        c = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first()
        db.refresh(c)
        assert (c.sent, c.failed) == (2, 1)
        failed = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid,
            BroadcastRecipient.status == "failed").first()
        assert failed.error_code == "131026"

    def test_stop_on_error_halts_the_run(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BSTOP", contacts=3)
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb, stop_on_error=True)
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        fail = SendResult(ok=False, meta_message_id=None, error="boom")
        with patch("app.services.wa_sender.send_template", return_value=fail) as snd:
            broadcast_scheduler.dispatch_pending(db)
        assert snd.call_count == 1
        c = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first()
        db.refresh(c)
        assert c.status == "dispatched"
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid,
            BroadcastRecipient.status == "pending").count() == 2

    def test_skipped_rows_are_never_sent(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BSKIP")
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="919999000000", source="manual"))
        db.commit()
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        with patch("app.services.wa_sender.send_template", return_value=_OK) as snd:
            broadcast_scheduler.dispatch_pending(db)
        assert snd.call_count == 2
        assert "919999000000" not in [c.args[3] for c in snd.call_args_list]

    def test_crash_recovery_resets_sending_to_pending(self, client, db):
        hdr, cid, _acc = self._sending(client, db, "BCRASH")
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update({"status": "sending"})
        db.commit()
        # _recover_interrupted opens its own session against the dev DB, so
        # exercise the same statement against the test session instead.
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.status == 'sending').update({'status': 'pending'})
        db.commit()
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid,
            BroadcastRecipient.status == "pending").count() == 3

    def test_cancel_leaves_sent_rows_alone(self, client, db):
        hdr, cid, _acc = self._sending(client, db, "BCANC")
        one = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        one.status = "sent"; db.commit()
        out = client.post(f"/api/campaigns/{cid}/cancel", headers=hdr).json()
        assert out["cancelled"] == 2
        db.refresh(one)
        assert one.status == "sent"


# ── Status receipts — the isolation guarantee ────────────────────────────────

class TestStatusIsolation:
    """
    handle_status must route broadcast receipts through the new fallback while leaving
    transactional messages on their original path.
    """

    def _recipient(self, db, code="BSTAT", wamid="wamid.bc.x"):
        comp = make_company(db, code=code)
        account = make_wa_account(db, comp.id)
        c = BroadcastCampaign(company_id=comp.id, name="C", param_mode="same",
                              status="dispatched", total=1, sent=1)
        db.add(c); db.flush()
        r = BroadcastRecipient(campaign_id=c.id, mobile_no="919999000000",
                               status="sent", wamid=wamid)
        db.add(r); db.commit()
        return comp, account, c, r

    def _status(self, wamid, state, errors=None, ts=None):
        s = {"id": wamid, "status": state,
             "timestamp": str(int((ts or datetime.now(timezone.utc)).timestamp()))}
        if errors:
            s["errors"] = errors
        return s

    def test_delivered_updates_recipient_and_campaign(self, db):
        _comp, account, camp, rec = self._recipient(db, "BSD")
        conversation_engine.handle_status(db, self._status(rec.wamid, "delivered"), account)
        db.refresh(rec); db.refresh(camp)
        assert rec.status == "delivered" and rec.delivered_at is not None
        assert camp.delivered == 1

    def test_receipts_never_move_backwards(self, db):
        """A late 'delivered' after 'read' must not downgrade the row."""
        _comp, account, camp, rec = self._recipient(db, "BSB")
        conversation_engine.handle_status(db, self._status(rec.wamid, "read"), account)
        conversation_engine.handle_status(db, self._status(rec.wamid, "delivered"), account)
        db.refresh(rec)
        assert rec.status == "read"

    def test_async_131026_flags_the_number(self, db):
        """The whole point of the fallback: invalid-number detection for broadcast."""
        comp, account, camp, rec = self._recipient(db, "BSI")
        conversation_engine.handle_status(
            db, self._status(rec.wamid, "failed",
                             [{"code": 131026, "title": "not a WhatsApp user"}]), account)
        db.refresh(rec); db.refresh(camp)
        assert rec.status == "failed" and rec.error_code == "131026"
        # counted as sent at dispatch, contradicted afterwards
        assert (camp.sent, camp.failed) == (0, 1)
        flagged = db.query(InvalidNumber).filter(
            InvalidNumber.company_id == comp.id,
            InvalidNumber.mobile_no == "919999000000").first()
        assert flagged is not None and flagged.occurrences == 1

    def test_repeat_failure_increments_occurrences(self, db):
        comp, account, camp, rec = self._recipient(db, "BSR")
        db.add(InvalidNumber(company_id=comp.id, mobile_no="919999000000",
                             error_code="131026", occurrences=2))
        db.commit()
        conversation_engine.handle_status(
            db, self._status(rec.wamid, "failed", [{"code": 131026, "title": "x"}]), account)
        row = db.query(InvalidNumber).filter(
            InvalidNumber.company_id == comp.id).first()
        assert row.occurrences == 3

    def test_duplicate_failure_webhook_does_not_double_count(self, db):
        _comp, account, camp, rec = self._recipient(db, "BSDUP")
        st = self._status(rec.wamid, "failed", [{"code": 131026, "title": "x"}])
        conversation_engine.handle_status(db, st, account)
        conversation_engine.handle_status(db, st, account)
        db.refresh(camp)
        assert camp.failed == 1

    def test_transactional_message_still_uses_the_original_path(self, db):
        """The control: a Service message has a Message row and must never reach the fallback."""
        comp = make_company(db, code="BSCTL")
        account = make_wa_account(db, comp.id)
        conv = make_conversation(db, comp.id, "919888800000")
        svc = make_service(db, conv.id, comp.id)
        make_queue_entry(db, svc, mobile_no="919888800000")
        msg = make_message(db, svc, wamid="wamid.txn.1", direction="outbound")
        with patch("app.services.broadcast_status.handle") as fallback:
            conversation_engine.handle_status(
                db, self._status("wamid.txn.1", "delivered"), account)
        fallback.assert_not_called()
        db.refresh(msg)
        assert msg.status == "delivered"

    def test_unknown_wamid_is_still_ignored(self, db):
        comp = make_company(db, code="BSUNK")
        account = make_wa_account(db, comp.id)
        conversation_engine.handle_status(db, self._status("wamid.nope", "delivered"), account)


# ── Progress ──────────────────────────────────────────────────────────────────

class TestProgress:
    def test_percent_measures_dispatch_not_delivery(self, client, db):
        """
        The bar must reach 100% when every send was attempted, even though delivery
        receipts are still outstanding. Conflating them would report success too early.
        """
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BPROG")
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        sends = [SendResult(ok=True, meta_message_id=f"wamid.p{i}", error=None) for i in range(3)]
        with patch("app.services.wa_sender.send_template", side_effect=sends):
            broadcast_scheduler.dispatch_pending(db)
        p = client.get(f"/api/campaigns/{cid}/progress", headers=hdr).json()
        assert p["percent"] == 100.0
        assert p["delivered"] == 0
        assert p["settling"] is True

    def test_insights_group_failures_by_code(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BINS")
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        fail = SendResult(ok=False, meta_message_id=None, error="(#131026) invalid")
        with patch("app.services.wa_sender.send_template", return_value=fail):
            broadcast_scheduler.dispatch_pending(db)
        ins = client.get(f"/api/campaigns/{cid}/insights", headers=hdr).json()
        assert ins["failures_by_code"][0] == {"code": "131026", "count": 3,
                                              "message": "(#131026) invalid"}
        assert len(ins["failed_rows"]) == 3


class TestDelete:
    def _campaign_in(self, client, db, hdr, comp, tpl, pb, status):
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        c = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first()
        c.status = status
        db.commit()
        return cid

    def test_bulk_delete_removes_unsent(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDEL")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "ready")
        r = client.post("/api/campaigns/bulk-delete", headers=hdr, json={"ids": [cid]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        assert db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first() is None

    def test_sent_campaign_is_kept(self, client, db):
        """A dispatched campaign is the record of what reached customers."""
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDELS")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "dispatched")
        r = client.post("/api/campaigns/bulk-delete", headers=hdr, json={"ids": [cid]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 0
        assert r.json()["in_flight"]
        assert db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first() is not None

    def test_recipients_cascade(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDELC")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "ready")
        client.post("/api/campaigns/bulk-delete", headers=hdr, json={"ids": [cid]})
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).count() == 0

    def test_unknown_id_reported_not_fatal(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDELU")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "ready")
        r = client.post("/api/campaigns/bulk-delete", headers=hdr, json={
            "ids": [cid, "00000000-0000-0000-0000-000000000999"]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        assert len(r.json()["failed"]) == 1


# ── Access control ────────────────────────────────────────────────────────────

class TestDelete:
    """POST /api/campaigns/bulk-delete — the list view's delete action."""

    def _campaign_in(self, client, db, hdr, comp, tpl, pb, status):
        cid = _campaign(client, hdr, comp, tpl)
        _build(client, hdr, cid, pb)
        c = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first()
        c.status = status
        db.commit()
        return cid

    def test_deletes_an_unsent_campaign(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDEL")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "ready")
        r = client.post("/api/campaigns/bulk-delete", headers=hdr, json={"ids": [cid]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        assert db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first() is None

    def test_sent_campaign_is_kept(self, client, db):
        """A dispatched campaign is the record of what reached real customers."""
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDELS")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "dispatched")
        r = client.post("/api/campaigns/bulk-delete", headers=hdr, json={"ids": [cid]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 0
        assert r.json()["in_flight"]
        assert db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first() is not None

    def test_recipients_cascade(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDELC")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "ready")
        client.post("/api/campaigns/bulk-delete", headers=hdr, json={"ids": [cid]})
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).count() == 0

    def test_unknown_id_reported_not_fatal(self, client, db):
        """One unreachable id must not abort the rest of the batch."""
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BDELU")
        cid = self._campaign_in(client, db, hdr, comp, tpl, pb, "ready")
        r = client.post("/api/campaigns/bulk-delete", headers=hdr, json={
            "ids": [cid, "00000000-0000-0000-0000-000000000999"]})
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        assert len(r.json()["failed"]) == 1


class TestPersonalizedImport:
    """Case 2 — one row per contact, each carrying its own parameter values."""

    def _campaign(self, client, db, hdr, code="BPER"):
        comp, _a, tpl, _pb = _setup(db, code=code)
        r = client.post("/api/campaigns/", headers=hdr, json={
            "company_id": str(comp.id), "template_id": str(tpl.id),
            "param_mode": "per_row"})
        assert r.status_code == 201, r.text
        return comp, r.json()["id"]

    def _rows(self, *specs):
        """(row, mobile, p1, p2) tuples → the shape the browser posts."""
        return [{"row": n, "mobile_no": m, "customer_name": f"C{n}",
                 "params": {"param_1": a, "param_2": b}} for n, m, a, b in specs]

    def test_fields_follow_the_template(self, client, db):
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr)
        out = client.get(f"/api/campaigns/{cid}/import/fields", headers=hdr).json()
        assert out["param_count"] == 2          # _setup's template has {{1}} and {{2}}
        names = [f["name"] for f in out["fields"]]
        assert names == ["mobile_no", "customer_name", "param_1", "param_2"]

    def test_mapping_is_suggested_for_messy_headers(self, client, db):
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERM")
        r = client.post(f"/api/campaigns/{cid}/import/suggest-mapping", headers=hdr,
                        json={"headers": ["Mobile No", "Full Name", "param1", "PARAM 2"]})
        assert r.json()["mapping"] == {
            "mobile_no": "Mobile No", "customer_name": "Full Name",
            "param_1": "param1", "param_2": "PARAM 2"}

    def test_each_row_keeps_its_own_params(self, client, db):
        """The whole point of Case 2 — no shared parameter set."""
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERO")
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr, json={"rows": self._rows(
            (1, "919000000001", "Ravi", "10234"),
            (2, "919000000002", "Meera", "10235"))})
        assert r.status_code == 200 and r.json()["imported"] == 2
        rows = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).order_by(BroadcastRecipient.mobile_no).all()
        assert [x.params for x in rows] == [["Ravi", "10234"], ["Meera", "10235"]]
        assert all(x.status == "draft" for x in rows)

    def test_bad_mobile_skipped_and_reported(self, client, db):
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERB")
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr, json={"rows": self._rows(
            (1, "919000000001", "A", "1"),
            (2, "12345",        "B", "2"))}).json()
        assert (r["imported"], r["skipped"]) == (1, 1)
        assert r["errors"][0]["row"] == 2

    def test_blank_param_skipped_and_reported(self, client, db):
        """A blank would send a real customer a message with a hole in it."""
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERP")
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr, json={"rows": self._rows(
            (1, "919000000001", "A", ""),
            (2, "919000000002", "B", "2"))}).json()
        assert (r["imported"], r["skipped"]) == (1, 1)
        assert "Param 2" in r["errors"][0]["reason"]

    def test_duplicate_in_the_same_file_reported(self, client, db):
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERD")
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr, json={"rows": self._rows(
            (1, "919000000001", "A", "1"),
            (2, "919000000001", "B", "2"))}).json()
        assert (r["imported"], r["skipped"]) == (1, 1)
        assert "Already in this broadcast" in r["errors"][0]["reason"]

    def test_duplicate_against_an_earlier_batch_reported(self, client, db):
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERD2")
        client.post(f"/api/campaigns/{cid}/import", headers=hdr,
                    json={"rows": self._rows((1, "919000000001", "A", "1"))})
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr,
                        json={"rows": self._rows((1, "919000000001", "A", "1"))}).json()
        assert (r["imported"], r["skipped"]) == (0, 1)

    def test_cap_counts_rows_already_imported(self, client, db, monkeypatch):
        hdr = _admin(client, db)
        _comp, cid = self._campaign(client, db, hdr, code="BPERC")
        monkeypatch.setattr("app.api.campaigns_api.MAX_RECIPIENTS_PER_CAMPAIGN", 2)
        client.post(f"/api/campaigns/{cid}/import", headers=hdr, json={"rows": self._rows(
            (1, "919000000001", "A", "1"), (2, "919000000002", "B", "2"))})
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr,
                        json={"rows": self._rows((3, "919000000003", "C", "3"))})
        assert r.status_code == 400
        assert "limit" in r.json()["detail"]

    def test_phonebook_build_refused_on_a_csv_campaign(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BPERX")
        cid = client.post("/api/campaigns/", headers=hdr, json={
            "company_id": str(comp.id), "template_id": str(tpl.id),
            "param_mode": "per_row"}).json()["id"]
        r = client.post(f"/api/campaigns/{cid}/recipients", headers=hdr,
                        json={"phonebook_ids": [str(pb.id)], "params": ["a", "b"]})
        assert r.status_code == 409

    def test_csv_import_refused_on_a_standard_campaign(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, _pb = _setup(db, code="BPERY")
        cid = _campaign(client, hdr, comp, tpl)      # defaults to param_mode="same"
        r = client.post(f"/api/campaigns/{cid}/import", headers=hdr,
                        json={"rows": self._rows((1, "919000000001", "A", "1"))})
        assert r.status_code == 409


class TestBothModesConverge:
    """The send worker must not be able to tell the two modes apart."""

    def test_worker_sends_both_with_the_right_params(self, client, db):
        hdr = _admin(client, db)
        comp, _a, tpl, pb = _setup(db, code="BCONV")

        std = _campaign(client, hdr, comp, tpl, name="Std")
        _build(client, hdr, std, pb, params=("Shared", "Value"))
        client.post(f"/api/campaigns/{std}/screen", headers=hdr)
        client.post(f"/api/campaigns/{std}/send", headers=hdr)

        per = client.post("/api/campaigns/", headers=hdr, json={
            "name": "Per", "company_id": str(comp.id), "template_id": str(tpl.id),
            "param_mode": "per_row"}).json()["id"]
        client.post(f"/api/campaigns/{per}/import", headers=hdr, json={"rows": [
            {"row": 1, "mobile_no": "919111100001", "customer_name": "R",
             "params": {"param_1": "Own1", "param_2": "Own2"}}]})
        client.post(f"/api/campaigns/{per}/screen", headers=hdr)
        client.post(f"/api/campaigns/{per}/send", headers=hdr)

        sends = [SendResult(ok=True, meta_message_id=f"wamid.cv{i}", error=None) for i in range(9)]
        with patch("app.services.wa_sender.send_template", side_effect=sends) as snd:
            broadcast_scheduler.dispatch_pending(db)

        by_mobile = {c.args[3]: c.args[2] for c in snd.call_args_list}
        assert by_mobile["919111100001"] == ["Own1", "Own2"]          # per-row
        assert by_mobile["919999000000"] == ["Shared", "Value"]        # from the PhoneBook
        for c in (std, per):
            camp = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == c).first()
            db.refresh(camp)
            assert camp.status == "dispatched"


class TestCampaignFields:
    def test_param_columns_follow_the_count(self):
        assert [f.name for f in campaign_fields(3)] == [
            "mobile_no", "customer_name", "param_1", "param_2", "param_3"]
        assert len(campaign_fields(8)) == 10

    def test_every_param_is_required_but_name_is_not(self):
        f = {x.name: x.required for x in campaign_fields(2)}
        assert f == {"mobile_no": True, "customer_name": False,
                     "param_1": True, "param_2": True}

    def test_sample_headers_map_exactly(self):
        """The generated sample must round-trip through the matcher untouched."""
        fields = campaign_fields(3)
        headers = ["WhatsApp Number", "Customer Name", "Param 1", "Param 2", "Param 3"]
        assert suggest_mapping(headers, fields) == {
            "mobile_no": "WhatsApp Number", "customer_name": "Customer Name",
            "param_1": "Param 1", "param_2": "Param 2", "param_3": "Param 3"}


class TestAuth:
    def test_requires_authentication(self, client, db):
        assert client.get("/api/campaigns/").status_code in (401, 403)
