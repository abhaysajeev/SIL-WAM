"""
The agent app's send paths and chat window — /agent-api/v1/{messages,broadcasts,threads}.

Three properties carry the weight here:

  **The 24-hour window is enforced server-side.** A device clock can be wrong by hours,
  and Meta rejects a late free-text send with 131047 whatever the handset displayed. The
  app's countdown is a courtesy; this is the authority.

  **An agent broadcast is an ordinary campaign.** Same table, same builder, same
  screening, same queue. A second send path would be a second place for opt-out
  enforcement to be forgotten.

  **Pagination is keyset, never offset.** A message arriving mid-scroll must not shift a
  page and silently hide a customer's question.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.agent import Agent, AgentPhonebook, AgentTemplate
from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.broadcast_settings import BroadcastSettings
from app.models.messaging import MessagingOptOut
from app.models.phonebook import Phonebook, PhonebookContact
from app.services import agent_timeline
from app.services.wa_sender import SendResult
from tests.conftest import (
    login, make_company, make_user, make_wa_account, make_wa_template,
)

LOGIN     = "/agent-api/v1/auth/login"
MESSAGES  = "/agent-api/v1/messages"
BROADCAST = "/agent-api/v1/broadcasts"
THREADS   = "/agent-api/v1/threads"
DEVICE    = "9774d56d682e549c"
CUSTOMER  = "FM-2291"

_OK = SendResult(ok=True, meta_message_id="wamid.agent.1", error=None)


def _admin(client, db, username="as_admin"):
    make_user(db, username=username, role_name="super_admin")
    t = login(client, username)
    return {"Authorization": f"Bearer {t['access_token']}"}


def _setup(client, db, code="ASEND", contacts=3, username="ravi.k"):
    """A company with an account, an approved template, a phonebook, and one agent."""
    hdr = _admin(client, db, f"admin_{code.lower()}")
    comp = make_company(db, code=code)
    account = make_wa_account(db, comp.id)
    tpl = make_wa_template(db, comp.id, name=f"promo_{code.lower()}")
    tpl.components = [{"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]

    pb = Phonebook(company_id=comp.id, name=f"List {code}")
    db.add(pb); db.flush()
    for i in range(contacts):
        db.add(PhonebookContact(phonebook_id=pb.id, mobile_no=f"9166660{i:05d}",
                                customer_name=f"Cust {i}", agent_id=f"AG{i}"))
    user = make_user(db, username=username, role_name="agent", company_id=comp.id)
    db.commit()

    r = client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json={
        "agent_id": "AG01", "customer_id": CUSTOMER, "is_active": True,
        "phonebook_ids": [str(pb.id)], "template_ids": [str(tpl.id)],
    })
    assert r.status_code == 200, r.text
    return comp, account, tpl, pb, user, hdr


def _agent_headers(client, username="ravi.k", device_id=DEVICE):
    r = client.post(LOGIN, json={"username": username, "password": "testpass123",
                                 "customer_id": CUSTOMER, "device_id": device_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Device-Id": device_id}


def _open_window(db, company_id, mobile, hours_ago=1, name="Cust 0"):
    """A customer who wrote to us `hours_ago` — the only thing that opens the window."""
    t = agent_timeline.get_or_create_thread(db, company_id, mobile, name)
    t.last_inbound_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    t.last_message_at = t.last_inbound_at
    db.commit()
    return t


def _settings(db, company_id, **kw):
    row = db.query(BroadcastSettings).filter(
        BroadcastSettings.company_id == company_id).first()
    if row is None:
        row = BroadcastSettings(company_id=company_id)
        db.add(row)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()
    return row


# ── 1:1 free text, and the window ─────────────────────────────────────────────

class TestSendMessage:
    def test_sends_inside_the_window(self, client, db):
        comp, *_ = _setup(client, db, "SND1")
        _open_window(db, comp.id, "916666000000")
        h = _agent_headers(client)

        with patch("app.services.wa_sender.send_text", return_value=_OK) as snd:
            r = client.post(MESSAGES, headers=h,
                            json={"mobile_no": "916666000000", "text": "On my way"})

        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "sent" and body["wamid"] == "wamid.agent.1"
        assert snd.call_args.args[2] == "On my way"

    def test_refused_outside_the_window(self, client, db):
        """25 hours is the whole point: Meta would reject this with 131047."""
        comp, *_ = _setup(client, db, "SND2")
        _open_window(db, comp.id, "916666000000", hours_ago=25)
        h = _agent_headers(client)

        with patch("app.services.wa_sender.send_text", return_value=_OK) as snd:
            r = client.post(MESSAGES, headers=h,
                            json={"mobile_no": "916666000000", "text": "too late"})

        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "window_closed"
        assert snd.call_count == 0          # never even attempted

    def test_a_customer_who_never_wrote_has_no_window(self, client, db):
        """
        The rule the design rests on: sending never opens the window. A contact we have
        broadcast to but who never replied is still closed.
        """
        comp, *_ = _setup(client, db, "SND3")
        agent_timeline.record_outbound(db, comp.id, "916666000000", kind="broadcast",
                                       body_rendered="hi", wamid="w1")
        db.commit()
        h = _agent_headers(client)

        r = client.post(MESSAGES, headers=h,
                        json={"mobile_no": "916666000000", "text": "hello?"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "window_closed"

    def test_an_unknown_contact_is_also_window_closed(self, client, db):
        comp, *_ = _setup(client, db, "SND4")
        h = _agent_headers(client)
        r = client.post(MESSAGES, headers=h,
                        json={"mobile_no": "919876543210", "text": "hi"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "window_closed"

    def test_an_opted_out_contact_is_a_hard_block(self, client, db):
        comp, *_ = _setup(client, db, "SND5")
        _open_window(db, comp.id, "916666000000")
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="916666000000",
                               source="manual"))
        db.commit()
        h = _agent_headers(client)

        with patch("app.services.wa_sender.send_text", return_value=_OK) as snd:
            r = client.post(MESSAGES, headers=h,
                            json={"mobile_no": "916666000000", "text": "hi"})

        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "recipient_opted_out"
        assert snd.call_count == 0

    def test_the_message_is_durable_before_the_network_call(self, client, db):
        """
        Meta failing must cost a retry, not the agent's typed text. The row exists
        either way, carrying the error.
        """
        comp, *_ = _setup(client, db, "SND6")
        _open_window(db, comp.id, "916666000000")
        h = _agent_headers(client)
        fail = SendResult(ok=False, meta_message_id=None, error="Meta is down")

        with patch("app.services.wa_sender.send_text", return_value=fail):
            r = client.post(MESSAGES, headers=h,
                            json={"mobile_no": "916666000000", "text": "do not lose me"})

        assert r.status_code == 502
        row = db.query(AgentMessage).filter(
            AgentMessage.body_rendered == "do not lose me").first()
        assert row is not None and row.status == "failed"

    def test_the_sent_message_lands_in_the_thread(self, client, db):
        comp, *_ = _setup(client, db, "SND7")
        t = _open_window(db, comp.id, "916666000000")
        h = _agent_headers(client)

        with patch("app.services.wa_sender.send_text", return_value=_OK):
            client.post(MESSAGES, headers=h,
                        json={"mobile_no": "916666000000", "text": "reply here"})

        rows = db.query(AgentMessage).filter(
            AgentMessage.thread_id == t.id).order_by(AgentMessage.seq).all()
        assert rows[-1].direction == "outbound" and rows[-1].kind == "agent_reply"
        assert rows[-1].sent_by_agent_id is not None

    def test_the_daily_limit_is_enforced(self, client, db):
        comp, *_ = _setup(client, db, "SND8")
        _open_window(db, comp.id, "916666000000")
        _settings(db, comp.id, agent_daily_message_limit=2)
        h = _agent_headers(client)

        with patch("app.services.wa_sender.send_text", return_value=_OK):
            for _ in range(2):
                assert client.post(MESSAGES, headers=h, json={
                    "mobile_no": "916666000000", "text": "x"}).status_code == 201
            r = client.post(MESSAGES, headers=h,
                            json={"mobile_no": "916666000000", "text": "one too many"})

        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "daily_limit_reached"

    def test_zero_daily_limit_means_unlimited(self, client, db):
        comp, *_ = _setup(client, db, "SND9")
        _open_window(db, comp.id, "916666000000")
        _settings(db, comp.id, agent_daily_message_limit=0)
        h = _agent_headers(client)
        with patch("app.services.wa_sender.send_text", return_value=_OK):
            for _ in range(4):
                assert client.post(MESSAGES, headers=h, json={
                    "mobile_no": "916666000000", "text": "x"}).status_code == 201

    def test_no_session_is_refused(self, client, db):
        _setup(client, db, "SND10")
        assert client.post(MESSAGES, json={
            "mobile_no": "916666000000", "text": "x"}).status_code == 401


# ── Agent-initiated broadcast ─────────────────────────────────────────────────

class TestAgentBroadcast:
    def test_creates_an_ordinary_campaign(self, client, db):
        """
        Not a parallel send path: a real broadcast_campaigns row that the web panel
        can see and the normal queue drains.
        """
        comp, _a, tpl, pb, _u, hdr = _setup(client, db, "BC1", contacts=3)
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})

        assert r.status_code == 201, r.text
        body = r.json()
        camp = db.query(BroadcastCampaign).filter(
            BroadcastCampaign.id == body["campaign_id"]).first()
        assert camp is not None
        assert camp.status == "sending"
        assert body["sendable"] == 3

    def test_the_agent_is_recorded_as_the_creator(self, client, db):
        """Which is what makes the per-send cap apply, here and at the panel's /send."""
        comp, _a, tpl, pb, user, _hdr = _setup(client, db, "BC2")
        h = _agent_headers(client)
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})
        camp = db.query(BroadcastCampaign).filter(
            BroadcastCampaign.id == r.json()["campaign_id"]).first()
        assert str(camp.created_by_id) == str(user.id)

    def test_the_recipient_cap_is_enforced(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC3", contacts=5)
        _settings(db, comp.id, agent_max_recipients_per_send=2)
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})

        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "recipient_cap_exceeded"
        # Refused before anything was written — no orphan campaign left behind.
        assert db.query(BroadcastCampaign).filter(
            BroadcastCampaign.company_id == comp.id).count() == 0

    def test_an_unmapped_phonebook_is_not_found(self, client, db):
        """
        Belongs to the right company, but was never given to this agent. A 404, not a
        403 — the agent has no business knowing it exists.
        """
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC4")
        other = Phonebook(company_id=comp.id, name="Not mapped")
        db.add(other); db.commit()
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(other.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})
        assert r.status_code == 404

    def test_an_unmapped_template_is_not_found(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC5")
        other = make_wa_template(db, comp.id, name="unmapped_tpl")
        other.components = [{"type": "BODY", "text": "Hi {{1}}, offer on {{2}}."}]
        db.commit()
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(other.id),
            "params": ["Ravi", "Diwali"]})
        assert r.status_code == 404

    def test_a_stale_param_count_says_so(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC6")
        h = _agent_headers(client)
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["only one"]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "param_count_mismatch"
        assert r.json()["detail"]["expected"] == 2

    def test_opted_out_contacts_are_screened_out(self, client, db):
        """Same screening as the web panel — the whole reason to reuse the pipeline."""
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC7", contacts=3)
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="916666000000",
                               source="manual"))
        db.commit()
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})

        assert r.status_code == 201
        assert r.json()["sendable"] == 2
        assert r.json()["skipped"] == 1

    def test_a_fully_opted_out_list_is_refused(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC8", contacts=2)
        for i in range(2):
            db.add(MessagingOptOut(company_id=comp.id, mobile_no=f"9166660{i:05d}",
                                   source="manual"))
        db.commit()
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "nothing_to_send"

    def test_an_app_render_mismatch_is_logged_not_rejected(self, client, db):
        """
        The customer gets Meta's wording regardless. The discrepancy is a signal the
        app's cached template is stale — worth recording, not worth blocking a send.

        Asserted on the call rather than on an error_logs row: log_error opens its own
        session bound to the application database, not the test one, so a row written
        here is invisible to this fixture.
        """
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC9")
        h = _agent_headers(client)

        with patch("app.agent.broadcasts.log_error") as logged:
            r = client.post(BROADCAST, headers=h, json={
                "phonebook_id": str(pb.id), "template_id": str(tpl.id),
                "params": ["Ravi", "Diwali"],
                "rendered": "Something the app made up entirely"})

        assert r.status_code == 201
        assert logged.call_count == 1
        assert logged.call_args.args[0] == "Agent broadcast render mismatch"

    def test_a_matching_app_render_logs_nothing(self, client, db):
        """The common case must be silent, or the log is noise nobody reads."""
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "BC10")
        h = _agent_headers(client)

        with patch("app.agent.broadcasts.log_error") as logged:
            r = client.post(BROADCAST, headers=h, json={
                "phonebook_id": str(pb.id), "template_id": str(tpl.id),
                "params": ["Ravi", "Diwali"],
                "rendered": "Hi Ravi, offer on Diwali."})

        assert r.status_code == 201
        assert logged.call_count == 0


