"""
Phase 1b — one canonical number per customer, and BSUID identity.

The failure this prevents: a phonebook row typed as 8075571893 and Meta's reply from
918075571893 were two strings, so the broadcast and the customer's answer went to two
different threads and the reply never reached the agent who broadcast.
"""
import pytest
from sqlalchemy import text

from app.core import phone
from app.models.agent_timeline import AgentMessage, AgentThread
from app.models.broadcast_campaign import BroadcastCampaign, BroadcastRecipient
from app.models.broadcast_settings import BroadcastSettings
from app.models.phonebook import Phonebook, PhonebookContact
from app.services import agent_timeline, broadcast_builder
from scripts import normalize_numbers
from tests.conftest import make_company, make_wa_template


# ── normalize() ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,region,expected", [
    ("8075571893",       "IN", "918075571893"),
    ("918075571893",     "IN", "918075571893"),
    ("+91 80755 71893",  "IN", "918075571893"),
    ("080755-71893",     "IN", "918075571893"),
    ("9123456789",       "IN", "919123456789"),   # national number that starts with 91
    ("971501234567",     "IN", "971501234567"),   # foreign number in an Indian company
    ("00971501234567",   "IN", "971501234567"),
    ("0501234567",       "AE", "971501234567"),
    ("545001529",        "AE", "971545001529"),
])
def test_normalize(raw, region, expected):
    assert phone.normalize(raw, region) == expected


@pytest.mark.parametrize("raw", ["", "12", "abc", None, "+999 123"])
def test_normalize_rejects(raw):
    with pytest.raises(phone.InvalidPhone):
        phone.normalize(raw, "IN")


def test_excel_scientific_notation_gets_a_specific_message():
    with pytest.raises(phone.InvalidPhone, match="Excel"):
        phone.normalize("9.18076E+11", "IN")


# ── Broadcast recipients are canonical ───────────────────────────────────────

def _book(db, comp, numbers):
    pb = Phonebook(company_id=comp.id, name="B")
    db.add(pb)
    db.flush()
    for n in numbers:
        db.add(PhonebookContact(phonebook_id=pb.id, mobile_no=n, customer_name="C"))
    db.commit()
    return pb


def _build(db, comp, pb):
    tpl = make_wa_template(db, comp.id)
    camp = BroadcastCampaign(company_id=comp.id, name="c", template_id=tpl.id, status="draft")
    db.add(camp)
    db.flush()
    broadcast_builder.build_from_phonebooks(db, camp, tpl, phonebook_ids=[pb.id], params=[])
    db.flush()
    return sorted(r.mobile_no for r in
                  db.query(BroadcastRecipient).filter_by(campaign_id=camp.id))


def test_builder_canonicalises_and_dedups_spellings(db):
    comp = make_company(db, code="PH1")
    pb = _book(db, comp, ["8075571893", "+91 80755 71893", "918075571893", "7025985366"])
    assert _build(db, comp, pb) == ["917025985366", "918075571893"]


def test_builder_uses_company_default_country(db):
    comp = make_company(db, code="PH2")
    db.add(BroadcastSettings(company_id=comp.id, default_country="AE"))
    db.commit()
    pb = _book(db, comp, ["0501234567"])
    assert _build(db, comp, pb) == ["971501234567"]


def test_builder_keeps_numbers_the_library_cannot_read(db):
    """Sendable before canonical form existed → still sendable (old 7–15 digit rule)."""
    comp = make_company(db, code="PH3")
    pb = _book(db, comp, ["1234567"])
    assert _build(db, comp, pb) == ["1234567"]


# ── One thread for the broadcast and the reply ───────────────────────────────

def test_broadcast_and_reply_share_a_thread(db):
    comp = make_company(db, code="PH4")
    pb = _book(db, comp, ["8075571893"])
    [mobile] = _build(db, comp, pb)
    agent_timeline.record_outbound(db, comp.id, mobile, kind="broadcast",
                                   body_rendered="Hi", wamid="wamid.B1")
    agent_timeline.record_inbound(db, comp.id, "918075571893", body_rendered="Yes please",
                                  wamid="wamid.R1")
    db.commit()
    threads = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert len(threads) == 1
    assert threads[0].message_seq == 2


# ── BSUID ────────────────────────────────────────────────────────────────────

def test_username_only_customer_gets_a_thread(db):
    comp = make_company(db, code="PH5")
    agent_timeline.record_inbound(db, comp.id, None, bsuid="IN.123", body_rendered="hi",
                                  wamid="wamid.U1")
    db.commit()
    [t] = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert t.mobile_no is None and t.bsuid == "IN.123"


