"""
/api/phonebooks — CRUD, validation, and company isolation.

The isolation tests matter most: a phonebook is company-owned, and a
company-scoped user must never read, edit or delete another company's contact
lists. That is the same class of bug as the one where a user could once see
every company's data.
"""
import pytest

from app.core.column_mapper import suggest_mapping
from app.core.pagination import Page, _build_window
from app.models.phonebook import Phonebook, PhonebookContact

from .conftest import login, make_company, make_role, make_user


def _auth(client, username, password="testpass123"):
    tokens = login(client, username, password)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def admin_hdr(client, db):
    """Admin-tier user: company_id is None, so sees every company."""
    make_user(db, username="bl_admin", role_name="super_admin")
    return _auth(client, "bl_admin")


def _make_scoped_user(db, client, company_id, username):
    """A user pinned to one company, with full phonebook permissions."""
    role = make_role(db, name=f"bl_role_{username}", display_name="BL Role")
    db.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO role_page_permission "
            "(role_id, page_name, can_read, can_create, can_write, can_delete) "
            "VALUES (:r, 'phonebooks', true, true, true, true)"
        ),
        {"r": str(role.id)},
    )
    db.commit()
    u = make_user(db, username=username, company_id=company_id)
    u.role_id = role.id
    db.commit()
    return _auth(client, username)


class TestPhonebookCrud:
    def test_create_and_fetch(self, client, db, admin_hdr):
        comp = make_company(db, code="PB1")
        r = client.post("/api/phonebooks/", json={"name": "VIP", "company_id": str(comp.id)},
                        headers=admin_hdr)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "VIP"
        assert body["contact_count"] == 0

        r2 = client.get(f"/api/phonebooks/{body['id']}", headers=admin_hdr)
        assert r2.status_code == 200
        assert r2.json()["name"] == "VIP"

    def test_duplicate_name_in_same_company_rejected(self, client, db, admin_hdr):
        comp = make_company(db, code="PB2")
        payload = {"name": "Diwali", "company_id": str(comp.id)}
        assert client.post("/api/phonebooks/", json=payload, headers=admin_hdr).status_code == 201
        r = client.post("/api/phonebooks/", json=payload, headers=admin_hdr)
        assert r.status_code == 409

    def test_same_name_allowed_in_different_companies(self, client, db, admin_hdr):
        a = make_company(db, code="PB3A")
        b = make_company(db, code="PB3B")
        for comp in (a, b):
            r = client.post("/api/phonebooks/", json={"name": "Shared", "company_id": str(comp.id)},
                            headers=admin_hdr)
            assert r.status_code == 201

    def test_blank_name_rejected(self, client, db, admin_hdr):
        comp = make_company(db, code="PB4")
        r = client.post("/api/phonebooks/", json={"name": "   ", "company_id": str(comp.id)},
                        headers=admin_hdr)
        assert r.status_code == 422

    def test_delete_cascades_contacts(self, client, db, admin_hdr):
        comp = make_company(db, code="PB5")
        pb = client.post("/api/phonebooks/", json={"name": "Temp", "company_id": str(comp.id)},
                         headers=admin_hdr).json()
        client.post(f"/api/phonebooks/{pb['id']}/contacts",
                    json={"mobile_no": "919000000001", "customer_name": "A"}, headers=admin_hdr)

        assert client.delete(f"/api/phonebooks/{pb['id']}", headers=admin_hdr).status_code == 204
        assert db.query(Phonebook).filter(Phonebook.id == pb["id"]).first() is None
        assert db.query(PhonebookContact).filter(
            PhonebookContact.phonebook_id == pb["id"]).count() == 0


