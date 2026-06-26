"""Companies API — CRUD + permission enforcement."""
import pytest
from tests.conftest import login, make_role, make_user


# ── POST /api/companies/ ───────────────────────────────────────────────────

class TestCreateCompany:
    def test_super_admin_creates_company(self, client, sa):
        r = client.post("/api/companies/",
                        json={"name": "Acme", "company_code": "ACME"},
                        headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 201
        d = r.json()
        assert d["name"] == "Acme"
        assert d["company_code"] == "ACME"
        assert d["is_active"] is True

    def test_admin_with_permission_creates_company(self, client, admin_full):
        r = client.post("/api/companies/",
                        json={"name": "Beta Ltd", "company_code": "BETA"},
                        headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 201

    def test_viewer_cannot_create_company(self, client, viewer):
        r = client.post("/api/companies/",
                        json={"name": "X", "company_code": "X"},
                        headers={"Authorization": f"Bearer {viewer}"})
        assert r.status_code == 403

    def test_unauthenticated_returns_403(self, client):
        r = client.post("/api/companies/", json={"name": "X", "company_code": "X"})
        assert r.status_code == 403

    def test_duplicate_code_returns_409(self, client, sa):
        h = {"Authorization": f"Bearer {sa}"}
        client.post("/api/companies/", json={"name": "First", "company_code": "DUP"}, headers=h)
        r = client.post("/api/companies/", json={"name": "Second", "company_code": "DUP"}, headers=h)
        assert r.status_code == 409

    def test_missing_name_returns_422(self, client, sa):
        r = client.post("/api/companies/",
                        json={"company_code": "NONAME"},
                        headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 422

    def test_missing_code_returns_422(self, client, sa):
        r = client.post("/api/companies/",
                        json={"name": "NoCode"},
                        headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 422


# ── GET /api/companies/ ────────────────────────────────────────────────────

class TestListCompanies:
    def test_returns_all_companies(self, client, sa):
        h = {"Authorization": f"Bearer {sa}"}
        client.post("/api/companies/", json={"name": "A", "company_code": "AAA"}, headers=h)
        client.post("/api/companies/", json={"name": "B", "company_code": "BBB"}, headers=h)
        r = client.get("/api/companies/", headers=h)
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_empty_list_returns_200(self, client, sa):
        r = client.get("/api/companies/", headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 200
        assert r.json() == []

    def test_viewer_cannot_list(self, client, viewer):
        r = client.get("/api/companies/", headers={"Authorization": f"Bearer {viewer}"})
        assert r.status_code == 403


# ── GET /api/companies/{id} ────────────────────────────────────────────────

class TestGetCompany:
    def test_get_existing_company(self, client, sa, company):
        r = client.get(f"/api/companies/{company['id']}",
                       headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 200
        assert r.json()["company_code"] == "TESTCORP"

    def test_get_nonexistent_returns_404(self, client, sa):
        r = client.get("/api/companies/00000000-0000-0000-0000-000000000999",
                       headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 404


# ── PUT /api/companies/{id} ────────────────────────────────────────────────

class TestUpdateCompany:
    def test_update_name(self, client, sa, company):
        r = client.put(f"/api/companies/{company['id']}",
                       json={"name": "Updated Name"},
                       headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"

    def test_update_active_status(self, client, sa, company):
        r = client.put(f"/api/companies/{company['id']}",
                       json={"is_active": False},
                       headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 200
        assert r.json()["is_active"] is False

    def test_viewer_cannot_update(self, client, viewer, company):
        r = client.put(f"/api/companies/{company['id']}",
                       json={"name": "Hacked"},
                       headers={"Authorization": f"Bearer {viewer}"})
        assert r.status_code == 403

    def test_update_nonexistent_returns_404(self, client, sa):
        r = client.put("/api/companies/00000000-0000-0000-0000-000000000999",
                       json={"name": "Ghost"},
                       headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 404


# ── DELETE /api/companies/{id} ─────────────────────────────────────────────

class TestDeleteCompany:
    def test_delete_removes_company(self, client, sa, company):
        h = {"Authorization": f"Bearer {sa}"}
        r = client.delete(f"/api/companies/{company['id']}", headers=h)
        assert r.status_code == 204
        r2 = client.get(f"/api/companies/{company['id']}", headers=h)
        assert r2.status_code == 404

    def test_viewer_cannot_delete(self, client, viewer, company):
        r = client.delete(f"/api/companies/{company['id']}",
                          headers={"Authorization": f"Bearer {viewer}"})
        assert r.status_code == 403

    def test_delete_nonexistent_returns_404(self, client, sa):
        r = client.delete("/api/companies/00000000-0000-0000-0000-000000000999",
                          headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 404
