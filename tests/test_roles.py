"""Roles API + permission matrix — CRUD and enforcement."""
import pytest
from tests.conftest import make_role, make_user, login


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ── POST /api/roles/ ───────────────────────────────────────────────────────

class TestCreateRole:
    def test_super_admin_creates_role(self, client, sa):
        r = client.post("/api/roles/",
                        json={"name": "analyst", "display_name": "Analyst"},
                        headers=_h(sa))
        assert r.status_code == 201
        d = r.json()
        assert d["name"] == "analyst"
        assert d["is_system"] is False

    def test_duplicate_name_returns_409(self, client, sa):
        body = {"name": "dup_role", "display_name": "Dup"}
        client.post("/api/roles/", json=body, headers=_h(sa))
        r = client.post("/api/roles/", json=body, headers=_h(sa))
        assert r.status_code == 409

    def test_non_super_admin_cannot_create(self, client, admin_full):
        r = client.post("/api/roles/",
                        json={"name": "hack", "display_name": "Hack"},
                        headers=_h(admin_full))
        assert r.status_code == 403

    def test_unauthenticated_returns_403(self, client):
        r = client.post("/api/roles/", json={"name": "x", "display_name": "X"})
        assert r.status_code == 403


# ── GET /api/roles/ ────────────────────────────────────────────────────────

class TestListRoles:
    def test_returns_created_roles(self, client, sa):
        h = _h(sa)
        client.post("/api/roles/", json={"name": "r1", "display_name": "R1"}, headers=h)
        client.post("/api/roles/", json={"name": "r2", "display_name": "R2"}, headers=h)
        r = client.get("/api/roles/", headers=h)
        assert r.status_code == 200
        names = [x["name"] for x in r.json()]
        assert "r1" in names and "r2" in names

    def test_non_super_admin_returns_403(self, client, admin_full):
        r = client.get("/api/roles/", headers=_h(admin_full))
        assert r.status_code == 403


# ── PUT /api/roles/{id} ────────────────────────────────────────────────────

class TestUpdateRole:
    def test_update_display_name(self, client, sa):
        h = _h(sa)
        role = client.post("/api/roles/",
                           json={"name": "updrole", "display_name": "Old"},
                           headers=h).json()
        r = client.put(f"/api/roles/{role['id']}",
                       json={"display_name": "New"},
                       headers=h)
        assert r.status_code == 200
        assert r.json()["display_name"] == "New"

    def test_cannot_update_system_role(self, client, sa, db):
        from app.models.role import Role
        # sa fixture already created super_admin role — just look it up
        sr = db.query(Role).filter(Role.name == "super_admin").first()
        r = client.put(f"/api/roles/{sr.id}",
                       json={"display_name": "Hacked"},
                       headers=_h(sa))
        assert r.status_code == 403


# ── DELETE /api/roles/{id} ─────────────────────────────────────────────────

class TestDeleteRole:
    def test_delete_custom_role(self, client, sa):
        h = _h(sa)
        role = client.post("/api/roles/",
                           json={"name": "todel", "display_name": "To Delete"},
                           headers=h).json()
        r = client.delete(f"/api/roles/{role['id']}", headers=h)
        assert r.status_code == 204

    def test_cannot_delete_system_role(self, client, sa, db):
        from app.models.role import Role
        sr = db.query(Role).filter(Role.name == "super_admin").first()
        r = client.delete(f"/api/roles/{sr.id}", headers=_h(sa))
        assert r.status_code == 403


# ── GET /api/roles/{id}/permissions ───────────────────────────────────────

class TestGetPermissions:
    def test_returns_permission_rows(self, client, sa, db):
        role = make_role(db, "perm_read", "Perm Read",
                         pages=["dashboard", "reports"], actions=["read"])
        r = client.get(f"/api/roles/{role.id}/permissions", headers=_h(sa))
        assert r.status_code == 200
        d = r.json()
        assert d["role"]["name"] == "perm_read"
        pages = {p["page_name"]: p for p in d["permissions"]}
        assert pages["dashboard"]["can_read"] is True
        assert pages["dashboard"]["can_create"] is False


# ── PUT /api/roles/{id}/permissions ───────────────────────────────────────