class TestContactValidation:
    @pytest.fixture
    def pb(self, client, db, admin_hdr):
        comp = make_company(db, code="PBV")
        return client.post("/api/phonebooks/", json={"name": "V", "company_id": str(comp.id)},
                           headers=admin_hdr).json()

    def test_plus_prefix_is_stripped(self, client, pb, admin_hdr):
        r = client.post(f"/api/phonebooks/{pb['id']}/contacts",
                        json={"mobile_no": "+919876543210", "customer_name": "Ravi"},
                        headers=admin_hdr)
        assert r.status_code == 201
        # Stored digits-only, or opt-out matching and inbound routing break.
        assert r.json()["mobile_no"] == "919876543210"

    def test_duplicate_number_in_same_phonebook_rejected(self, client, pb, admin_hdr):
        body = {"mobile_no": "919876543211", "customer_name": "A"}
        assert client.post(f"/api/phonebooks/{pb['id']}/contacts", json=body,
                           headers=admin_hdr).status_code == 201
        r = client.post(f"/api/phonebooks/{pb['id']}/contacts",
                        json={"mobile_no": "919876543211", "customer_name": "B"},
                        headers=admin_hdr)
        assert r.status_code == 409

    def test_same_number_allowed_in_another_phonebook(self, client, db, pb, admin_hdr):
        """A customer can legitimately be on more than one list."""
        other = client.post("/api/phonebooks/",
                            json={"name": "V2", "company_id": str(pb["company_id"])},
                            headers=admin_hdr).json()
        body = {"mobile_no": "919876543212", "customer_name": "A"}
        assert client.post(f"/api/phonebooks/{pb['id']}/contacts", json=body,
                           headers=admin_hdr).status_code == 201
        assert client.post(f"/api/phonebooks/{other['id']}/contacts", json=body,
                           headers=admin_hdr).status_code == 201

    @pytest.mark.parametrize("mobile", ["123456", "9198765432101234", "91abc543210", ""])
    def test_invalid_numbers_rejected(self, client, pb, admin_hdr, mobile):
        r = client.post(f"/api/phonebooks/{pb['id']}/contacts",
                        json={"mobile_no": mobile, "customer_name": "X"}, headers=admin_hdr)
        assert r.status_code == 422

    def test_customer_name_is_required(self, client, pb, admin_hdr):
        r = client.post(f"/api/phonebooks/{pb['id']}/contacts",
                        json={"mobile_no": "919876543213", "customer_name": "   "},
                        headers=admin_hdr)
        assert r.status_code == 422

    def test_blank_optional_fields_become_null(self, client, pb, admin_hdr):
        r = client.post(f"/api/phonebooks/{pb['id']}/contacts",
                        json={"mobile_no": "919876543214", "customer_name": "A",
                              "email": "  ", "agent_id": ""},
                        headers=admin_hdr)
        assert r.status_code == 201
        assert r.json()["email"] is None
        assert r.json()["agent_id"] is None


class TestContactUpdate:
    @pytest.fixture
    def contact(self, client, db, admin_hdr):
        comp = make_company(db, code="PBU")
        pb = client.post("/api/phonebooks/", json={"name": "U", "company_id": str(comp.id)},
                         headers=admin_hdr).json()
        return client.post(f"/api/phonebooks/{pb['id']}/contacts",
                           json={"mobile_no": "919000000001", "customer_name": "Ravi",
                                 "email": "ravi@example.com", "agent_id": "AG1"},
                           headers=admin_hdr).json()

    def test_edit_updates_sent_fields(self, client, contact, admin_hdr):
        r = client.put(f"/api/phonebooks/contacts/{contact['id']}",
                       json={"customer_name": "Ravi Kumar", "mobile_no": "919000000002",
                             "email": "ravi@example.com", "agent_id": "AG1"},
                       headers=admin_hdr)
        assert r.status_code == 200
        assert r.json()["customer_name"] == "Ravi Kumar"
        assert r.json()["mobile_no"] == "919000000002"

    def test_explicit_null_clears_optional_fields(self, client, contact, admin_hdr):
        """The edit form sends null for an emptied field. Dropping those nulls
        made Email and Agent ID impossible to clear from the UI."""
        r = client.put(f"/api/phonebooks/contacts/{contact['id']}",
                       json={"customer_name": "Ravi", "mobile_no": "919000000001",
                             "email": None, "agent_id": None},
                       headers=admin_hdr)
        assert r.status_code == 200
        assert r.json()["email"] is None
        assert r.json()["agent_id"] is None

    def test_omitted_fields_are_left_alone(self, client, contact, admin_hdr):
        """A partial PUT must not blank whatever it did not mention."""
        r = client.put(f"/api/phonebooks/contacts/{contact['id']}",
                       json={"customer_name": "Renamed"}, headers=admin_hdr)
        assert r.status_code == 200
        assert r.json()["email"] == "ravi@example.com"
        assert r.json()["agent_id"] == "AG1"
        assert r.json()["mobile_no"] == "919000000001"

    def test_null_does_not_blank_required_fields(self, client, contact, admin_hdr):
        r = client.put(f"/api/phonebooks/contacts/{contact['id']}",
                       json={"customer_name": None, "mobile_no": None}, headers=admin_hdr)
        assert r.status_code == 200
        assert r.json()["customer_name"] == "Ravi"
        assert r.json()["mobile_no"] == "919000000001"