class TestBroadcastSelection:
    """
    Sending to a subset of a mapped phonebook — the agent ticking rows in a list.

    The property that matters most is not in this class but beside it: every test in
    TestAgentBroadcast sends **without** `contact_ids` and must keep passing unchanged.
    Selection is an add-on, and an app shipping today's payload has to behave exactly as
    it did before it existed.
    """

    def _contacts(self, db, pb):
        rows = db.query(PhonebookContact).filter(
            PhonebookContact.phonebook_id == pb.id
        ).order_by(PhonebookContact.mobile_no).all()
        return [str(c.id) for c in rows]

    def test_sends_to_only_the_selected_contacts(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL1", contacts=5)
        ids = self._contacts(db, pb)[:2]
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": ids})

        assert r.status_code == 201, r.text
        assert r.json()["total"] == 2 and r.json()["sendable"] == 2
        got = {rr.mobile_no for rr in db.query(BroadcastRecipient).filter(
            BroadcastRecipient.campaign_id == r.json()["campaign_id"])}
        assert got == {"916666000000", "916666000001"}

    def test_omitting_the_field_still_sends_to_everyone(self, client, db):
        """The compatibility guarantee, asserted explicitly rather than assumed."""
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL2", contacts=5)
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"]})

        assert r.status_code == 201
        assert r.json()["total"] == 5

    def test_null_is_the_same_as_omitting(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL3", contacts=4)
        h = _agent_headers(client)
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": None})
        assert r.status_code == 201
        assert r.json()["total"] == 4

    def test_an_empty_selection_is_refused_not_read_as_all(self, client, db):
        """
        The expensive mistake this design exists to prevent: an app bug serialising an
        empty array must never become a broadcast to the whole phonebook.
        """
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL4", contacts=5)
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": []})

        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "nothing_selected"
        assert db.query(BroadcastCampaign).filter(
            BroadcastCampaign.company_id == comp.id).count() == 0

    def test_a_stale_contact_id_names_itself(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL5", contacts=3)
        ids = self._contacts(db, pb)[:1] + ["00000000-0000-0000-0000-000000000999"]
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": ids})

        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "unknown_contacts"
        # Named, so the app can resync rather than guess which row went stale.
        assert detail["contact_ids"] == ["00000000-0000-0000-0000-000000000999"]

    def test_nothing_is_sent_when_one_id_is_stale(self, client, db):
        """Refused whole, never partially — the agent must not believe they messaged
        three people when only two went out."""
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL6", contacts=3)
        ids = self._contacts(db, pb) + ["00000000-0000-0000-0000-000000000999"]
        h = _agent_headers(client)

        client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": ids})

        assert db.query(BroadcastRecipient).count() == 0
        assert db.query(BroadcastCampaign).filter(
            BroadcastCampaign.company_id == comp.id).count() == 0

    def test_a_malformed_id_is_a_400_not_a_500(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL7", contacts=2)
        h = _agent_headers(client)
        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": ["not-a-uuid"]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "unknown_contacts"

    def test_a_contact_from_another_phonebook_is_unknown(self, client, db):
        """
        The containment guarantee. An id from a book this agent was never given must
        not resolve just because it is a real contact somewhere.
        """
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL8", contacts=2)
        other = Phonebook(company_id=comp.id, name="Not mapped")
        db.add(other); db.flush()
        stranger = PhonebookContact(phonebook_id=other.id, mobile_no="919999888877",
                                    customer_name="Stranger")
        db.add(stranger); db.commit()
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": [str(stranger.id)]})

        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "unknown_contacts"

    def test_duplicate_ids_count_once(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL9", contacts=3)
        one = self._contacts(db, pb)[:1]
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": one * 3})

        assert r.status_code == 201
        assert r.json()["total"] == 1

    def test_selection_does_not_bypass_opt_out_screening(self, client, db):
        """
        Ticking a row is not consent. Somebody can unsubscribe after the app synced,
        and the server screens whatever set is built.
        """
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL10", contacts=3)
        ids = self._contacts(db, pb)[:2]
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="916666000000",
                               source="manual"))
        db.commit()
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": ids})

        assert r.status_code == 201
        assert r.json()["total"] == 2
        assert r.json()["skipped"] == 1
        assert r.json()["sendable"] == 1

    def test_the_cap_applies_to_the_selection_not_the_book(self, client, db):
        """
        The reason this feature is worth building: a book larger than the cap is
        currently unusable, and selection makes it sendable.
        """
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL11", contacts=5)
        _settings(db, comp.id, agent_max_recipients_per_send=3)
        h = _agent_headers(client)
        body = {"phonebook_id": str(pb.id), "template_id": str(tpl.id),
                "params": ["Ravi", "Diwali"]}

        # The whole book is over the cap — refused, as before.
        assert client.post(BROADCAST, headers=h, json=body).status_code == 403

        # A selection within it is allowed.
        r = client.post(BROADCAST, headers=h,
                        json={**body, "contact_ids": self._contacts(db, pb)[:3]})
        assert r.status_code == 201
        assert r.json()["sendable"] == 3

    def test_a_selection_over_the_cap_is_still_refused(self, client, db):
        comp, _a, tpl, pb, _u, _hdr = _setup(client, db, "SEL12", contacts=5)
        _settings(db, comp.id, agent_max_recipients_per_send=2)
        h = _agent_headers(client)

        r = client.post(BROADCAST, headers=h, json={
            "phonebook_id": str(pb.id), "template_id": str(tpl.id),
            "params": ["Ravi", "Diwali"], "contact_ids": self._contacts(db, pb)[:4]})

        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "recipient_cap_exceeded"
        assert r.json()["detail"]["recipient_count"] == 4


# ── The chat window ───────────────────────────────────────────────────────────

class TestThreads:
    def _thread_with(self, db, comp, mobile, n, inbound_at=None):
        for i in range(n):
            agent_timeline.record_outbound(db, comp.id, mobile, kind="broadcast",
                                           body_rendered=f"m{i}", wamid=f"w-{mobile}-{i}")
        t = agent_timeline.get_or_create_thread(db, comp.id, mobile)
        if inbound_at:
            t.last_inbound_at = inbound_at
        db.commit()
        return t

    def test_the_inbox_lists_only_customers_who_replied(self, client, db):
        """
        A 2,000-recipient campaign creates 2,000 threads. Listing them all would bury
        the handful of people actually waiting.
        """
        comp, *_ = _setup(client, db, "TH1")
        self._thread_with(db, comp, "916666000000", 1)                       # silent
        self._thread_with(db, comp, "916666000001", 1,
                          inbound_at=datetime.now(timezone.utc))             # replied
        h = _agent_headers(client)

        got = client.get(THREADS, headers=h).json()
        assert [t["mobile_no"] for t in got["threads"]] == ["916666000001"]

    def test_silent_threads_can_be_asked_for(self, client, db):
        comp, *_ = _setup(client, db, "TH2")
        self._thread_with(db, comp, "916666000000", 1)
        h = _agent_headers(client)
        got = client.get(f"{THREADS}?include_silent=true", headers=h).json()
        assert len(got["threads"]) == 1

    def test_a_contact_can_be_opened_by_number(self, client, db):
        """Prior broadcasts are visible even though they never replied."""
        comp, *_ = _setup(client, db, "TH3")
        self._thread_with(db, comp, "916666000000", 3)
        h = _agent_headers(client)

        got = client.get(f"{THREADS}/by-mobile/916666000000", headers=h).json()
        assert len(got["messages"]) == 3
        assert got["thread"]["window_state"] == "closed"

    def test_messages_come_back_newest_first(self, client, db):
        comp, *_ = _setup(client, db, "TH4")
        t = self._thread_with(db, comp, "916666000000", 5)
        h = _agent_headers(client)

        got = client.get(f"{THREADS}/{t.id}/messages?limit=3", headers=h).json()
        assert [m["seq"] for m in got["messages"]] == [5, 4, 3]
        assert got["has_more"] is True

    def test_scrolling_up_uses_before_seq(self, client, db):
        comp, *_ = _setup(client, db, "TH5")
        t = self._thread_with(db, comp, "916666000000", 5)
        h = _agent_headers(client)

        got = client.get(f"{THREADS}/{t.id}/messages?limit=3&before_seq=3",
                         headers=h).json()
        assert [m["seq"] for m in got["messages"]] == [2, 1]
        assert got["has_more"] is False

    def test_syncing_uses_after_seq(self, client, db):
        """
        Exact, not approximate: the sequence is gap-free, so "everything after N"
        cannot silently omit a message the way a timestamp cursor can.
        """
        comp, *_ = _setup(client, db, "TH6")
        t = self._thread_with(db, comp, "916666000000", 5)
        h = _agent_headers(client)

        got = client.get(f"{THREADS}/{t.id}/messages?after_seq=3", headers=h).json()
        assert [m["seq"] for m in got["messages"]] == [4, 5]

    def test_paging_never_repeats_or_skips(self, client, db):
        comp, *_ = _setup(client, db, "TH7")
        t = self._thread_with(db, comp, "916666000000", 12)
        h = _agent_headers(client)

        seen, cursor = [], None
        for _ in range(5):
            url = f"{THREADS}/{t.id}/messages?limit=5"
            if cursor:
                url += f"&before_seq={cursor}"
            page = client.get(url, headers=h).json()["messages"]
            if not page:
                break
            seen += [m["seq"] for m in page]
            cursor = page[-1]["seq"]

        assert sorted(seen) == list(range(1, 13))
        assert len(seen) == len(set(seen))

    def test_the_window_is_reported_with_the_thread(self, client, db):
        comp, *_ = _setup(client, db, "TH8")
        t = self._thread_with(db, comp, "916666000000", 1,
                              inbound_at=datetime.now(timezone.utc) - timedelta(hours=1))
        h = _agent_headers(client)

        got = client.get(f"{THREADS}/{t.id}/messages", headers=h).json()
        assert got["thread"]["window_state"] == "open"
        assert got["thread"]["window_expires_at"] is not None
        # For clock skew, not for starting the timer.
        assert got["server_time"] is not None

    def test_opted_out_is_surfaced_on_the_thread(self, client, db):
        comp, *_ = _setup(client, db, "TH9")
        t = self._thread_with(db, comp, "916666000000", 1)
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="916666000000",
                               source="manual"))
        db.commit()
        h = _agent_headers(client)

        got = client.get(f"{THREADS}/{t.id}/messages", headers=h).json()
        assert got["thread"]["opted_out"] is True

    def test_another_companys_thread_is_not_found(self, client, db):
        """A thread id from another tenant must be a 404, never someone's conversation."""
        comp, *_ = _setup(client, db, "TH10")
        other = make_company(db, code="OTHERCO")
        t = agent_timeline.get_or_create_thread(db, other.id, "919999999999")
        db.commit()
        h = _agent_headers(client)

        assert client.get(f"{THREADS}/{t.id}/messages", headers=h).status_code == 404

    def test_no_session_is_refused(self, client, db):
        _setup(client, db, "TH11")
        assert client.get(THREADS).status_code == 401
