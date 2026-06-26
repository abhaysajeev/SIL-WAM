"""SSR page routes — session auth, sidebar rendering, access control."""
import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_role, make_user, login


# ── Unauthenticated redirects ──────────────────────────────────────────────

class TestUnauthenticatedRedirect:
    def test_dashboard_redirects_to_login(self, client):
        c = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = c.get("/dashboard")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_companies_redirects_to_login(self, client):
        c = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = c.get("/companies")
        assert r.status_code == 302

    def test_users_redirects_to_login(self, client):
        c = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = c.get("/users")
        assert r.status_code == 302

    def test_roles_redirects_to_login(self, client):
        c = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = c.get("/roles")
        assert r.status_code == 302

    def test_login_page_accessible(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "Sign In" in r.text or "login" in r.text.lower()


# ── Dashboard page ─────────────────────────────────────────────────────────

class TestDashboardPage:
    def test_super_admin_sees_dashboard(self, client, db):
        make_user(db, "sadash", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sadash", "password": "testpass123"})
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "Dashboard" in r.text

    def test_dashboard_shows_role_chip(self, client, db):
        make_user(db, "roleship", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "roleship", "password": "testpass123"})
        r = client.get("/dashboard")
        assert "super admin" in r.text.lower() or "super_admin" in r.text.lower()

    def test_super_admin_sees_roles_in_sidebar(self, client, db):
        make_user(db, "saroles", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "saroles", "password": "testpass123"})
        r = client.get("/dashboard")
        assert "/roles" in r.text

    def test_viewer_cannot_see_roles_in_sidebar(self, client, db):
        make_role(db, "company_viewer", "Viewer",
                  pages=["dashboard", "reports"], actions=["read"])
        make_user(db, "cvdash", role_name="company_viewer")
        client.post("/api/auth/login", json={"username": "cvdash", "password": "testpass123"})
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "/roles" not in r.text

    def test_messaging_not_in_sidebar(self, client, db):
        """Messaging module removed — must not appear in sidebar."""
        make_user(db, "nomsg", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "nomsg", "password": "testpass123"})
        r = client.get("/dashboard")
        assert "/messaging/" not in r.text
        assert "Templates" not in r.text
        assert "Promotional" not in r.text


# ── Companies page ─────────────────────────────────────────────────────────

class TestCompaniesPage:
    def test_super_admin_sees_companies_page(self, client, db):
        make_user(db, "sacomp", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sacomp", "password": "testpass123"})
        r = client.get("/companies")
        assert r.status_code == 200
        assert "Companies" in r.text

    def test_viewer_gets_access_denied(self, client, db):
        make_role(db, "company_viewer", "Viewer",
                  pages=["dashboard", "reports"], actions=["read"])
        make_user(db, "cvcomp", role_name="company_viewer")
        client.post("/api/auth/login", json={"username": "cvcomp", "password": "testpass123"})
        r = client.get("/companies")
        assert r.status_code == 403

    def test_new_link_shown_when_create_perm(self, client, db):
        make_role(db, "mgr", "Manager",
                  pages=["companies"], actions=["read", "create"])
        make_user(db, "mgr1", role_name="mgr")
        client.post("/api/auth/login", json={"username": "mgr1", "password": "testpass123"})
        r = client.get("/companies")
        assert r.status_code == 200
        assert "/companies/new" in r.text

    def test_new_link_hidden_when_no_create_perm(self, client, db):
        make_role(db, "readonlymgr", "Read Only",
                  pages=["companies"], actions=["read"])
        make_user(db, "romgr", role_name="readonlymgr")
        client.post("/api/auth/login", json={"username": "romgr", "password": "testpass123"})
        r = client.get("/companies")
        assert r.status_code == 200
        assert "/companies/new" not in r.text


# ── Users page ─────────────────────────────────────────────────────────────

class TestUsersPage:
    def test_super_admin_sees_users_page(self, client, db):
        make_user(db, "sausers", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sausers", "password": "testpass123"})
        r = client.get("/users")
        assert r.status_code == 200
        assert "Users" in r.text

    def test_viewer_gets_access_denied(self, client, db):
        make_role(db, "cvusers", "Viewer", pages=["dashboard"], actions=["read"])
        make_user(db, "cvusers1", role_name="cvusers")
        client.post("/api/auth/login", json={"username": "cvusers1", "password": "testpass123"})
        r = client.get("/users")
        assert r.status_code == 403


# ── Roles page ─────────────────────────────────────────────────────────────

class TestRolesPage:
    def test_super_admin_sees_roles_page(self, client, db):
        make_user(db, "saroles2", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "saroles2", "password": "testpass123"})
        r = client.get("/roles")
        assert r.status_code == 200
        assert "Roles" in r.text

    def test_admin_gets_access_denied(self, client, admin_full):
        """Roles page is super_admin only."""
        client.post("/api/auth/login", json={"username": "admin", "password": "testpass123"})
        r = client.get("/roles")
        assert r.status_code == 403

    def test_permissions_page_renders(self, client, db):
        role = make_role(db, "permpg", "Perm Page",
                         pages=["dashboard"], actions=["read"])
        make_user(db, "saperm", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "saperm", "password": "testpass123"})
        r = client.get(f"/roles/{role.id}/permissions")
        assert r.status_code == 200
        assert "perm-check" in r.text or "Save Permissions" in r.text

    def test_permissions_page_nonexistent_role_returns_404(self, client, db):
        make_user(db, "sa404", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa404", "password": "testpass123"})
        r = client.get("/roles/00000000-0000-0000-0000-000000000999/permissions")
        assert r.status_code == 404


# ── Logout ─────────────────────────────────────────────────────────────────

class TestLogoutPage:
    def test_logout_clears_session_and_redirects(self, client, db):
        make_user(db, "logme", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "logme", "password": "testpass123"})

        nc = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        # Copy cookies from logged-in client
        for name, val in client.cookies.items():
            nc.cookies.set(name, val)

        r = nc.get("/auth/logout")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_after_logout_dashboard_redirects(self, client, db):
        make_user(db, "logme2", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "logme2", "password": "testpass123"})
        client.get("/auth/logout")

        nc = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = nc.get("/dashboard")
        assert r.status_code == 302