class TestUpdatePermissions:
    def test_set_permissions(self, client, sa):
        h = _h(sa)
        role = client.post("/api/roles/",
                           json={"name": "permtest", "display_name": "PermTest"},
                           headers=h).json()
        payload = {"permissions": [
            {"page_name": "dashboard",  "can_read": True,  "can_create": False, "can_write": False, "can_delete": False},
            {"page_name": "reports",    "can_read": True,  "can_create": False, "can_write": False, "can_delete": False},
            {"page_name": "companies",  "can_read": True,  "can_create": True,  "can_write": True,  "can_delete": False},
        ]}
        r = client.put(f"/api/roles/{role['id']}/permissions", json=payload, headers=h)
        assert r.status_code == 204

        # Verify persisted
        r2 = client.get(f"/api/roles/{role['id']}/permissions", headers=h)
        pages = {p["page_name"]: p for p in r2.json()["permissions"]}
        assert pages["companies"]["can_create"] is True
        assert pages["companies"]["can_delete"] is False
        assert pages["reports"]["can_read"] is True

    def test_replace_replaces_all(self, client, sa):
        """Second PUT fully replaces first — old rows gone."""
        h = _h(sa)
        role = client.post("/api/roles/",
                           json={"name": "replace_test", "display_name": "Replace"},
                           headers=h).json()
        rid = role["id"]
        # First save: dashboard + reports
        client.put(f"/api/roles/{rid}/permissions",
                   json={"permissions": [
                       {"page_name": "dashboard", "can_read": True,  "can_create": False, "can_write": False, "can_delete": False},
                       {"page_name": "reports",   "can_read": True,  "can_create": False, "can_write": False, "can_delete": False},
                   ]}, headers=h)
        # Second save: only companies
        client.put(f"/api/roles/{rid}/permissions",
                   json={"permissions": [
                       {"page_name": "companies", "can_read": True, "can_create": False, "can_write": False, "can_delete": False},
                   ]}, headers=h)

        r = client.get(f"/api/roles/{rid}/permissions", headers=h)
        pages = {p["page_name"] for p in r.json()["permissions"]}
        assert "companies" in pages
        assert "dashboard" not in pages   # old row gone
        assert "reports" not in pages

    def test_invalid_page_name_returns_422(self, client, sa):
        h = _h(sa)
        role = client.post("/api/roles/",
                           json={"name": "badpage", "display_name": "Bad"},
                           headers=h).json()
        r = client.put(f"/api/roles/{role['id']}/permissions",
                       json={"permissions": [
                           {"page_name": "nonexistent_page",
                            "can_read": True, "can_create": False,
                            "can_write": False, "can_delete": False},
                       ]}, headers=h)
        assert r.status_code == 422

    def test_cannot_set_invalid_action_for_page(self, client, sa):
        """dashboard only supports read — can_create must be rejected."""
        h = _h(sa)
        role = client.post("/api/roles/",
                           json={"name": "invact", "display_name": "Inv"},
                           headers=h).json()
        r = client.put(f"/api/roles/{role['id']}/permissions",
                       json={"permissions": [
                           {"page_name": "dashboard",
                            "can_read": True, "can_create": True,
                            "can_write": False, "can_delete": False},
                       ]}, headers=h)
        assert r.status_code == 422

    def test_cannot_update_system_role_permissions(self, client, sa, db):
        from app.models.role import Role
        sr = db.query(Role).filter(Role.name == "super_admin").first()
        r = client.put(f"/api/roles/{sr.id}/permissions",
                       json={"permissions": []},
                       headers=_h(sa))
        assert r.status_code == 403

    def test_non_super_admin_cannot_update_permissions(self, client, admin_full, db):
        role = make_role(db, "target", "Target")
        r = client.put(f"/api/roles/{role.id}/permissions",
                       json={"permissions": []},
                       headers=_h(admin_full))
        assert r.status_code == 403


# ── Permission enforcement on other APIs ──────────────────────────────────

class TestPermissionEnforcement:
    def test_role_with_read_only_cannot_write(self, client, db):
        """User with companies.read only → PUT /api/companies/ returns 403."""
        make_role(db, "readonly", "Read Only",
                  pages=["companies"], actions=["read"])
        make_user(db, "rdonly", role_name="readonly")
        tok = login(client, "rdonly")["access_token"]

        # Can list
        r = client.get("/api/companies/", headers=_h(tok))
        assert r.status_code == 200

        # Cannot create
        r = client.post("/api/companies/",
                        json={"name": "X", "company_code": "X"},
                        headers=_h(tok))
        assert r.status_code == 403

    def test_no_role_returns_403_on_protected_route(self, client, db):
        make_user(db, "norole")
        tok = login(client, "norole")["access_token"]
        r = client.get("/api/companies/", headers=_h(tok))
        assert r.status_code == 403
