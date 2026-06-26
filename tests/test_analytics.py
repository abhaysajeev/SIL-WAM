"""Module 7 — Analytics API: auth, response shape, company scoping."""
from tests.conftest import (
    make_company, make_conversation, make_role, make_user,
)


class TestAnalyticsSummary:
    def test_no_auth_returns_403(self, client):
        r = client.get("/api/analytics/summary")
        assert r.status_code == 403

    def test_no_reports_perm_returns_403(self, client, db, viewer):
        r = client.get("/api/analytics/summary",
                       headers={"Authorization": f"Bearer {viewer}"})
        # viewer fixture has only dashboard+reports read — wait, viewer HAS reports.read
        # Use a role with no reports perm instead
        make_role(db, "noreports2", "No Reports 2", pages=["dashboard"], actions=["read"])
        make_user(db, "u_norep2", role_name="noreports2")
        from tests.conftest import login
        tok = login(client, "u_norep2")["access_token"]
        r = client.get("/api/analytics/summary",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403

    def test_admin_sees_summary(self, client, admin_full):
        r = client.get("/api/analytics/summary",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        assert "total" in d
        assert "inbound" in d
        assert "outbound" in d
        assert "sent" in d
        assert "delivered" in d
        assert "read" in d
        assert "failed" in d
        assert "delivery_rate" in d
        assert "daily" in d
        assert isinstance(d["daily"], list)

    def test_super_admin_sees_summary(self, client, db, sa):
        r = client.get("/api/analytics/summary",
                       headers={"Authorization": f"Bearer {sa}"})
        assert r.status_code == 200

    def test_returns_zeros_with_no_data(self, client, admin_full):
        r = client.get("/api/analytics/summary",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 0
        assert d["delivery_rate"] == 0

    def test_date_range_params_accepted(self, client, admin_full):
        r = client.get("/api/analytics/summary?from_date=2026-01-01&to_date=2026-06-30",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        assert d["from_date"] == "2026-01-01"
        assert d["to_date"] == "2026-06-30"

    def test_company_scoped_user_returns_200(self, client, db):
        comp = make_company(db, code="ANALCO")
        make_role(db, "anal_ro", "Anal RO", pages=["reports"], actions=["read"])
        make_user(db, "u_analro", role_name="anal_ro", company_id=comp.id)
        from tests.conftest import login
        tok = login(client, "u_analro")["access_token"]
        r = client.get("/api/analytics/summary",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200


class TestAnalyticsServices:
    def test_no_auth_returns_403(self, client):
        r = client.get("/api/analytics/services")
        assert r.status_code == 403

    def test_admin_sees_services(self, client, admin_full):
        r = client.get("/api/analytics/services",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        for key in ("total", "completed", "in_progress", "waiting", "expired", "failed",
                    "completion_rate", "daily"):
            assert key in d, f"missing key: {key}"
        assert isinstance(d["daily"], list)

    def test_zeros_with_no_services(self, client, admin_full):
        r = client.get("/api/analytics/services",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 0
        assert d["completed"] == 0

    def test_status_breakdown_counts_match(self, client, db, sa, admin_full):
        comp = make_company(db, code="STATCO")
        conv = make_conversation(db, comp.id)
        from tests.conftest import make_service
        make_service(db, conv.id, comp.id, status="completed")
        make_service(db, conv.id, comp.id, status="completed")
        make_service(db, conv.id, comp.id, status="expired")

        r = client.get("/api/analytics/services",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 3
        assert d["completed"] == 2
        assert d["expired"] == 1

    def test_company_scoped_user_sees_only_own(self, client, db):
        comp_a = make_company(db, name="Scoped A", code="SCPA")
        comp_b = make_company(db, name="Scoped B", code="SCPB")
        conv_a = make_conversation(db, comp_a.id, "91111")
        conv_b = make_conversation(db, comp_b.id, "92222")
        from tests.conftest import make_service
        make_service(db, conv_a.id, comp_a.id, status="completed")
        make_service(db, conv_b.id, comp_b.id, status="completed")
        make_service(db, conv_b.id, comp_b.id, status="completed")

        make_role(db, "rep_ro2", "Rep RO2", pages=["reports"], actions=["read"])
        make_user(db, "u_repro2", role_name="rep_ro2", company_id=comp_a.id)
        from tests.conftest import login
        tok = login(client, "u_repro2")["access_token"]
        r = client.get("/api/analytics/services",
                       headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 1   # only company A's service
        assert d["completed"] == 1


class TestAnalyticsConversations:
    def test_no_auth_returns_403(self, client):
        r = client.get("/api/analytics/conversations")
        assert r.status_code == 403

    def test_admin_sees_conversations(self, client, admin_full):
        r = client.get("/api/analytics/conversations",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        d = r.json()
        assert "total" in d
        assert "active_7d" in d
        assert "total_messages" in d

    def test_counts_conversations_correctly(self, client, db, admin_full):
        comp = make_company(db, code="CONVCO2")
        make_conversation(db, comp.id, "911")
        make_conversation(db, comp.id, "922")
        r = client.get("/api/analytics/conversations",
                       headers={"Authorization": f"Bearer {admin_full}"})
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_viewer_with_reports_perm_sees_conversations(self, client, viewer):
        r = client.get("/api/analytics/conversations",
                       headers={"Authorization": f"Bearer {viewer}"})
        assert r.status_code == 200
