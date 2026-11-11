"""
Agent phonebook mapping — /api/agents/by-user/*/phonebooks and /agent-api/v1/phonebooks.

Two groups carry the weight:

  TestOptOut — a contact who texted STOP is a hard block in the broadcast pipeline, but
  nothing on the phonebook side ever surfaced that. Without the flag this endpoint is the
  one place in the system that hands an agent someone who unsubscribed.

  TestContactAgentId — each contact keeps its OWN agent_id, which may name a different
  agent than the caller. That is deliberate, and it is the behaviour most likely to be
  "helpfully" filtered out by a later change.
"""
from app.models.agent import Agent, AgentPhonebook
from app.models.messaging import MessagingOptOut
from app.models.phonebook import Phonebook, PhonebookContact
from tests.conftest import login, make_company, make_user

LOGIN = "/agent-api/v1/auth/login"
AGENT_PB = "/agent-api/v1/phonebooks"
DEVICE = "9774d56d682e549c"
CUSTOMER = "FM-2291"


def _admin(client, db):
    make_user(db, username="pb_admin", role_name="super_admin")
    t = login(client, "pb_admin")
    return {"Authorization": f"Bearer {t['access_token']}"}


def _book(db, company_id, name, contacts=()):
    """contacts: (mobile, name, agent_id) tuples."""
    pb = Phonebook(company_id=company_id, name=name)
    db.add(pb); db.flush()
    for mobile, cname, agent_id in contacts:
        db.add(PhonebookContact(phonebook_id=pb.id, mobile_no=mobile,
                                customer_name=cname, agent_id=agent_id,
                                email=f"{cname.lower()}@example.com"))
    db.commit(); db.refresh(pb)
    return pb


def _agent_user(db, company, username="ravi.k"):
    return make_user(db, username=username, role_name="agent", company_id=company.id)


def _configure(client, hdr, user_id, phonebook_ids=None, agent_id="AG01"):
    body = {"agent_id": agent_id, "customer_id": CUSTOMER, "is_active": True}
    if phonebook_ids is not None:
        body["phonebook_ids"] = [str(p) for p in phonebook_ids]
    return client.put(f"/api/agents/by-user/{user_id}", headers=hdr, json=body)