def test_bsuid_matches_first_and_number_is_filled_in(db):
    comp = make_company(db, code="PH6")
    agent_timeline.record_inbound(db, comp.id, None, bsuid="IN.777", body_rendered="a",
                                  wamid="wamid.U2")
    agent_timeline.record_inbound(db, comp.id, "918075571893", bsuid="IN.777",
                                  body_rendered="b", wamid="wamid.U3")
    db.commit()
    [t] = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert t.mobile_no == "918075571893"
    assert t.message_seq == 2


def test_number_thread_learns_its_bsuid(db):
    comp = make_company(db, code="PH7")
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast",
                                   wamid="wamid.B2")
    agent_timeline.record_inbound(db, comp.id, "918075571893", bsuid="IN.555",
                                  body_rendered="x", wamid="wamid.U4")
    db.commit()
    [t] = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert t.bsuid == "IN.555"


def test_two_username_only_customers_do_not_collide(db):
    comp = make_company(db, code="PH8")
    agent_timeline.record_inbound(db, comp.id, None, bsuid="IN.1", wamid="wamid.a")
    agent_timeline.record_inbound(db, comp.id, None, bsuid="IN.2", wamid="wamid.b")
    db.commit()
    assert db.query(AgentThread).filter_by(company_id=comp.id).count() == 2


def test_changed_number_relinks_the_thread(db):
    comp = make_company(db, code="PH9")
    agent_timeline.record_inbound(db, comp.id, "918075571893", bsuid="IN.9", wamid="wamid.c")
    assert agent_timeline.relink_identity(db, comp.id, old_mobile="918075571893",
                                          new_mobile="919999999999")
    db.commit()
    [t] = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert t.mobile_no == "919999999999"


def test_system_message_is_not_recorded_and_does_not_open_window(db):
    from app.services import conversation_engine
    comp = make_company(db, code="PH10")
    agent_timeline.record_outbound(db, comp.id, "918075571893", kind="broadcast",
                                   wamid="wamid.B9")
    db.commit()
    conversation_engine._relink_from_system(
        db, comp.id,
        {"type": "system", "system": {"type": "user_changed_number",
                                      "new_wa_id": "919000000001"}},
        "918075571893", None)
    db.commit()
    [t] = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert t.mobile_no == "919000000001"
    assert t.last_inbound_at is None


# ── Backfill script ──────────────────────────────────────────────────────────

def _thread_with(db, comp, mobile, bodies, inbound=False):
    for b in bodies:
        if inbound:
            agent_timeline.record_inbound(db, comp.id, mobile, body_rendered=b)
        else:
            agent_timeline.record_outbound(db, comp.id, mobile, kind="broadcast",
                                           body_rendered=b)
    db.commit()


def test_backfill_dry_run_writes_nothing(db):
    comp = make_company(db, code="BF1")
    _book(db, comp, ["8075571893"])
    _thread_with(db, comp, "8075571893", ["sent"])
    _thread_with(db, comp, "918075571893", ["reply"], inbound=True)

    report = normalize_numbers.run(db, apply=False)
    assert report["contacts"]["changes"][0]["new"] == "918075571893"
    assert [a["type"] for a in report["threads"]["actions"]] == ["merge"]
    db.expire_all()
    assert db.query(AgentThread).filter_by(company_id=comp.id).count() == 2
    assert db.query(PhonebookContact).first().mobile_no == "8075571893"


def test_backfill_apply_merges_split_threads_in_time_order(db):
    comp = make_company(db, code="BF2")
    _thread_with(db, comp, "8075571893", ["first", "second"])
    _thread_with(db, comp, "918075571893", ["third"], inbound=True)

    normalize_numbers.run(db, apply=True)
    db.expire_all()
    [t] = db.query(AgentThread).filter_by(company_id=comp.id).all()
    assert t.mobile_no == "918075571893"
    assert t.message_seq == 3 and t.rev == 3
    assert t.last_inbound_at is not None
    msgs = (db.query(AgentMessage).filter_by(thread_id=t.id)
            .order_by(AgentMessage.seq).all())
    assert [m.body_rendered for m in msgs] == ["first", "second", "third"]
    assert [m.seq for m in msgs] == [1, 2, 3]
    assert [m.rev for m in msgs] == [1, 2, 3]


def test_backfill_leaves_a_duplicate_inside_one_phonebook_alone(db):
    comp = make_company(db, code="BF3")
    _book(db, comp, ["8075571893", "918075571893"])
    report = normalize_numbers.run(db, apply=True)
    assert report["contacts"]["changes"] == []
    assert "already in this phonebook" in report["contacts"]["problems"][0]["reason"]
    db.expire_all()
    assert sorted(c.mobile_no for c in db.query(PhonebookContact).all()) == \
        ["8075571893", "918075571893"]
