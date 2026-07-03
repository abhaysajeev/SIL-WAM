from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.resources import ALL_PAGE_NAMES
from app.core.security import decode_access_token

bearer = HTTPBearer()

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)
_403_disabled = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Account is disabled",
)
_403_locked = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Account is temporarily locked",
)


# ── Shared user loader ─────────────────────────────────────────────────────────

def _load_user_row(user_id: str, db: Session):
    """Load minimal user + role info needed for both API and page deps."""
    return db.execute(
        text("""
            SELECT u.id, u.username, u.full_name, u.is_active, u.must_change_password,
                   u.company_id, u.locked_until,
                   r.id   AS role_id,
                   r.name AS role_name
            FROM users u
            LEFT JOIN roles r ON r.id = u.role_id
            WHERE u.id = :id
        """),
        {"id": user_id},
    ).fetchone()


def _check_locked(row) -> None:
    if row.locked_until:
        locked_until = row.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > datetime.now(timezone.utc):
            raise _403_locked


# ── API dependency: Bearer JWT ─────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
):
    """Used by all /api/* routes. Returns minimal user row."""
    user_id = decode_access_token(credentials.credentials)
    if not user_id:
        raise _401

    row = _load_user_row(user_id, db)
    if not row:
        raise _401
    if not row.is_active:
        raise _403_disabled
    _check_locked(row)

    return row


def require(page: str, action: str = "read"):
    """
    Route guard factory for API routes.
    Usage: Depends(require("companies", "create"))

    super_admin bypasses all checks.
    Others must have the explicit permission row.
    """
    def guard(
        user=Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        if user.role_name == "super_admin":
            return user

        # No role assigned → no access
        if user.role_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No role assigned",
            )

        perm = db.execute(
            text("""
                SELECT can_read, can_create, can_write, can_delete
                FROM role_page_permission
                WHERE role_id = :role_id AND page_name = :page
            """),
            {"role_id": str(user.role_id), "page": page},
        ).fetchone()

        if not perm or not getattr(perm, f"can_{action}", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {page}.{action}",
            )
        return user

    return guard


def require_super_admin(user=Depends(get_current_user)):
    """Strict guard — super_admin only."""
    if user.role_name != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin only")
    return user


# ── Page dependency: session cookie ───────────────────────────────────────────

def _build_perms_dict(role_id: Optional[UUID], role_name: str, db: Session) -> dict:
    """
    Build the permissions dict injected into every Jinja2 template.
    super_admin gets all permissions set to True.
    Others get their DB rows, defaulting to False for unset pages.
    """
    # initialise all pages to all-False
    perms: dict[str, dict[str, bool]] = {
        page: {"read": False, "create": False, "write": False, "delete": False}
        for page in ALL_PAGE_NAMES
    }

    if role_name == "super_admin":
        for page in perms:
            perms[page] = {"read": True, "create": True, "write": True, "delete": True}
        return perms

    if role_id is None:
        return perms  # no role → no access

    rows = db.execute(
        text("""
            SELECT page_name, can_read, can_create, can_write, can_delete
            FROM role_page_permission
            WHERE role_id = :role_id
        """),
        {"role_id": str(role_id)},
    ).fetchall()

    for row in rows:
        if row.page_name in perms:
            perms[row.page_name] = {
                "read":   row.can_read,
                "create": row.can_create,
                "write":  row.can_write,
                "delete": row.can_delete,
            }
    return perms


class _LoginRedirect(Exception):
    """Raised by get_page_user when the session is missing or invalid."""


def get_page_user(request: Request, db: Session = Depends(get_db)):
    """
    Used by SSR page routes. Reads the session cookie.
    Returns a dict with keys: user, perms.
    Raises _LoginRedirect if not authenticated (caught by app exception handler).
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise _LoginRedirect()

    row = _load_user_row(user_id, db)
    if not row or not row.is_active:
        request.session.clear()
        raise _LoginRedirect()

    perms = _build_perms_dict(row.role_id, row.role_name or "", db)

    return {
        "user": {
            "id":                   str(row.id),
            "username":             row.username,
            "full_name":            row.full_name,
            "role_name":            row.role_name or "",
            "company_id":           str(row.company_id) if row.company_id else None,
            "must_change_password": row.must_change_password,
        },
        "perms": perms,
    }


# ── Company scoping helper ─────────────────────────────────────────────────────

def company_filter(user_row) -> Optional[str]:
    """
    Returns None if user sees all companies (admin tier),
    or the company UUID string to use in WHERE company_id = :cid.
    """
    if user_row.company_id is None:
        return None
    return str(user_row.company_id)


# ── X-API-Key dependency for /client-api/v1/* routes ──────────────────────────

def get_api_company(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
):
    """
    Authenticates external client calls (e.g. from .NET SFA).
    Returns the Company row the key belongs to.
    """
    from app.models.api_key import CompanyApiKey
    from app.models.company import Company

    company = (
        db.query(Company)
        .join(CompanyApiKey, CompanyApiKey.company_id == Company.id)
        .filter(
            CompanyApiKey.api_key == x_api_key,
            CompanyApiKey.is_active.is_(True),
        )
        .first()
    )
    if not company:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return company


def get_api_key_and_company(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
):
    """
    Like get_api_company, but also returns the specific CompanyApiKey row used —
    needed to record which key ingested a Service (a company can have multiple
    active keys, each with its own notify_url).
    """
    from app.models.api_key import CompanyApiKey
    from app.models.company import Company

    row = (
        db.query(CompanyApiKey, Company)
        .join(Company, CompanyApiKey.company_id == Company.id)
        .filter(
            CompanyApiKey.api_key == x_api_key,
            CompanyApiKey.is_active.is_(True),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return row  # (CompanyApiKey, Company)