def _agent_headers(client, username="ravi.k", device_id=DEVICE):
    r = client.post(LOGIN, json={"username": username, "password": "testpass123",
                                 "customer_id": CUSTOMER, "device_id": device_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Device-Id": device_id}


# ── Admin mapping ─────────────────────────────────────────────────────────────

class TestMapping:
    def test_maps_selected_phonebooks(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBM1")
        user = _agent_user(db, comp)
        a = _book(db, comp.id, "Kerala retail", [("919999000001", "Ravi", "AG01")])
        b = _book(db, comp.id, "Kochi VIP")

        assert _configure(client, hdr, user.id, [a.id, b.id]).status_code == 200
        got = client.get(f"/api/agents/by-user/{user.id}/phonebooks", headers=hdr).json()
        assert {p["name"] for p in got} == {"Kerala retail", "Kochi VIP"}
        assert {p["name"]: p["contact_count"] for p in got}["Kerala retail"] == 1

    def test_saving_replaces_the_whole_set(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBM2")
        user = _agent_user(db, comp)
        a, b = _book(db, comp.id, "aaa"), _book(db, comp.id, "bbb")

        _configure(client, hdr, user.id, [a.id, b.id])
        _configure(client, hdr, user.id, [b.id])
        names = {p["name"] for p in
                 client.get(f"/api/agents/by-user/{user.id}/phonebooks", headers=hdr).json()}
        assert names == {"bbb"}

    def test_omitting_phonebook_ids_leaves_the_mapping_alone(self, client, db):
        """A failed load must never be able to wipe a mapping."""
        hdr = _admin(client, db)
        comp = make_company(db, code="PBM3")
        user = _agent_user(db, comp)
        a = _book(db, comp.id, "keepme")
        _configure(client, hdr, user.id, [a.id])
        _configure(client, hdr, user.id, None)
        assert len(client.get(f"/api/agents/by-user/{user.id}/phonebooks",
                              headers=hdr).json()) == 1

    def test_empty_list_clears_the_mapping(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBM4")
        user = _agent_user(db, comp)
        _configure(client, hdr, user.id, [_book(db, comp.id, "dropme").id])
        _configure(client, hdr, user.id, [])
        assert client.get(f"/api/agents/by-user/{user.id}/phonebooks", headers=hdr).json() == []

    def test_another_companys_phonebook_is_refused(self, client, db):
        hdr = _admin(client, db)
        mine, theirs = make_company(db, code="PBMINE"), make_company(db, code="PBTHEIRS")
        user = _agent_user(db, mine)
        foreign = _book(db, theirs.id, "not_yours")

        r = _configure(client, hdr, user.id, [foreign.id])
        assert r.status_code == 400
        assert "another company" in r.json()["detail"]

    def test_repeated_saves_do_not_violate_the_unique_constraint(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBM5")
        user = _agent_user(db, comp)
        a = _book(db, comp.id, "steady")
        for _ in range(3):
            assert _configure(client, hdr, user.id, [a.id]).status_code == 200
        assert db.query(AgentPhonebook).count() == 1

    def test_templates_and_phonebooks_save_independently(self, client, db):
        """Both mappings ride one PUT; neither may clobber the other."""
        hdr = _admin(client, db)
        comp = make_company(db, code="PBM6")
        user = _agent_user(db, comp)
        pb = _book(db, comp.id, "book")

        _configure(client, hdr, user.id, [pb.id])
        # A save that mentions templates only must leave phonebooks untouched.
        client.put(f"/api/agents/by-user/{user.id}", headers=hdr, json={
            "agent_id": "AG01", "customer_id": CUSTOMER, "is_active": True,
            "template_ids": []})
        assert len(client.get(f"/api/agents/by-user/{user.id}/phonebooks",
                              headers=hdr).json()) == 1


class TestAvailablePhonebooks:
    def test_lists_only_this_users_company(self, client, db):
        hdr = _admin(client, db)
        mine, theirs = make_company(db, code="PBA1"), make_company(db, code="PBA2")
        user = _agent_user(db, mine)
        _book(db, mine.id, "ours")
        _book(db, theirs.id, "theirs")

        names = {p["name"] for p in client.get(
            f"/api/agents/by-user/{user.id}/available-phonebooks", headers=hdr).json()}
        assert names == {"ours"}

    def test_user_with_no_company_gets_nothing(self, client, db):
        hdr = _admin(client, db)
        u = make_user(db, username="nocompany", role_name="agent")
        assert client.get(f"/api/agents/by-user/{u.id}/available-phonebooks",
                          headers=hdr).json() == []


# ── The agent-facing endpoint ─────────────────────────────────────────────────

class TestAgentEndpoint:
    def _ready(self, client, db, code="PBE1", books=None):
        hdr = _admin(client, db)
        comp = make_company(db, code=code)
        user = _agent_user(db, comp)
        made = [_book(db, comp.id, n, cs) for n, cs in (books or [])]
        _configure(client, hdr, user.id, [b.id for b in made])
        return hdr, comp, user, made

    def test_returns_books_with_nested_contacts(self, client, db):
        self._ready(client, db, "PBE1", [
            ("Kerala retail", [("919999000001", "Ravi", "AG01"),
                               ("919999000002", "Anil", "AG02")]),
        ])
        r = client.get(AGENT_PB, headers=_agent_headers(client))
        assert r.status_code == 200
        book = r.json()[0]
        assert book["name"] == "Kerala retail"
        assert book["contact_count"] == 2
        assert [c["customer_name"] for c in book["contacts"]] == ["Anil", "Ravi"]
        assert book["contacts"][0]["email"] == "anil@example.com"

    def test_unmapped_agent_gets_an_empty_list(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBE2")
        user = _agent_user(db, comp)
        _book(db, comp.id, "exists_but_unmapped", [("919999000009", "X", None)])
        _configure(client, hdr, user.id)          # no phonebook_ids

        assert client.get(AGENT_PB, headers=_agent_headers(client)).json() == []

    def test_empty_phonebook_returns_an_empty_contacts_array(self, client, db):
        """Not omitted — the agent should see the book exists and is empty."""
        self._ready(client, db, "PBE3", [("Empty book", [])])
        book = client.get(AGENT_PB, headers=_agent_headers(client)).json()[0]
        assert book["contacts"] == []
        assert book["contact_count"] == 0

    def test_multiple_books_keep_their_own_contacts(self, client, db):
        self._ready(client, db, "PBE4", [
            ("AAA", [("919999000001", "One", None)]),
            ("BBB", [("919999000002", "Two", None), ("919999000003", "Three", None)]),
        ])
        got = {b["name"]: b for b in
               client.get(AGENT_PB, headers=_agent_headers(client)).json()}
        assert got["AAA"]["contact_count"] == 1
        assert got["BBB"]["contact_count"] == 2
        assert {c["mobile_no"] for c in got["AAA"]["contacts"]} == {"919999000001"}

    def test_requires_a_session(self, client, db):
        assert client.get(AGENT_PB).status_code == 401

    def test_requires_the_device_header(self, client, db):
        self._ready(client, db, "PBE5", [("Book", [])])
        tok = _agent_headers(client)["Authorization"]
        assert client.get(AGENT_PB, headers={"Authorization": tok}).status_code == 401


# ── Each contact keeps its own agent ──────────────────────────────────────────

class TestContactAgentId:
    def test_contacts_of_other_agents_are_still_returned(self, client, db):
        """
        The caller is AG01; the book holds contacts for AG02, AG03 and none at all.
        All are returned, each carrying its own value. Filtering to the caller's own
        agent_id would be a different feature, and would silently hide most of the book.
        """
        hdr = _admin(client, db)
        comp = make_company(db, code="PBAG")
        user = _agent_user(db, comp)
        pb = _book(db, comp.id, "Mixed", [
            ("919999000001", "Alpha", "AG01"),
            ("919999000002", "Bravo", "AG02"),
            ("919999000003", "Chi",   "AG03"),
            ("919999000004", "Delta", None),
        ])
        _configure(client, hdr, user.id, [pb.id], agent_id="AG01")

        contacts = client.get(AGENT_PB, headers=_agent_headers(client)).json()[0]["contacts"]
        assert len(contacts) == 4
        assert {c["customer_name"]: c["agent_id"] for c in contacts} == {
            "Alpha": "AG01", "Bravo": "AG02", "Chi": "AG03", "Delta": None}


# ── Opt-out ───────────────────────────────────────────────────────────────────

class TestOptOut:
    def test_opted_out_contact_is_flagged(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBOPT")
        user = _agent_user(db, comp)
        pb = _book(db, comp.id, "List", [
            ("919999000001", "Ravi", "AG01"),
            ("919999000002", "Anil", "AG01"),
        ])
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="919999000002",
                               source="stop_keyword"))
        db.commit()
        _configure(client, hdr, user.id, [pb.id])

        contacts = client.get(AGENT_PB, headers=_agent_headers(client)).json()[0]["contacts"]
        flags = {c["customer_name"]: c["opted_out"] for c in contacts}
        assert flags == {"Anil": True, "Ravi": False}

    def test_opted_out_contacts_are_shown_not_omitted(self, client, db):
        """The agent must see the person exists and why they cannot be messaged."""
        hdr = _admin(client, db)
        comp = make_company(db, code="PBOPT2")
        user = _agent_user(db, comp)
        pb = _book(db, comp.id, "List", [("919999000001", "Ravi", None)])
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="919999000001",
                               source="manual"))
        db.commit()
        _configure(client, hdr, user.id, [pb.id])

        book = client.get(AGENT_PB, headers=_agent_headers(client)).json()[0]
        assert book["contact_count"] == 1
        assert book["contacts"][0]["opted_out"] is True

    def test_opt_out_is_company_scoped(self, client, db):
        """
        Unsubscribing from one business must not unsubscribe you from another —
        BROADCAST_DESIGN.md §9. A join that forgot company_id would fail only here.
        """
        hdr = _admin(client, db)
        mine  = make_company(db, code="PBSCOPE1")
        other = make_company(db, code="PBSCOPE2")
        user = _agent_user(db, mine)
        pb = _book(db, mine.id, "List", [("919999000001", "Ravi", None)])
        # Opted out of the OTHER company only.
        db.add(MessagingOptOut(company_id=other.id, mobile_no="919999000001",
                               source="stop_keyword"))
        db.commit()
        _configure(client, hdr, user.id, [pb.id])

        book = client.get(AGENT_PB, headers=_agent_headers(client)).json()[0]
        assert book["contacts"][0]["opted_out"] is False

    def test_flag_is_live_not_stored(self, client, db):
        """Opting out after the mapping was made still shows on the next call."""
        hdr = _admin(client, db)
        comp = make_company(db, code="PBLIVE")
        user = _agent_user(db, comp)
        pb = _book(db, comp.id, "List", [("919999000001", "Ravi", None)])
        _configure(client, hdr, user.id, [pb.id])
        headers = _agent_headers(client)

        before = client.get(AGENT_PB, headers=headers).json()[0]["contacts"][0]
        assert before["opted_out"] is False

        db.add(MessagingOptOut(company_id=comp.id, mobile_no="919999000001",
                               source="stop_keyword"))
        db.commit()

        after = client.get(AGENT_PB, headers=headers).json()[0]["contacts"][0]
        assert after["opted_out"] is True


# ── Isolation and shape ───────────────────────────────────────────────────────

class TestIsolation:
    def test_another_companys_book_is_never_returned(self, client, db):
        """Even with a mapping row forced in — the company filter is the backstop."""
        hdr = _admin(client, db)
        mine, theirs = make_company(db, code="PBI1"), make_company(db, code="PBI2")
        user = _agent_user(db, mine)
        mine_book = _book(db, mine.id, "Ours", [("919999000001", "Ravi", None)])
        their_book = _book(db, theirs.id, "Theirs", [("919999000002", "Other", None)])
        _configure(client, hdr, user.id, [mine_book.id])

        agent = db.query(Agent).filter(Agent.user_id == user.id).first()
        db.add(AgentPhonebook(agent_id=agent.id, phonebook_id=their_book.id))
        db.commit()

        names = {b["name"] for b in client.get(AGENT_PB, headers=_agent_headers(client)).json()}
        assert names == {"Ours"}

    def test_deleting_a_phonebook_removes_the_mapping(self, client, db):
        hdr = _admin(client, db)
        comp = make_company(db, code="PBI3")
        user = _agent_user(db, comp)
        pb = _book(db, comp.id, "doomed", [("919999000001", "Ravi", None)])
        _configure(client, hdr, user.id, [pb.id])
        assert db.query(AgentPhonebook).count() == 1

        db.delete(pb)
        db.commit()
        assert db.query(AgentPhonebook).count() == 0
        assert client.get(AGENT_PB, headers=_agent_headers(client)).json() == []

    def test_query_count_does_not_grow_with_phonebooks(self, client, db):
        """
        Two queries regardless of how many books. Fetching contacts per phonebook would
        be N+1 on the endpoint most likely to grow, and nothing else would catch it.
        """
        hdr = _admin(client, db)
        comp = make_company(db, code="PBN1")
        user = _agent_user(db, comp)
        books = [_book(db, comp.id, f"Book {i}",
                       [(f"91999900{i}00{j}", f"C{i}{j}", None) for j in range(3)])
                 for i in range(3)]
        _configure(client, hdr, user.id, [b.id for b in books])
        headers = _agent_headers(client)

        from sqlalchemy import event
        seen = []
        engine = db.get_bind()

        def count(conn, cursor, statement, *a):
            if "phonebook" in statement.lower():
                seen.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            r = client.get(AGENT_PB, headers=headers)
        finally:
            event.remove(engine, "before_cursor_execute", count)

        assert r.status_code == 200
        assert len(r.json()) == 3
        # One for the books, one for all contacts. Anything more is per-book work.
        assert len(seen) <= 2, f"expected <=2 phonebook queries, saw {len(seen)}"


class TestAccessControl:
    def test_mapping_requires_authentication(self, client, db):
        comp = make_company(db, code="PBAC1")
        user = _agent_user(db, comp)
        assert client.get(f"/api/agents/by-user/{user.id}/phonebooks").status_code == 403

    def test_company_scoped_admin_cannot_reach_another_company(self, client, db):
        theirs, mine = make_company(db, code="PBACT"), make_company(db, code="PBACM")
        target = _agent_user(db, theirs, username="their.agent")
        make_user(db, username="scoped_pb_admin", role_name="admin", company_id=mine.id)
        tok = login(client, "scoped_pb_admin")["access_token"]

        r = client.get(f"/api/agents/by-user/{target.id}/phonebooks",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403


# ── Phonebooks an agent builds from the app ──────────────────────────────────

class TestAgentBuiltPhonebooks:
    """POST/PATCH/DELETE under /agent-api/v1/phonebooks: an agent creates books in
    their own company and may change only the ones they created."""

    def _agent(self, client, db, code="PBW1", username="ravi.k", books=()):
        hdr = _admin(client, db)
        comp = make_company(db, code=code)
        user = _agent_user(db, comp, username=username)
        made = [_book(db, comp.id, n, cs) for n, cs in books]
        _configure(client, hdr, user.id, [b.id for b in made])
        return comp, user, made, _agent_headers(client, username=username)

    def _create(self, client, h, name="My shop", contacts=()):
        return client.post(AGENT_PB, headers=h, json={
            "name": name,
            "contacts": [{"mobile_no": m, "customer_name": n} for m, n in contacts],
        })

    def test_create_puts_the_book_in_the_agents_company_and_maps_it(self, client, db):
        comp, user, _, h = self._agent(client, db)
        r = self._create(client, h, contacts=[("919999000001", "Ravi"), ("+919999000002", "Anil")])
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["editable"] is True and body["contact_count"] == 2 and body["skipped"] == []

        pb = db.query(Phonebook).filter(Phonebook.name == "My shop").one()
        assert pb.company_id == comp.id and pb.created_by_id == user.id
        assert db.query(AgentPhonebook).filter(AgentPhonebook.phonebook_id == pb.id).count() == 1
        # Stored the way every other number is: '+' stripped, contact tagged with the agent.
        stored = {c.mobile_no: c.agent_id for c in
                  db.query(PhonebookContact).filter(PhonebookContact.phonebook_id == pb.id)}
        assert stored == {"919999000001": "AG01", "919999000002": "AG01"}

        listed = client.get(AGENT_PB, headers=h).json()
        assert [b["name"] for b in listed] == ["My shop"]

    def test_company_id_in_the_body_is_ignored(self, client, db):
        comp, _, _, h = self._agent(client, db, "PBW2")
        other = make_company(db, name="Other Co", code="PBW2X")
        r = client.post(AGENT_PB, headers=h, json={"name": "Sneaky", "company_id": str(other.id)})
        assert r.status_code == 201
        assert db.query(Phonebook).filter(Phonebook.name == "Sneaky").one().company_id == comp.id

    def test_duplicate_name_in_the_company_is_refused(self, client, db):
        comp, _, _, h = self._agent(client, db, "PBW3", books=[("Taken", [])])
        r = self._create(client, h, name="Taken")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "phonebook_name_taken"

    def test_bad_and_duplicate_rows_are_skipped_and_reported(self, client, db):
        _, _, _, h = self._agent(client, db, "PBW4")
        r = self._create(client, h, contacts=[
            ("919999000001", "Ravi"), ("12", "Too short"), ("919999000001", "Again"), ("919999000003", ""),
        ])
        assert r.status_code == 201
        body = r.json()
        assert body["contact_count"] == 1
        assert [(s["row"], s["reason"] != "") for s in body["skipped"]] == [(2, True), (3, True), (4, True)]
        assert body["skipped"][1]["reason"] == "Already in this phonebook"

    def test_add_edit_and_remove_contacts_on_own_book(self, client, db):
        _, _, _, h = self._agent(client, db, "PBW5")
        pid = self._create(client, h).json()["id"]

        r = client.post(f"{AGENT_PB}/{pid}/contacts", headers=h,
                        json={"contacts": [{"mobile_no": "919999000001", "customer_name": "Ravi"},
                                           {"mobile_no": "919999000002", "customer_name": "Anil"}]})
        assert r.status_code == 201 and r.json() == {"added": 2, "skipped": []}

        cid = next(c["id"] for c in client.get(AGENT_PB, headers=h).json()[0]["contacts"]
                   if c["customer_name"] == "Ravi")
        r = client.patch(f"{AGENT_PB}/{pid}/contacts/{cid}", headers=h,
                         json={"customer_name": "Ravi Kumar", "email": "ravi@example.com"})
        assert r.status_code == 200
        assert r.json()["customer_name"] == "Ravi Kumar" and r.json()["email"] == "ravi@example.com"

        clash = client.patch(f"{AGENT_PB}/{pid}/contacts/{cid}", headers=h, json={"mobile_no": "919999000002"})
        assert clash.status_code == 409 and clash.json()["detail"]["code"] == "contact_exists"

        bad = client.patch(f"{AGENT_PB}/{pid}/contacts/{cid}", headers=h, json={"mobile_no": "12"})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_contact"

        assert client.delete(f"{AGENT_PB}/{pid}/contacts/{cid}", headers=h).status_code == 204
        names = [c["customer_name"] for c in client.get(AGENT_PB, headers=h).json()[0]["contacts"]]
        assert names == ["Anil"]

    def test_rename_own_book(self, client, db):
        _, _, _, h = self._agent(client, db, "PBW6")
        pid = self._create(client, h).json()["id"]
        r = client.patch(f"{AGENT_PB}/{pid}", headers=h, json={"name": "  Renamed  "})
        assert r.status_code == 200 and r.json()["name"] == "Renamed"

    def test_books_cannot_be_deleted_from_the_app(self, client, db):
        _, _, _, h = self._agent(client, db, "PBW7")
        pid = self._create(client, h).json()["id"]
        assert client.delete(f"{AGENT_PB}/{pid}", headers=h).status_code == 405
        assert db.query(Phonebook).count() == 1

    def test_admin_made_book_is_read_only(self, client, db):
        _, _, made, h = self._agent(client, db, "PBW8",
                                    books=[("Admin list", [("919999000001", "Ravi", "AG01")])])
        pid = made[0].id
        listed = client.get(AGENT_PB, headers=h).json()[0]
        assert listed["editable"] is False
        cid = listed["contacts"][0]["id"]
        for r in (
            client.patch(f"{AGENT_PB}/{pid}", headers=h, json={"name": "Mine now"}),
            client.post(f"{AGENT_PB}/{pid}/contacts", headers=h,
                        json={"contacts": [{"mobile_no": "919999000002", "customer_name": "X"}]}),
            client.patch(f"{AGENT_PB}/{pid}/contacts/{cid}", headers=h, json={"customer_name": "X"}),
            client.delete(f"{AGENT_PB}/{pid}/contacts/{cid}", headers=h),
        ):
            assert r.status_code == 403 and r.json()["detail"]["code"] == "phonebook_read_only"

    def test_another_agents_book_is_not_found(self, client, db):
        _, _, _, h1 = self._agent(client, db, "PBW9")
        pid = self._create(client, h1).json()["id"]
        # A second agent in another company, on another device.
        comp2 = make_company(db, name="Second Co", code="PBW9B")
        u2 = _agent_user(db, comp2, username="anil.s")
        admin = {"Authorization": f"Bearer {login(client, 'pb_admin')['access_token']}"}
        _configure(client, admin, u2.id, [], agent_id="AG02")
        h2 = _agent_headers(client, username="anil.s", device_id="second-device-01")

        r = client.patch(f"{AGENT_PB}/{pid}", headers=h2, json={"name": "Stolen"})
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
        unknown = "00000000-0000-0000-0000-000000000999"
        assert client.post(f"{AGENT_PB}/{unknown}/contacts", headers=h1,
                           json={"contacts": [{"mobile_no": "919999000001", "customer_name": "R"}]}
                           ).status_code == 404

    def test_more_than_500_rows_in_one_call_is_refused(self, client, db):
        _, _, _, h = self._agent(client, db, "PBW10")
        rows = [{"mobile_no": f"9199990{i:05d}", "customer_name": "C"} for i in range(501)]
        assert client.post(AGENT_PB, headers=h, json={"name": "Big", "contacts": rows}).status_code == 422

    def test_a_book_is_capped(self, client, db, monkeypatch):
        from app.agent import phonebooks as agent_pb
        monkeypatch.setattr(agent_pb, "MAX_CONTACTS_PER_BOOK", 3)
        _, _, _, h = self._agent(client, db, "PBW11")
        pid = self._create(client, h, contacts=[("919999000001", "A"), ("919999000002", "B")]).json()["id"]
        r = client.post(f"{AGENT_PB}/{pid}/contacts", headers=h,
                        json={"contacts": [{"mobile_no": "919999000003", "customer_name": "C"},
                                           {"mobile_no": "919999000004", "customer_name": "D"}]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "phonebook_full"
        assert db.query(PhonebookContact).count() == 2

    def test_opted_out_number_added_by_an_agent_is_still_flagged(self, client, db):
        comp, _, _, h = self._agent(client, db, "PBW12")
        db.add(MessagingOptOut(company_id=comp.id, mobile_no="919999000002", source="stop_keyword"))
        db.commit()
        self._create(client, h, contacts=[("919999000001", "Ravi"), ("919999000002", "Anil")])
        flags = {c["customer_name"]: c["opted_out"] for c in client.get(AGENT_PB, headers=h).json()[0]["contacts"]}
        assert flags == {"Anil": True, "Ravi": False}

    def test_agent_without_a_company_cannot_create(self, client, db):
        hdr = _admin(client, db)
        user = make_user(db, username="nocomp", role_name="agent")
        _configure(client, hdr, user.id, [])
        h = _agent_headers(client, username="nocomp", device_id="nocomp-device")
        r = self._create(client, h)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "no_company"
