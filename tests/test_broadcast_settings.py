"""
Per-company broadcast settings, the concurrent send loop, retries, and the
"unconfirmed" status.

The theme running through these: **a send that WhatsApp accepted is not a send that
arrived**, and the machinery added here exists to stop the system implying otherwise —
either by leaving a recipient in "awaiting" forever, or by sending them the same
message twice while recovering from a crash.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core import broadcast_config
from app.models.agent import Agent
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.broadcast_settings import BroadcastSettings
from app.models.phonebook import Phonebook, PhonebookContact
from app.services import broadcast_scheduler
from app.services.wa_sender import SendResult
from tests.conftest import (
    make_api_key, make_company, make_user, make_wa_account, make_wa_template, login,
)

_OK = SendResult(ok=True, meta_message_id="wamid.set.1", error=None)

# A network-level failure: wa_sender flags these at the except site, where the
# exception type still exists.
_TRANSIENT = SendResult(ok=False, meta_message_id=None,
                        error="WhatsApp send failed (internal error)", is_transient=True)
# A rejection Meta will repeat identically however many times it is asked.
_PERMANENT = SendResult(ok=False, meta_message_id=None,
                        error="(#131026) not a WhatsApp user", is_transient=False)


def _admin(client, db, username="bs_admin"):
    make_user(db, username=username, role_name="super_admin")
    tokens = login(client, username)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _setup(db, code="BSET", contacts=3):
    comp = make_company(db, code=code)
    make_api_key(db, comp.id, key=f"key-{code}")
    account = make_wa_account(db, comp.id)
    tpl = make_wa_template(db, comp.id, name=f"promo_{code.lower()}")
    tpl.components = [{"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]
    db.commit()

    pb = Phonebook(company_id=comp.id, name=f"List {code}")
    db.add(pb); db.flush()
    for i in range(contacts):
        db.add(PhonebookContact(
            phonebook_id=pb.id, mobile_no=f"9188880{i:05d}",
            customer_name=f"Cust {i}", agent_id=f"AG{i}",
        ))
    db.commit()
    return comp, account, tpl, pb


def _sending_campaign(client, hdr, db, code, contacts=3):
    """A campaign screened and flipped to "sending", ready for the dispatch loop."""
    comp, account, tpl, pb = _setup(db, code=code, contacts=contacts)
    r = client.post("/api/campaigns/", headers=hdr, json={
        "name": f"C {code}", "company_id": str(comp.id), "template_id": str(tpl.id)})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    client.post(f"/api/campaigns/{cid}/recipients", headers=hdr,
                json={"phonebook_ids": [str(pb.id)], "params": ["Ravi", "Diwali"]})
    client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
    client.post(f"/api/campaigns/{cid}/send", headers=hdr)
    return comp, account, tpl, pb, cid


def _settings(db, company_id, **overrides):
    """Give a company an explicit settings row."""
    row = db.query(BroadcastSettings).filter(
        BroadcastSettings.company_id == company_id).first()
    if row is None:
        row = BroadcastSettings(company_id=company_id)
        db.add(row)
    for k, v in overrides.items():
        setattr(row, k, v)
    db.commit()
    return row


# ── The accessor ──────────────────────────────────────────────────────────────

class TestBroadcastConfig:
    def test_company_with_no_row_gets_the_defaults(self, db):
        """
        A company must never be unable to send because nobody visited a settings page.
        """
        comp = make_company(db, code="NOROW")
        db.query(BroadcastSettings).filter(
            BroadcastSettings.company_id == comp.id).delete()
        db.commit()
        assert broadcast_config.get(db, comp.id) == broadcast_config.DEFAULTS

    def test_defaults_match_the_behaviour_that_preceded_the_table(self, db):
        """
        Installing this feature must not change how anything sends. Batch size and the
        stale window were compiled-in constants; these are those values.
        """
        d = broadcast_config.DEFAULTS
        assert (d.batch_size, d.stale_claim_minutes) == (20, 5)
        assert d.agent_max_recipients_per_send == 100

    def test_row_values_are_returned(self, db):
        comp = make_company(db, code="ROWVAL")
        _settings(db, comp.id, send_concurrency=7, max_retries=4)
        t = broadcast_config.get(db, comp.id)
        assert (t.send_concurrency, t.max_retries) == (7, 4)

    def test_a_zero_concurrency_degrades_to_serial_rather_than_stalling(self, db):
        """
        Clamped, not rejected. A concurrency of 0 would dispatch nothing at all and
        read as a hung campaign — far worse than sending one at a time.
        """
        comp = make_company(db, code="ZEROC")
        _settings(db, comp.id, send_concurrency=0)
        assert broadcast_config.get(db, comp.id).send_concurrency == 1

    def test_stale_window_cannot_be_set_below_the_send_timeout(self, db):
        """
        wa_sender waits up to 20s. A shorter window lets recovery requeue a send that
        is still in flight, and that customer receives the message twice.
        """
        comp = make_company(db, code="STALE0")
        _settings(db, comp.id, stale_claim_minutes=0)
        assert broadcast_config.get(db, comp.id).stale_claim_minutes >= 1

    def test_the_snapshot_is_detached_from_the_session(self, db):
        """
        Worker threads read these values. A live ORM instance carries a Session that
        is not safe to touch from another thread.
        """
        comp = make_company(db, code="SNAP")
        _settings(db, comp.id, send_concurrency=3)
        t = broadcast_config.get(db, comp.id)
        assert not hasattr(t, "_sa_instance_state")
        with pytest.raises(Exception):
            t.send_concurrency = 9          # frozen dataclass


# ── The settings API ──────────────────────────────────────────────────────────

class TestBroadcastSettingsApi:
    def test_a_new_company_is_seeded_with_a_row(self, client, db):
        """Settings are per-company with no inheritance, so every company needs one."""
        hdr = _admin(client, db, "bs_seed")
        r = client.post("/api/companies/", headers=hdr, json={
            "name": "Seeded Co", "company_code": "SEEDCO"})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        row = db.query(BroadcastSettings).filter(
            BroadcastSettings.company_id == cid).first()
        assert row is not None
        assert row.agent_max_recipients_per_send == 100

    def test_update_persists(self, client, db):
        hdr = _admin(client, db, "bs_upd")
        comp = make_company(db, code="UPDCO")
        row = _settings(db, comp.id)
        r = client.put(f"/api/broadcast-settings/{row.id}", headers=hdr,
                       json={"send_concurrency": 25})
        assert r.status_code == 200, r.text
        assert r.json()["send_concurrency"] == 25

    def test_out_of_range_is_refused_at_the_edge(self, client, db):
        hdr = _admin(client, db, "bs_range")
        comp = make_company(db, code="RANGECO")
        row = _settings(db, comp.id)
        r = client.put(f"/api/broadcast-settings/{row.id}", headers=hdr,
                       json={"send_concurrency": 5000})
        assert r.status_code == 422

    def test_one_row_per_company(self, client, db):
        hdr = _admin(client, db, "bs_dup")
        comp = make_company(db, code="DUPCO")
        _settings(db, comp.id)
        r = client.post("/api/broadcast-settings/", headers=hdr,
                        json={"company_id": str(comp.id)})
        assert r.status_code == 409

    def test_deleting_resets_rather_than_disables(self, client, db):
        """The company keeps sending, on the defaults."""
        hdr = _admin(client, db, "bs_del")
        comp = make_company(db, code="DELCO")
        row = _settings(db, comp.id, send_concurrency=3)
        assert client.delete(f"/api/broadcast-settings/{row.id}",
                             headers=hdr).status_code == 204
        assert broadcast_config.get(db, comp.id) == broadcast_config.DEFAULTS


# ── Concurrency ───────────────────────────────────────────────────────────────

class TestConcurrentDispatch:
    def test_every_recipient_is_sent_exactly_once(self, client, db):
        """
        The property concurrency must not break. Order is now nondeterministic, so the
        assertion is on the set of numbers reached, never on the sequence.
        """
        hdr = _admin(client, db, "bs_conc")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "CONC", contacts=8)
        _settings(db, comp.id, send_concurrency=4)

        with patch("app.services.wa_sender.send_template", return_value=_OK) as snd:
            broadcast_scheduler.dispatch_pending(db)

        mobiles = [c.args[3] for c in snd.call_args_list]
        assert len(mobiles) == len(set(mobiles)) == 8
        rows = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).all()
        assert all(r.status == "sent" and r.wamid for r in rows)

    def test_sends_actually_overlap(self, client, db):
        """
        Proves the thread pool is real. The old loop was serial despite the design
        claiming otherwise, which is the difference between 6 minutes and 83 for a
        2,000-recipient campaign.
        """
        import threading
        hdr = _admin(client, db, "bs_overlap")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "OVLP", contacts=6)
        _settings(db, comp.id, send_concurrency=6)

        live = 0
        peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(6, timeout=10)

        def slow_send(*a, **kw):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            # Every worker must arrive before any may leave. Serial execution can
            # never satisfy this, so a regression to a plain loop times out here
            # rather than passing quietly.
            barrier.wait()
            with lock:
                live -= 1
            return _OK

        with patch("app.services.wa_sender.send_template", side_effect=slow_send):
            broadcast_scheduler.dispatch_pending(db)

        assert peak == 6

    def test_stop_on_error_forces_serial(self, client, db):
        """
        You cannot have ten sends in flight and also stop before the second. The flag
        is a serial promise, so it disables concurrency rather than being approximated.
        """
        hdr = _admin(client, db, "bs_stop")
        comp, _a, tpl, pb = _setup(db, code="STOPC", contacts=5)
        r = client.post("/api/campaigns/", headers=hdr, json={
            "name": "Stopper", "company_id": str(comp.id), "template_id": str(tpl.id)})
        cid = r.json()["id"]
        client.post(f"/api/campaigns/{cid}/recipients", headers=hdr, json={
            "phonebook_ids": [str(pb.id)], "params": ["Ravi", "Diwali"],
            "stop_on_error": True})
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        _settings(db, comp.id, send_concurrency=10)

        with patch("app.services.wa_sender.send_template",
                   return_value=_PERMANENT) as snd:
            broadcast_scheduler.dispatch_pending(db)

        assert snd.call_count == 1
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid,
            BroadcastRecipient.status == "pending").count() == 4


# ── Retries ───────────────────────────────────────────────────────────────────

class TestRetries:
    def test_a_transient_failure_is_requeued_with_backoff(self, client, db):
        hdr = _admin(client, db, "bs_retry")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "RETRY", contacts=1)
        _settings(db, comp.id, max_retries=2, retry_backoff_seconds=30)

        with patch("app.services.wa_sender.send_template", return_value=_TRANSIENT):
            broadcast_scheduler.dispatch_pending(db)

        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        db.refresh(r)
        assert r.status == "pending"        # not failed — it gets another go
        assert r.attempts == 1
        assert r.next_attempt_at is not None

    def test_a_deferred_row_is_not_claimed_before_its_time(self, client, db):
        """Otherwise the backoff is decorative and the retry storms Meta."""
        hdr = _admin(client, db, "bs_defer")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "DEFER", contacts=1)
        _settings(db, comp.id, max_retries=2, retry_backoff_seconds=600)

        with patch("app.services.wa_sender.send_template", return_value=_TRANSIENT):
            broadcast_scheduler.dispatch_pending(db)
        with patch("app.services.wa_sender.send_template", return_value=_OK) as snd:
            broadcast_scheduler.dispatch_pending(db)

        assert snd.call_count == 0

    def test_retries_are_exhausted_and_the_row_fails(self, client, db):
        hdr = _admin(client, db, "bs_exh")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "EXH", contacts=1)
        _settings(db, comp.id, max_retries=1, retry_backoff_seconds=1)

        for _ in range(2):
            db.query(BroadcastRecipient).filter(
                BroadcastRecipient.campaign_id == cid).update({"next_attempt_at": None})
            db.commit()
            with patch("app.services.wa_sender.send_template", return_value=_TRANSIENT):
                broadcast_scheduler.dispatch_pending(db)

        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        db.refresh(r)
        assert (r.status, r.attempts) == ("failed", 2)

    def test_a_rejection_meta_will_repeat_is_never_retried(self, client, db):
        """
        131026 fails identically every time. Retrying it burns the number's daily
        quota and signals bad list hygiene to Meta, which degrades delivery for every
        other campaign on that number.
        """
        hdr = _admin(client, db, "bs_perm")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "PERM", contacts=1)
        _settings(db, comp.id, max_retries=5)

        with patch("app.services.wa_sender.send_template", return_value=_PERMANENT):
            broadcast_scheduler.dispatch_pending(db)

        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        db.refresh(r)
        assert (r.status, r.attempts, r.error_code) == ("failed", 1, "131026")

    def test_zero_retries_reproduces_the_original_behaviour(self, client, db):
        hdr = _admin(client, db, "bs_zero")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "ZRETRY", contacts=1)
        _settings(db, comp.id, max_retries=0)

        with patch("app.services.wa_sender.send_template", return_value=_TRANSIENT):
            broadcast_scheduler.dispatch_pending(db)

        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        db.refresh(r)
        assert r.status == "failed"


# ── The duplicate-send guard ──────────────────────────────────────────────────

class TestCrashRecovery:
    def test_a_stranded_row_that_already_has_a_wamid_is_not_sent_again(self, client, db):
        """
        The bug this replaces: Meta accepted the send, the bookkeeping was lost, and
        recovery returned the row to "pending" so the customer was messaged twice.
        A wamid is proof the send happened.
        """
        hdr = _admin(client, db, "bs_dupe")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "DUPE", contacts=1)
        stale = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update({
                "status": "sending", "claimed_at": stale, "wamid": "wamid.already.sent"})
        db.commit()

        broadcast_scheduler.reclaim_stale(db)

        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        db.refresh(r)
        assert r.status == "sent"
        assert r.sent_at is not None

    def test_a_stranded_row_with_no_wamid_is_requeued(self, client, db):
        """The other half: no evidence it was sent, so it must be attempted."""
        hdr = _admin(client, db, "bs_req")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "REQ", contacts=2)
        stale = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update(
                {"status": "sending", "claimed_at": stale, "wamid": None})
        db.commit()

        assert broadcast_scheduler.reclaim_stale(db) == 2

    def test_an_in_flight_claim_is_left_alone(self, client, db):
        """
        Age is the only safe discriminator. Resetting a fresh claim would resend to a
        customer whose message is still on its way to Meta.
        """
        hdr = _admin(client, db, "bs_inflight")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "INFL", contacts=2)
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update(
                {"status": "sending", "claimed_at": datetime.now(timezone.utc)})
        db.commit()

        assert broadcast_scheduler.reclaim_stale(db) == 0


# ── "unconfirmed" ─────────────────────────────────────────────────────────────

class TestUnconfirmed:
    def test_a_row_with_no_receipt_stops_claiming_to_be_awaiting(self, client, db):
        hdr = _admin(client, db, "bs_unconf")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "UNCONF", contacts=2)
        _settings(db, comp.id, receipt_timeout_minutes=15)

        with patch("app.services.wa_sender.send_template", return_value=_OK):
            broadcast_scheduler.dispatch_pending(db)
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update(
                {"sent_at": datetime.now(timezone.utc) - timedelta(minutes=30)})
        db.commit()

        assert broadcast_scheduler.sweep_unconfirmed(db) == 2
        p = client.get(f"/api/campaigns/{cid}/progress", headers=hdr).json()
        assert p["unconfirmed"] == 2
        assert p["awaiting_confirmation"] == 0
        # Still counted as having reached Meta — it did.
        assert p["sent"] == 2

    def test_a_recent_send_is_left_awaiting(self, client, db):
        """Receipts normally arrive in seconds. Retiring one early would be a lie too."""
        hdr = _admin(client, db, "bs_recent")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "RECENT", contacts=2)
        _settings(db, comp.id, receipt_timeout_minutes=15)

        with patch("app.services.wa_sender.send_template", return_value=_OK):
            broadcast_scheduler.dispatch_pending(db)

        assert broadcast_scheduler.sweep_unconfirmed(db) == 0
        p = client.get(f"/api/campaigns/{cid}/progress", headers=hdr).json()
        assert p["awaiting_confirmation"] == 2 and p["unconfirmed"] == 0

    def test_a_late_receipt_still_wins(self, client, db):
        """
        "unconfirmed" is an admission of ignorance, not a verdict. Meta confirming
        delivery afterwards must still promote the row.
        """
        from app.services import conversation_engine
        hdr = _admin(client, db, "bs_late")
        comp, account, _t, _pb, cid = _sending_campaign(
            client, hdr, db, "LATE", contacts=1)

        with patch("app.services.wa_sender.send_template",
                   return_value=SendResult(ok=True, meta_message_id="wamid.late.1",
                                           error=None)):
            broadcast_scheduler.dispatch_pending(db)
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update(
                {"sent_at": datetime.now(timezone.utc) - timedelta(hours=2)})
        db.commit()
        broadcast_scheduler.sweep_unconfirmed(db)

        conversation_engine.handle_status(
            db, {"id": "wamid.late.1", "status": "delivered"}, account)
        db.commit()

        p = client.get(f"/api/campaigns/{cid}/progress", headers=hdr).json()
        assert (p["delivered"], p["unconfirmed"]) == (1, 0)

    def test_the_timeout_is_per_company(self, client, db):
        hdr = _admin(client, db, "bs_percomp")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "PERCO", contacts=1)
        _settings(db, comp.id, receipt_timeout_minutes=240)

        with patch("app.services.wa_sender.send_template", return_value=_OK):
            broadcast_scheduler.dispatch_pending(db)
        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update(
                {"sent_at": datetime.now(timezone.utc) - timedelta(minutes=60)})
        db.commit()

        # An hour old, but this company allows four.
        assert broadcast_scheduler.sweep_unconfirmed(db) == 0


# ── The agent cap ─────────────────────────────────────────────────────────────

class TestAgentSendCap:
    def _agent_campaign(self, client, db, hdr, code, contacts):
        """A campaign whose creator is an agent."""
        comp, _a, tpl, pb = _setup(db, code=code, contacts=contacts)
        r = client.post("/api/campaigns/", headers=hdr, json={
            "name": f"Agent {code}", "company_id": str(comp.id),
            "template_id": str(tpl.id)})
        cid = r.json()["id"]
        client.post(f"/api/campaigns/{cid}/recipients", headers=hdr,
                    json={"phonebook_ids": [str(pb.id)], "params": ["Ravi", "Diwali"]})
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)

        camp = db.query(BroadcastCampaign).filter(BroadcastCampaign.id == cid).first()
        agent_user = make_user(db, username=f"agent_{code.lower()}",
                               role_name="super_admin", company_id=comp.id)
        db.add(Agent(user_id=agent_user.id, agent_id=f"AG-{code}",
                     customer_id=f"CUST-{code}"))
        camp.created_by_id = agent_user.id
        db.commit()
        return comp, cid

    def test_an_agent_broadcast_above_the_cap_is_refused(self, client, db):
        hdr = _admin(client, db, "bs_cap")
        comp, cid = self._agent_campaign(client, db, hdr, "CAP", contacts=5)
        _settings(db, comp.id, agent_max_recipients_per_send=3)

        r = client.post(f"/api/campaigns/{cid}/send", headers=hdr)
        assert r.status_code == 403
        assert "3" in r.json()["detail"]
        # Nothing was queued — the refusal happens before any row is touched.
        assert db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid,
            BroadcastRecipient.status == "pending").count() == 0

    def test_at_the_cap_is_allowed(self, client, db):
        hdr = _admin(client, db, "bs_atcap")
        comp, cid = self._agent_campaign(client, db, hdr, "ATCAP", contacts=3)
        _settings(db, comp.id, agent_max_recipients_per_send=3)
        assert client.post(f"/api/campaigns/{cid}/send", headers=hdr).status_code == 200

    def test_zero_means_unlimited(self, client, db):
        hdr = _admin(client, db, "bs_uncap")
        comp, cid = self._agent_campaign(client, db, hdr, "UNCAP", contacts=5)
        _settings(db, comp.id, agent_max_recipients_per_send=0)
        assert client.post(f"/api/campaigns/{cid}/send", headers=hdr).status_code == 200

    def test_an_admin_campaign_is_not_capped(self, client, db):
        """
        An administrator choosing a large phonebook meant to. The cap exists for the
        handset, where the same list is one tap away by accident.
        """
        hdr = _admin(client, db, "bs_admincap")
        comp, _a, tpl, pb = _setup(db, code="ADMCAP", contacts=5)
        r = client.post("/api/campaigns/", headers=hdr, json={
            "name": "Admin big", "company_id": str(comp.id), "template_id": str(tpl.id)})
        cid = r.json()["id"]
        client.post(f"/api/campaigns/{cid}/recipients", headers=hdr,
                    json={"phonebook_ids": [str(pb.id)], "params": ["Ravi", "Diwali"]})
        client.post(f"/api/campaigns/{cid}/screen", headers=hdr)
        _settings(db, comp.id, agent_max_recipients_per_send=2)

        assert client.post(f"/api/campaigns/{cid}/send", headers=hdr).status_code == 200


# ── The pages ─────────────────────────────────────────────────────────────────

class TestBroadcastSettingsPages:
    def _session(self, client, db, username, role_name="super_admin", pages=None):
        """
        Log in so the page routes see a session cookie. POST /api/auth/login sets it,
        which is how the rest of the SSR tests authenticate.
        """
        from app.core.resources import ALL_PAGE_NAMES
        from tests.conftest import make_role
        if role_name != "super_admin":
            make_role(db, name=role_name, display_name=role_name.title(),
                      pages=ALL_PAGE_NAMES if pages is None else pages,
                      actions=["read", "create", "write", "delete"])
        make_user(db, username=username, role_name=role_name)
        r = client.post("/api/auth/login",
                        json={"username": username, "password": "testpass123"})
        assert r.status_code == 200, r.text
        return client

    def test_list_page_renders(self, client, db):
        c = self._session(client, db, "pg_list")
        comp = make_company(db, code="PGLIST")
        _settings(db, comp.id, send_concurrency=6, max_retries=0)
        r = c.get("/broadcast-settings")
        assert r.status_code == 200
        # Rendered as prose, not bare integers whose units the reader has to guess.
        assert "6 at a time" in r.text
        assert "no retries" in r.text

    def test_new_page_prefills_the_defaults(self, client, db):
        """
        Blank inputs would make the form look like it sets values out of nothing.
        Showing today's values makes an edit visibly a change from them.
        """
        c = self._session(client, db, "pg_new")
        r = c.get("/broadcast-settings/new")
        assert r.status_code == 200
        assert 'value="100"' in r.text          # the agent cap
        assert 'type="number"' in r.text        # the field type added for this page

    def test_edit_page_renders_current_values(self, client, db):
        c = self._session(client, db, "pg_edit")
        comp = make_company(db, code="PGEDIT")
        row = _settings(db, comp.id, receipt_timeout_minutes=42)
        r = c.get(f"/broadcast-settings/{row.id}")
        assert r.status_code == 200
        assert 'value="42"' in r.text

    def test_a_role_without_the_permission_is_refused(self, client, db):
        """
        The check that normally cannot be made here: super_admin bypasses require(),
        so only a real role proves the page is gated at all.
        """
        c = self._session(client, db, "pg_noperm", role_name="limited",
                          pages=["dashboard"])
        assert c.get("/broadcast-settings").status_code == 403

    def test_the_sidebar_link_appears_for_a_permitted_role(self, client, db):
        c = self._session(client, db, "pg_side", role_name="sidebar_role")
        r = c.get("/dashboard")
        assert "/broadcast-settings" in r.text

    def test_the_resource_is_in_the_permission_matrix(self):
        """
        Without this the page can never be granted to anyone but super_admin.

        Note what this does NOT prove: that the *admin role* actually holds the
        permission. That comes from migration f2b5d8c03e49, and the test database is
        built by create_all rather than alembic, so no migration-seeded permission row
        exists here at all. Verify that by logging in as an admin — see CLAUDE.md.
        """
        from app.core.resources import ALL_PAGE_NAMES, VALID_ACTIONS
        assert "broadcast_settings" in ALL_PAGE_NAMES
        assert VALID_ACTIONS["broadcast_settings"] == {"read", "create", "write", "delete"}


class TestRetrySuccessIsClean:
    def test_a_row_that_succeeds_on_retry_carries_no_stale_error(self, client, db):
        """
        The campaign page prints error_message under the status pill. A row that
        failed once and then sent would otherwise show an error beneath a successful
        send, describing something that no longer happened.
        """
        hdr = _admin(client, db, "bs_clean")
        comp, _a, _t, _pb, cid = _sending_campaign(client, hdr, db, "CLEAN", contacts=1)
        _settings(db, comp.id, max_retries=2, retry_backoff_seconds=1)

        with patch("app.services.wa_sender.send_template", return_value=_TRANSIENT):
            broadcast_scheduler.dispatch_pending(db)
        r = db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).first()
        db.refresh(r)
        assert r.error_message                      # the failed attempt recorded one

        db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == cid).update({"next_attempt_at": None})
        db.commit()
        with patch("app.services.wa_sender.send_template", return_value=_OK):
            broadcast_scheduler.dispatch_pending(db)

        db.refresh(r)
        assert r.status == "sent"
        assert r.error_message is None and r.error_code is None
        assert r.next_attempt_at is None
