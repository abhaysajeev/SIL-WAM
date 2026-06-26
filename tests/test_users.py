"""Users API — CRUD + permission enforcement + email auto-generation."""
import pytest
from tests.conftest import login, make_role, make_user


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ── POST /api/users/ ───────────────────────────────────────────────────────

class TestCreateUser:
    def test_create_minimal(self, client, sa):
        r = client.post("/api/users/",
                        json={"username": "newuser", "full_name": "New User",
                              "password": "password123"},
                        headers=_h(sa))
        assert r.status_code == 201
        d = r.json()
        assert d["username"] == "newuser"
        assert d["full_name"] == "New User"
        assert "email" not in d  # email removed from response

    def test_create_with_phone(self, client, sa):
        r = client.post("/api/users/",
                        json={"username": "withphone", "full_name": "With Phone",
                              "phone": "+91 9000000000", "password": "password123"},
                        headers=_h(sa))
        assert r.status_code == 201
        assert r.json()["phone"] == "+91 9000000000"

    def test_with_role_and_company(self, client, sa, db, company):
        role = make_role(db, "cv", "Company Viewer",
                         pages=["dashboard"], actions=["read"])
        r = client.post("/api/users/",
                        json={"username": "scoped", "full_name": "Scoped User",
                              "password": "password123",
                              "role_id": str(role.id),
                              "company_id": company["id"]},
                        headers=_h(sa))
        assert r.status_code == 201
        d = r.json()
        assert str(d["role_id"]) == str(role.id)
        assert str(d["company_id"]) == company["id"]

    def test_duplicate_username_returns_409(self, client, sa):
        body = {"username": "dup", "full_name": "Dup", "password": "password123"}
        client.post("/api/users/", json=body, headers=_h(sa))
        r = client.post("/api/users/", json=body, headers=_h(sa))
        assert r.status_code == 409

    def test_short_password_returns_422(self, client, sa):
        r = client.post("/api/users/",
                        json={"username": "shortpw", "full_name": "X", "password": "short"},
                        headers=_h(sa))
        assert r.status_code == 422

    def test_viewer_cannot_create_user(self, client, viewer):
        r = client.post("/api/users/",
                        json={"username": "x", "full_name": "X", "password": "password123"},
                        headers=_h(viewer))
        assert r.status_code == 403

    def test_unauthenticated_returns_403(self, client):
        r = client.post("/api/users/",
                        json={"username": "x", "full_name": "X", "password": "password123"})
        assert r.status_code == 403


# ── GET /api/users/ ────────────────────────────────────────────────────────

class TestListUsers:
    def test_super_admin_sees_all(self, client, sa, db):
        make_user(db, "user1")
        make_user(db, "user2")
        r = client.get("/api/users/", headers=_h(sa))
        assert r.status_code == 200
        usernames = [u["username"] for u in r.json()]
        # sa itself + user1 + user2
        assert "user1" in usernames
        assert "user2" in usernames

    def test_viewer_cannot_list(self, client, viewer):
        r = client.get("/api/users/", headers=_h(viewer))
        assert r.status_code == 403


# ── GET /api/users/{id} ────────────────────────────────────────────────────

class TestGetUser:
    def test_get_existing_user(self, client, sa, db):
        u = make_user(db, "getme")
        r = client.get(f"/api/users/{u.id}", headers=_h(sa))
        assert r.status_code == 200
        assert r.json()["username"] == "getme"

    def test_get_nonexistent_returns_404(self, client, sa):
        r = client.get("/api/users/00000000-0000-0000-0000-000000000999", headers=_h(sa))
        assert r.status_code == 404


# ── PUT /api/users/{id} ────────────────────────────────────────────────────

class TestUpdateUser:
    def test_update_full_name(self, client, sa, db):
        u = make_user(db, "updateme")
        r = client.put(f"/api/users/{u.id}",
                       json={"full_name": "Updated Name"},
                       headers=_h(sa))
        assert r.status_code == 200
        assert r.json()["full_name"] == "Updated Name"

    def test_deactivate_user(self, client, sa, db):
        u = make_user(db, "deactivate")
        r = client.put(f"/api/users/{u.id}",
                       json={"is_active": False},
                       headers=_h(sa))
        assert r.status_code == 200
        assert r.json()["is_active"] is False

    def test_deactivated_user_cannot_login(self, client, sa, db):
        u = make_user(db, "willblock", password="pass12345")
        client.put(f"/api/users/{u.id}", json={"is_active": False}, headers=_h(sa))
        r = client.post("/api/auth/login",
                        json={"username": "willblock", "password": "pass12345"})
        assert r.status_code == 403

    def test_viewer_cannot_update(self, client, viewer, db):
        u = make_user(db, "victim")
        r = client.put(f"/api/users/{u.id}",
                       json={"full_name": "Hacked"},
                       headers=_h(viewer))
        assert r.status_code == 403


# ── PUT /api/users/{id}/password ──────────────────────────────────────────

class TestChangePassword:
    def test_change_password_allows_new_login(self, client, sa, db):
        u = make_user(db, "pwchange", password="oldpass12")
        client.put(f"/api/users/{u.id}/password",
                   json={"new_password": "newpass99"},
                   headers=_h(sa))
        r = client.post("/api/auth/login",
                        json={"username": "pwchange", "password": "newpass99"})
        assert r.status_code == 200

    def test_old_password_rejected_after_change(self, client, sa, db):
        u = make_user(db, "oldpw", password="oldpass12")
        client.put(f"/api/users/{u.id}/password",
                   json={"new_password": "newpass99"},
                   headers=_h(sa))
        r = client.post("/api/auth/login",
                        json={"username": "oldpw", "password": "oldpass12"})
        assert r.status_code == 401

    def test_short_new_password_returns_422(self, client, sa, db):
        u = make_user(db, "shortpw2")
        r = client.put(f"/api/users/{u.id}/password",
                       json={"new_password": "short"},
                       headers=_h(sa))
        assert r.status_code == 422


# ── DELETE /api/users/{id} ─────────────────────────────────────────────────

class TestDeleteUser:
    def test_delete_removes_user(self, client, sa, db):
        u = make_user(db, "deleteme")
        r = client.delete(f"/api/users/{u.id}", headers=_h(sa))
        assert r.status_code == 204
        r2 = client.get(f"/api/users/{u.id}", headers=_h(sa))
        assert r2.status_code == 404

    def test_viewer_cannot_delete(self, client, viewer, db):
        u = make_user(db, "safe")
        r = client.delete(f"/api/users/{u.id}", headers=_h(viewer))
        assert r.status_code == 403
