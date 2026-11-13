"""
Phase 2c — phonebook import from Excel (.xlsx) and CSV, on the device and the web.
"""
import io
import zipfile

import openpyxl
import pytest

from app.models.phonebook import PhonebookContact
from app.services import contact_import as ci
from tests.test_agent_send import _admin, _agent_headers, _settings, _setup

AGENT_PB = "/agent-api/v1/phonebooks"


def _xlsx(rows, headers=("WhatsApp Number", "Customer Name", "Email")) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Parsing ──────────────────────────────────────────────────────────────────

class TestParse:
    def test_numeric_phone_cell_keeps_every_digit(self):
        rows, _ = ci.read_contacts(_xlsx([(918075571893, "Ravi", None)]), "a.xlsx")
        assert rows[0].mobile_no == "918075571893"

    def test_blank_rows_skipped_row_numbers_kept(self):
        rows, _ = ci.read_contacts(_xlsx([("8075571893", "A", ""), (None, None, None),
                                          ("7025985366", "B", "")]), "a.xlsx")
        assert [r.row for r in rows] == [2, 4]

    def test_dates_and_formulas_become_display_values(self):
        from datetime import datetime
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["WhatsApp Number", "Customer Name", "Joined"])
        ws.append(["8075571893", "=\"Ra\"&\"vi\"", datetime(2026, 11, 13)])
        buf = io.BytesIO()
        wb.save(buf)
        parsed = ci.parse(buf.getvalue(), "a.xlsx")
        # No cached value was ever calculated (openpyxl does not compute formulas),
        # so the cell reads empty — never the formula text itself.
        assert parsed.rows[0][1][1] in ("", "Ravi")
        assert parsed.rows[0][1][2] == "13-11-2026"

    def test_csv_windows_1252_and_semicolons(self):
        rows, mp = ci.read_contacts("Name;Mobile\nJosé;+971501234567\n".encode("cp1252"), "a.csv")
        assert rows[0].customer_name == "José"
        assert rows[0].mobile_no == "+971501234567"
        assert mp["mobile_no"] == "Mobile"

    def test_csv_with_bom(self):
        rows, _ = ci.read_contacts("﻿WhatsApp Number,Customer Name\n8075571893,A\n"
                                   .encode("utf-8"), "a.csv")
        assert rows[0].mobile_no == "8075571893"

    def test_no_number_column(self):
        with pytest.raises(ci.ImportError_) as e:
            ci.read_contacts(b"Name,City\nA,Kochi\n", "a.csv")
        assert e.value.code == "missing_number_column"

    def test_old_xls_gets_a_save_as_message(self):
        with pytest.raises(ci.ImportError_, match="xlsx"):
            ci.parse(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "a.xls")

    def test_zip_bomb_refused_before_opening(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/huge.xml", b"0" * (ci.MAX_UNZIPPED_BYTES + 1))
        with pytest.raises(ci.ImportError_) as e:
            ci.parse(buf.getvalue(), "a.xlsx")
        assert e.value.code == "file_too_large"

    def test_empty_and_oversized(self):
        with pytest.raises(ci.ImportError_):
            ci.parse(b"", "a.csv")
        with pytest.raises(ci.ImportError_) as e:
            ci.parse(b"a" * (ci.MAX_FILE_BYTES + 1), "a.csv")
        assert e.value.code == "file_too_large"


class TestSample:
    def test_xlsx_number_column_is_text(self):
        data, mime, name = ci.sample("xlsx")
        assert name == "phonebook_sample.xlsx" and "spreadsheetml" in mime
        wb = openpyxl.load_workbook(io.BytesIO(data))
        ws = wb["Contacts"]
        assert [c.value for c in ws[1]] == ci.SAMPLE_HEADERS
        assert ws.column_dimensions["A"].number_format == "@"
        assert ws.max_row == 1                     # headings only
        assert "Instructions" in wb.sheetnames

    def test_csv_headings_only(self):
        data, _mime, _name = ci.sample("csv")
        assert data.decode("utf-8-sig").strip() == "WhatsApp Number,Customer Name,Email"

    def test_formula_injection_stripped_from_headings(self):
        data, *_ = ci.sample_for(["=HYPERLINK(1)", "WhatsApp Number"], "csv", "x")
        assert not data.decode("utf-8-sig").startswith("=")

    def test_round_trip_sample_imports(self):
        data, *_ = ci.sample("xlsx")
        wb = openpyxl.load_workbook(io.BytesIO(data))
        wb["Contacts"].append(["8075571893", "Ravi", "r@x.com"])
        buf = io.BytesIO()
        wb.save(buf)
        rows, mp = ci.read_contacts(buf.getvalue(), "phonebook_sample.xlsx")
        assert rows[0].mobile_no == "8075571893" and rows[0].customer_name == "Ravi"


# ── Device endpoints ─────────────────────────────────────────────────────────

def _agent_book(client, db, code):
    comp, *_ = _setup(client, db, code)
    h = _agent_headers(client)
    pid = client.post(AGENT_PB, headers=h, json={"name": "Imported"}).json()["id"]
    return comp, h, pid


class TestDeviceImport:
    def test_sample_download(self, client, db):
        _setup(client, db, "IMS1")
        h = _agent_headers(client)
        r = client.get(f"{AGENT_PB}/sample", headers=h, params={"format": "xlsx"})
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert client.get(f"{AGENT_PB}/sample", headers=h,
                          params={"format": "csv"}).status_code == 200
        assert client.get(f"{AGENT_PB}/sample", headers=h,
                          params={"format": "pdf"}).status_code == 422

    def test_import_adds_normalises_and_reports(self, client, db):
        comp, h, pid = _agent_book(client, db, "IMP1")
        data = _xlsx([("8075571893", "Ravi", "r@x.com"),
                      ("+91 80755 71893", "Ravi again", ""),   # same person
                      ("9.18E+11", "Excel mangled", ""),
                      ("12", "Too short", ""),
                      (7025985366, "Anil", None)])
        r = client.post(f"{AGENT_PB}/{pid}/import", headers=h,
                        files={"file": ("list.xlsx", data, "application/octet-stream")})
        assert r.status_code == 201, r.text
        b = r.json()
        assert b["added"] == 2 and b["total_in_book"] == 2
        reasons = {s["row"]: s["reason"] for s in b["skipped"]}
        assert set(reasons) == {3, 4, 5}
        assert "Already" in reasons[3]
        assert "Excel" in reasons[4]
        assert sorted(c.mobile_no for c in db.query(PhonebookContact)
                      .filter_by(phonebook_id=pid)) == ["917025985366", "918075571893"]

    def test_import_respects_company_ceiling(self, client, db):
        comp, h, pid = _agent_book(client, db, "IMP2")
        _settings(db, comp.id, agent_max_contacts_per_phonebook=2)
        data = _xlsx([("8075571893", "A", ""), ("7025985366", "B", ""), ("9895000001", "C", "")])
        r = client.post(f"{AGENT_PB}/{pid}/import", headers=h,
                        files={"file": ("list.xlsx", data, "application/octet-stream")})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "phonebook_full"
        assert db.query(PhonebookContact).filter_by(phonebook_id=pid).count() == 0

    def test_admin_assigned_book_is_read_only(self, client, db):
        comp, _a, _t, pb, *_ = _setup(client, db, "IMP3")
        h = _agent_headers(client)
        r = client.post(f"{AGENT_PB}/{pb.id}/import", headers=h,
                        files={"file": ("l.csv", b"WhatsApp Number,Customer Name\n8075571893,A\n",
                                        "text/csv")})
        assert r.status_code in (403, 404)

    def test_unreadable_file(self, client, db):
        comp, h, pid = _agent_book(client, db, "IMP4")
        r = client.post(f"{AGENT_PB}/{pid}/import", headers=h,
                        files={"file": ("l.xlsx", b"PK\x03\x04garbage", "application/octet-stream")})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "file_unreadable"


# ── Web endpoints ────────────────────────────────────────────────────────────

class TestWebImport:
    def test_parse_returns_grid(self, client, db):
        hdr = _admin(client, db, "imp_admin")
        data = _xlsx([(918075571893, "Ravi", "")], headers=("Mobile", "Name", "Email"))
        r = client.post("/api/phonebooks/import/parse", headers=hdr,
                        files={"file": ("x.xlsx", data, "application/octet-stream")})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["headers"] == ["Mobile", "Name", "Email"]
        assert b["rows"] == [["918075571893", "Ravi", ""]]

    def test_sample_with_custom_columns(self, client, db):
        hdr = _admin(client, db, "imp_admin2")
        r = client.get("/api/phonebooks/import/sample", headers=hdr, params={
            "format": "xlsx", "columns": "WhatsApp Number,Customer Name,Param 1",
            "name": "promo_sample"})
        assert r.status_code == 200
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        assert [c.value for c in wb.active[1]] == ["WhatsApp Number", "Customer Name", "Param 1"]
        assert 'promo_sample.xlsx' in r.headers["content-disposition"]

    def test_web_contact_add_normalises(self, client, db):
        comp, _a, _t, pb, _u, hdr = _setup(client, db, "WEBN")
        r = client.post(f"/api/phonebooks/{pb.id}/contacts", headers=hdr,
                        json={"mobile_no": "8075571893", "customer_name": "Ravi"})
        assert r.status_code == 201, r.text
        assert r.json()["mobile_no"] == "918075571893"
