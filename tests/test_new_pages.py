"""
Module 3 & 7 — SSR page routes: access control for /services, /conversations, /reports.
"""
from fastapi.testclient import TestClient
from tests.conftest import (
    make_company, make_conversation, make_role, make_service, make_user,
)


# ── /services ──────────────────────────────────────────────────────────────────

class TestServicesPage:
    def test_unauthenticated_redirects_to_login(self, client):
        nc = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = nc.get("/services")
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_no_perm_returns_403(self, client, db):
        make_role(db, "no_svc", "No Svc", pages=["dashboard"], actions=["read"])
        make_user(db, "u_nosvc", role_name="no_svc")
        client.post("/api/auth/login", json={"username": "u_nosvc", "password": "testpass123"})
        r = client.get("/services")
        assert r.status_code == 403

    def test_super_admin_sees_page(self, client, db):
        make_user(db, "sa_svc", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_svc", "password": "testpass123"})
        r = client.get("/services")
        assert r.status_code == 200
        assert "Services" in r.text

    def test_admin_with_perm_sees_page(self, client, db):
        make_role(db, "svcmgr", "Svc Mgr", pages=["services"], actions=["read"])
        make_user(db, "u_svcmgr", role_name="svcmgr")
        client.post("/api/auth/login", json={"username": "u_svcmgr", "password": "testpass123"})
        r = client.get("/services")
        assert r.status_code == 200

    def test_service_rows_appear_in_list(self, client, db):
        make_user(db, "sa_svcrows", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_svcrows", "password": "testpass123"})
        comp = make_company(db, code="SVCTEST")
        conv = make_conversation(db, comp.id)
        make_service(db, conv.id, comp.id, service_id="ORD-TEST-001")
        r = client.get("/services")
        assert r.status_code == 200
        assert "ORD-TEST-001" in r.text

    def test_service_detail_returns_200(self, client, db):
        make_user(db, "sa_svcd", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_svcd", "password": "testpass123"})
        comp = make_company(db, code="SVCD")
        conv = make_conversation(db, comp.id)
        svc  = make_service(db, conv.id, comp.id, service_id="ORD-DETAIL-001")
        r = client.get(f"/services/{svc.id}")
        assert r.status_code == 200
        assert "ORD-DETAIL-001" in r.text

    def test_service_detail_404_for_unknown(self, client, db):
        make_user(db, "sa_svc404", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_svc404", "password": "testpass123"})
        r = client.get("/services/00000000-0000-0000-0000-000000000999")
        assert r.status_code == 404

    def test_company_scoped_user_sees_only_own_services(self, client, db):
        comp_a = make_company(db, name="A Corp", code="ACORP")
        comp_b = make_company(db, name="B Corp", code="BCORP")
        conv_a = make_conversation(db, comp_a.id, "911111111111")
        conv_b = make_conversation(db, comp_b.id, "922222222222")
        make_service(db, conv_a.id, comp_a.id, service_id="SVC-ONLY-A")
        make_service(db, conv_b.id, comp_b.id, service_id="SVC-ONLY-B")

        make_role(db, "comp_svc", "Comp Svc", pages=["services"], actions=["read"])
        make_user(db, "u_compsvc", role_name="comp_svc", company_id=comp_a.id)
        client.post("/api/auth/login", json={"username": "u_compsvc", "password": "testpass123"})
        r = client.get("/services")
        assert r.status_code == 200
        assert "SVC-ONLY-A" in r.text
        assert "SVC-ONLY-B" not in r.text

    def test_status_filter_applied(self, client, db):
        make_user(db, "sa_svcflt", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_svcflt", "password": "testpass123"})
        comp = make_company(db, code="FLTCO")
        conv = make_conversation(db, comp.id)
        make_service(db, conv.id, comp.id, service_id="SVC-DONE", status="completed")
        make_service(db, conv.id, comp.id, service_id="SVC-PROG", status="in_progress")
        r = client.get("/services?status=completed")
        assert r.status_code == 200
        assert "SVC-DONE" in r.text
        assert "SVC-PROG" not in r.text


# ── /conversations ─────────────────────────────────────────────────────────────

class TestConversationsPage:
    def test_unauthenticated_redirects_to_login(self, client):
        nc = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = nc.get("/conversations")
        assert r.status_code == 302

    def test_no_perm_returns_403(self, client, db):
        make_role(db, "no_conv", "No Conv", pages=["dashboard"], actions=["read"])
        make_user(db, "u_noconv", role_name="no_conv")
        client.post("/api/auth/login", json={"username": "u_noconv", "password": "testpass123"})
        r = client.get("/conversations")
        assert r.status_code == 403

    def test_super_admin_sees_page(self, client, db):
        make_user(db, "sa_conv", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_conv", "password": "testpass123"})
        r = client.get("/conversations")
        assert r.status_code == 200
        assert "Conversations" in r.text

    def test_conversation_rows_appear(self, client, db):
        make_user(db, "sa_convrows", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_convrows", "password": "testpass123"})
        comp = make_company(db, code="CONVTEST")
        make_conversation(db, comp.id, "918888888888")
        r = client.get("/conversations")
        assert r.status_code == 200
        assert "918888888888" in r.text

    def test_conversation_detail_returns_200(self, client, db):
        make_user(db, "sa_convd", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_convd", "password": "testpass123"})
        comp = make_company(db, code="CONVD")
        conv = make_conversation(db, comp.id, "917777777777")
        r = client.get(f"/conversations/{conv.id}")
        assert r.status_code == 200
        assert "917777777777" in r.text

    def test_conversation_detail_404_for_unknown(self, client, db):
        make_user(db, "sa_convd404", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_convd404", "password": "testpass123"})
        r = client.get("/conversations/00000000-0000-0000-0000-000000000999")
        assert r.status_code == 404

    def test_company_scoped_user_sees_own_conversations(self, client, db):
        comp_a = make_company(db, name="Alpha", code="ALPHA")
        comp_b = make_company(db, name="Beta",  code="BETA2")
        make_conversation(db, comp_a.id, "91AAAAAAAAAA")
        make_conversation(db, comp_b.id, "91BBBBBBBBBB")

        make_role(db, "conv_ro", "Conv RO", pages=["conversations"], actions=["read"])
        make_user(db, "u_convro", role_name="conv_ro", company_id=comp_a.id)
        client.post("/api/auth/login", json={"username": "u_convro", "password": "testpass123"})
        r = client.get("/conversations")
        assert r.status_code == 200
        assert "91AAAAAAAAAA" in r.text
        assert "91BBBBBBBBBB" not in r.text


# ── /reports ───────────────────────────────────────────────────────────────────

class TestReportsPage:
    def test_unauthenticated_redirects_to_login(self, client):
        nc = TestClient(client.app, raise_server_exceptions=True, follow_redirects=False)
        r = nc.get("/reports")
        assert r.status_code == 302

    def test_no_perm_returns_403(self, client, db):
        make_role(db, "no_rep", "No Rep", pages=["dashboard"], actions=["read"])
        make_user(db, "u_norep", role_name="no_rep")
        client.post("/api/auth/login", json={"username": "u_norep", "password": "testpass123"})
        r = client.get("/reports")
        assert r.status_code == 403

    def test_super_admin_sees_reports_page(self, client, db):
        make_user(db, "sa_rep", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_rep", "password": "testpass123"})
        r = client.get("/reports")
        assert r.status_code == 200
        assert "Reports" in r.text

    def test_viewer_with_reports_perm_sees_page(self, client, db):
        make_role(db, "rep_viewer", "Rep Viewer",
                  pages=["dashboard", "reports"], actions=["read"])
        make_user(db, "u_repview", role_name="rep_viewer")
        client.post("/api/auth/login", json={"username": "u_repview", "password": "testpass123"})
        r = client.get("/reports")
        assert r.status_code == 200

    def test_chart_js_included(self, client, db):
        make_user(db, "sa_repjs", role_name="super_admin")
        client.post("/api/auth/login", json={"username": "sa_repjs", "password": "testpass123"})
        r = client.get("/reports")
        assert "chart.js" in r.text.lower() or "Chart" in r.text
