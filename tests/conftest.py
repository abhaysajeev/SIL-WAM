import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models.api_key import CompanyApiKey
from app.models.company import Company
from app.models.conversation import Conversation, MobileQueue, Service
from app.models.role import Role, RolePagePermission
from app.models.user import User
from app.models.whatsapp import WhatsAppAccount, WhatsAppTemplate
from app.core.resources import ALL_PAGE_NAMES

TEST_DATABASE_URL = "postgresql://silwam:silwam_dev@localhost:5433/sil_wam_test"

test_engine = create_engine(TEST_DATABASE_URL)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def clean_tables():
    with test_engine.connect() as conn:
        conn.execute(text(
            "TRUNCATE TABLE refresh_tokens, users, role_page_permission, roles, companies, "
            "failed_webhooks "
            "RESTART IDENTITY CASCADE"
        ))
        conn.commit()
    yield


@pytest.fixture()
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ── Low-level helpers ──────────────────────────────────────────────────────

def make_role(
    db,
    name: str = "tester",
    display_name: str = "Tester",
    pages: list[str] | None = None,
    actions: list[str] | None = None,
) -> Role:
    """
    Create a Role and optionally seed permission rows.
    pages=None → no permission rows.
    pages=['companies','users'] with actions=['read','create'] → rows for those pages.
    """
    role = Role(name=name, display_name=display_name, is_system=(name == "super_admin"))
    db.add(role)
    db.flush()

    if pages:
        acts = set(actions or ["read"])
        for page in pages:
            db.add(RolePagePermission(
                role_id=role.id,
                page_name=page,
                can_read="read" in acts,
                can_create="create" in acts,
                can_write="write" in acts,
                can_delete="delete" in acts,
            ))
    db.commit()
    db.refresh(role)
    return role


def make_user(
    db,
    username: str = "testuser",
    password: str = "testpass123",
    role_name: str | None = None,
    is_active: bool = True,
    company_id=None,
) -> User:
    role_id = None
    if role_name:
        existing = db.query(Role).filter(Role.name == role_name).first()
        role_id = existing.id if existing else make_role(db, name=role_name, display_name=role_name.replace("_", " ").title()).id

    user = User(
        username=username,
        full_name=username.replace("_", " ").title(),
        hashed_password=hash_password(password),
        role_id=role_id,
        company_id=company_id,
        is_active=is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login(client, username="testuser", password="testpass123") -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()


# ── Messaging data helpers ─────────────────────────────────────────────────

def make_company(db, name: str = "Test Corp", code: str = "TESTCORP") -> Company:
    c = Company(name=name, company_code=code)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_wa_account(
    db, company_id, phone_number_id: str = "1234567890",
    access_token_encrypted: str = "dummy-encrypted-token",
) -> WhatsAppAccount:
    a = WhatsAppAccount(
        company_id=company_id,
        phone_number_id=phone_number_id,
        access_token_encrypted=access_token_encrypted,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def make_wa_template(
    db, company_id, name: str = "payment_receipt",
    status: str = "APPROVED",
) -> WhatsAppTemplate:
    t = WhatsAppTemplate(
        company_id=company_id,
        waba_id="test-waba",
        name=name,
        category="UTILITY",
        language="en_US",
        status=status,
        components=[],
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def make_api_key(db, company_id, key: str = "test-api-key-12345") -> CompanyApiKey:
    k = CompanyApiKey(company_id=company_id, api_key=key, label="test")
    db.add(k)
    db.commit()
    db.refresh(k)
    return k


def make_conversation(db, company_id, mobile_no: str = "919876543210") -> Conversation:
    c = Conversation(company_id=company_id, mobile_no=mobile_no)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def make_service(
    db, conversation_id, company_id,
    service_id: str | None = None,
    status: str = "in_progress",
    questions=None,
    created_at=None,
    template_expiry_hours: int = 24,
    mobile_no: str = "919876543210",
) -> Service:
    svc = Service(
        conversation_id=conversation_id,
        company_id=company_id,
        service_id=service_id or str(_uuid.uuid4()),
        status=status,
        questions=questions,
        data={"customer_mobile": mobile_no},
        template_expiry_hours=template_expiry_hours,
    )
    if created_at is not None:
        svc.created_at = created_at
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def make_queue_entry(
    db, service: Service,
    mobile_no: str = "919876543210",
    status: str = "in_progress",
) -> MobileQueue:
    mq = MobileQueue(
        company_id=service.company_id,
        mobile_no=mobile_no,
        service_id=service.id,
        position=1,
        status=status,
    )
    db.add(mq)
    db.commit()
    db.refresh(mq)
    return mq


# ── Fixture shortcuts ──────────────────────────────────────────────────────

@pytest.fixture()
def sa(client, db):
    """super_admin user + access token — bypasses all permission checks."""
    make_user(db, username="sa", role_name="super_admin")
    return login(client, "sa")["access_token"]


@pytest.fixture()
def admin_full(client, db):
    """admin user with all permissions on all pages."""
    make_role(db, name="admin", display_name="Admin",
              pages=ALL_PAGE_NAMES, actions=["read", "create", "write", "delete"])
    make_user(db, username="admin", role_name="admin")
    return login(client, "admin")["access_token"]


@pytest.fixture()
def viewer(client, db):
    """company_viewer with only dashboard+reports read."""
    make_role(db, name="company_viewer", display_name="Viewer",
              pages=["dashboard", "reports"], actions=["read"])
    make_user(db, username="viewer", role_name="company_viewer")
    return login(client, "viewer")["access_token"]


@pytest.fixture()
def company(client, sa):
    """A seeded company, returned as dict."""
    r = client.post(
        "/api/companies/",
        json={"name": "Test Corp", "company_code": "TESTCORP"},
        headers={"Authorization": f"Bearer {sa}"},
    )
    assert r.status_code == 201
    return r.json()