class TestCompanyIsolation:
    """A company-scoped user must not reach another company's phonebooks."""

    def test_scoped_user_only_lists_own_company(self, client, db, admin_hdr):
        mine = make_company(db, code="ISOA")
        theirs = make_company(db, code="ISOB")
        client.post("/api/phonebooks/", json={"name": "Mine", "company_id": str(mine.id)},
                    headers=admin_hdr)
        client.post("/api/phonebooks/", json={"name": "Theirs", "company_id": str(theirs.id)},
                    headers=admin_hdr)

        hdr = _make_scoped_user(db, client, mine.id, "iso_user")
        names = [p["name"] for p in client.get("/api/phonebooks/", headers=hdr).json()]
        assert names == ["Mine"]

    def test_scoped_user_cannot_read_other_company_phonebook(self, client, db, admin_hdr):
        mine = make_company(db, code="ISOC")
        theirs = make_company(db, code="ISOD")
        other = client.post("/api/phonebooks/", json={"name": "Theirs", "company_id": str(theirs.id)},
                            headers=admin_hdr).json()

        hdr = _make_scoped_user(db, client, mine.id, "iso_user2")
        assert client.get(f"/api/phonebooks/{other['id']}", headers=hdr).status_code == 403

    def test_scoped_user_cannot_delete_other_company_phonebook(self, client, db, admin_hdr):
        mine = make_company(db, code="ISOE")
        theirs = make_company(db, code="ISOF")
        other = client.post("/api/phonebooks/", json={"name": "Theirs", "company_id": str(theirs.id)},
                            headers=admin_hdr).json()

        hdr = _make_scoped_user(db, client, mine.id, "iso_user3")
        assert client.delete(f"/api/phonebooks/{other['id']}", headers=hdr).status_code == 403

    def test_scoped_user_create_ignores_payload_company(self, client, db, admin_hdr):
        """A crafted company_id must not place a phonebook under another company."""
        mine = make_company(db, code="ISOG")
        theirs = make_company(db, code="ISOH")

        hdr = _make_scoped_user(db, client, mine.id, "iso_user4")
        r = client.post("/api/phonebooks/",
                        json={"name": "Sneaky", "company_id": str(theirs.id)}, headers=hdr)
        assert r.status_code == 201
        assert r.json()["company_id"] == str(mine.id)


class TestAuth:
    def test_requires_authentication(self, client):
        assert client.get("/api/phonebooks/").status_code in (401, 403)


class TestPaginationHelper:
    """_build_window keeps the first and last page reachable at any size."""

    def test_small_page_count_shows_all(self):
        assert _build_window(1, 5) == [1, 2, 3, 4, 5]

    def test_large_count_elides_middle(self):
        w = _build_window(9, 40)
        assert w[0] == 1 and w[-1] == 40      # ends always reachable
        assert 0 in w                          # gap marker present
        assert 9 in w                          # current page present

    def test_no_gap_marker_adjacent_to_start(self):
        w = _build_window(2, 40)
        assert w[:4] == [1, 2, 3, 4]           # no "1 … 2"

    def test_page_math(self):
        p = Page(items=[], page=3, per_page=50, total=122)
        assert p.pages == 3
        assert p.first_index == 101
        assert p.last_index == 122
        assert p.has_next is False
        assert p.has_prev is True

    def test_empty_result_is_one_page(self):
        p = Page(items=[], page=1, per_page=50, total=0)
        assert p.pages == 1
        assert p.first_index == 0
        assert p.last_index == 0


class TestColumnMapper:
    """
    Header guessing. The important property is not that it maps everything —
    it is that it never maps something it is unsure about, because a wrong
    pre-selection is harder to notice than an empty dropdown.
    """

    def test_exact_field_names(self):
        m = suggest_mapping(["customer_name", "mobile_no", "email", "agent_id"])
        assert m == {"customer_name": "customer_name", "mobile_no": "mobile_no",
                     "email": "email", "agent_id": "agent_id"}

    def test_human_headers(self):
        m = suggest_mapping(["Customer Name", "WhatsApp No", "Email ID", "Agent"])
        assert m["customer_name"] == "Customer Name"
        assert m["mobile_no"] == "WhatsApp No"
        assert m["email"] == "Email ID"
        assert m["agent_id"] == "Agent"

    def test_abbreviations(self):
        m = suggest_mapping(["Cust Name", "WA No.", "E-Mail ID", "Assigned Agent"])
        assert m["customer_name"] == "Cust Name"
        assert m["mobile_no"] == "WA No."
        assert m["email"] == "E-Mail ID"
        assert m["agent_id"] == "Assigned Agent"

    def test_typos_are_matched_fuzzily(self):
        m = suggest_mapping(["custmer nam", "mobil no", "emial"])
        assert m["customer_name"] == "custmer nam"
        assert m["mobile_no"] == "mobil no"
        assert m["email"] == "emial"

    def test_meaningless_headers_map_to_nothing(self):
        """A column called "A" must not be guessed into a required field."""
        assert all(v is None for v in suggest_mapping(["A", "B", "C"]).values())

    def test_unrelated_headers_left_unmapped(self):
        m = suggest_mapping(["Full Name", "Phone", "Notes", "Region"])
        assert m["customer_name"] == "Full Name"
        assert m["mobile_no"] == "Phone"
        assert m["email"] is None
        assert m["agent_id"] is None

    def test_each_header_claimed_once(self):
        m = suggest_mapping(["Name", "Contact Number"])
        used = [v for v in m.values() if v]
        assert len(used) == len(set(used))

    def test_empty_input(self):
        assert all(v is None for v in suggest_mapping([]).values())


class TestCsvImport:
    @pytest.fixture
    def pb(self, client, db, admin_hdr):
        comp = make_company(db, code="IMP")
        return client.post("/api/phonebooks/", json={"name": "Imp", "company_id": str(comp.id)},
                           headers=admin_hdr).json()

    def _import(self, client, pb, hdr, rows):
        return client.post(f"/api/phonebooks/{pb['id']}/import",
                           json={"rows": rows}, headers=hdr)

    def test_valid_rows_imported(self, client, pb, admin_hdr):
        r = self._import(client, pb, admin_hdr, [
            {"row": 2, "mobile_no": "919876543210", "customer_name": "Ravi"},
            {"row": 3, "mobile_no": "919812345678", "customer_name": "Meera"},
        ])
        assert r.status_code == 200
        assert r.json() == {"imported": 2, "skipped": 0, "errors": []}

    def test_bad_row_skipped_not_fatal(self, client, pb, admin_hdr):
        """One invalid row must not abort the rest of the batch."""
        r = self._import(client, pb, admin_hdr, [
            {"row": 2, "mobile_no": "919876543211", "customer_name": "Good One"},
            {"row": 3, "mobile_no": "12345",        "customer_name": "Bad Number"},
            {"row": 4, "mobile_no": "919876543212", "customer_name": "Good Two"},
        ])
        body = r.json()
        assert body["imported"] == 2
        assert body["skipped"] == 1
        assert body["errors"][0]["row"] == 3

    def test_duplicate_within_batch_reported(self, client, pb, admin_hdr):
        r = self._import(client, pb, admin_hdr, [
            {"row": 2, "mobile_no": "919876543213", "customer_name": "First"},
            {"row": 3, "mobile_no": "919876543213", "customer_name": "Second"},
        ])
        body = r.json()
        assert body["imported"] == 1
        assert body["skipped"] == 1
        assert "Already in this phonebook" in body["errors"][0]["reason"]

    def test_duplicate_against_existing_reported(self, client, pb, admin_hdr):
        client.post(f"/api/phonebooks/{pb['id']}/contacts",
                    json={"mobile_no": "919876543214", "customer_name": "Existing"},
                    headers=admin_hdr)
        body = self._import(client, pb, admin_hdr, [
            {"row": 2, "mobile_no": "919876543214", "customer_name": "Dup"},
        ]).json()
        assert body["imported"] == 0
        assert body["skipped"] == 1

    def test_plus_stripped_on_import(self, client, pb, admin_hdr):
        self._import(client, pb, admin_hdr, [
            {"row": 2, "mobile_no": "+919876543215", "customer_name": "Plus"},
        ])
        listing = client.get(f"/api/phonebooks/{pb['id']}", headers=admin_hdr).json()
        assert listing["contact_count"] == 1

    def test_missing_name_reported_with_row_number(self, client, pb, admin_hdr):
        body = self._import(client, pb, admin_hdr, [
            {"row": 9, "mobile_no": "919876543216", "customer_name": "  "},
        ]).json()
        assert body["skipped"] == 1
        assert body["errors"][0]["row"] == 9
        assert "name" in body["errors"][0]["reason"].lower()

    def test_oversized_batch_rejected(self, client, pb, admin_hdr):
        rows = [{"row": i, "mobile_no": f"9198{i:08d}", "customer_name": f"C{i}"}
                for i in range(501)]
        assert self._import(client, pb, admin_hdr, rows).status_code == 413

    def test_import_respects_company_isolation(self, client, db, admin_hdr):
        mine = make_company(db, code="IMPA")
        theirs = make_company(db, code="IMPB")
        other = client.post("/api/phonebooks/", json={"name": "T", "company_id": str(theirs.id)},
                            headers=admin_hdr).json()
        hdr = _make_scoped_user(db, client, mine.id, "imp_user")
        r = client.post(f"/api/phonebooks/{other['id']}/import",
                        json={"rows": [{"row": 2, "mobile_no": "919876543217",
                                        "customer_name": "X"}]}, headers=hdr)
        assert r.status_code == 403
