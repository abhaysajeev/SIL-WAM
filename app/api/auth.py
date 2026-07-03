import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import _build_perms_dict, get_current_user
from app.core.security import (
    create_access_token,
    dummy_verify,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.core.config import settings
from app.models.user import RefreshToken, User
from app.schemas.auth import (
    AccessTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["Auth"])

_401 = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
_403_disabled = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")


@router.post("/login", response_model=TokenResponse)
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()

    # Reject locked accounts before spending bcrypt cycles
    if user and user.locked_until:
        locked_until = user.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is temporarily locked. Try again later.",
            )

    # Always run bcrypt to prevent username enumeration via timing.
    # Short-circuit means verify_password is never called when user is None.
    if not user or not verify_password(payload.password, user.hashed_password):
        if not user:
            dummy_verify()
        else:
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= settings.MAX_LOGIN_ATTEMPTS:
                user.locked_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=settings.LOCKOUT_MINUTES)
                )
            db.commit()
        raise _401

    if not user.is_active:
        raise _403_disabled

    # Successful login — reset lockout state
    user.last_login_at = datetime.now(timezone.utc)
    user.failed_login_attempts = 0
    user.locked_until = None

    # Purge this user's dead tokens — cheap, scoped to one user_id
    db.execute(
        text("DELETE FROM refresh_tokens WHERE user_id = :uid AND (revoked = TRUE OR expires_at < now())"),
        {"uid": str(user.id)},
    )

    access_token = create_access_token(str(user.id))
    raw_refresh = generate_refresh_token()

    db.add(RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    ))
    db.commit()

    # Write session so SSR page routes can identify the user without a Bearer token
    request.session["user_id"] = str(user.id)

    return TokenResponse(access_token=access_token, refresh_token=raw_refresh)


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    token_hash = hash_refresh_token(payload.refresh_token)

    # Atomic revocation: UPDATE returns the row only if revoked=False at the moment of the write.
    # PostgreSQL locks the row during the UPDATE, so a second simultaneous request waits,
    # then finds revoked=True and gets 0 rows back → 401. No two requests can both win.
    record = db.execute(
        text("""
            UPDATE refresh_tokens
            SET revoked = TRUE
            WHERE token_hash = :hash AND revoked = FALSE
            RETURNING user_id, expires_at
        """),
        {"hash": token_hash},
    ).fetchone()

    if not record:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

    user_row = db.execute(
        text("SELECT is_active FROM users WHERE id = :id"),
        {"id": str(record.user_id)},
    ).fetchone()
    if not user_row or not user_row.is_active:
        raise _403_disabled

    raw_new = generate_refresh_token()
    db.add(RefreshToken(
        user_id=record.user_id,
        token_hash=hash_refresh_token(raw_new),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    ))

    # 1-in-100 chance: sweep the entire table for dead tokens across all users
    if random.randint(1, 100) == 1:
        db.execute(text("DELETE FROM refresh_tokens WHERE revoked = TRUE OR expires_at < now()"))

    db.commit()

    return AccessTokenResponse(
        access_token=create_access_token(str(record.user_id)),
        refresh_token=raw_new,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, payload: RefreshRequest, db: Session = Depends(get_db)):
    token_hash = hash_refresh_token(payload.refresh_token)
    record = db.query(RefreshToken).filter(
        RefreshToken.token_hash == token_hash,
        RefreshToken.revoked == False,
    ).first()
    if record:
        record.revoked = True
        db.commit()
    request.session.clear()


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter(User.id == user.id).first()
    if not db_user or not verify_password(payload.current_password, db_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    db_user.hashed_password = hash_password(payload.new_password)
    db_user.must_change_password = False
    # Revoke all refresh tokens so other sessions can't persist after a password change
    db.execute(text("DELETE FROM refresh_tokens WHERE user_id = :uid"), {"uid": str(user.id)})
    db.commit()


@router.get("/me", response_model=MeResponse)
def me(user=Depends(get_current_user), db: Session = Depends(get_db)):
    full_row = db.execute(
        text("""
            SELECT u.id, u.username, u.full_name, u.company_id,
                   r.name AS role_name, r.id AS role_id
            FROM users u
            LEFT JOIN roles r ON r.id = u.role_id
            WHERE u.id = :id
        """),
        {"id": str(user.id)},
    ).fetchone()

    perms = _build_perms_dict(
        full_row.role_id if full_row else None,
        full_row.role_name or "" if full_row else "",
        db,
    )

    return MeResponse(
        id=full_row.id,
        username=full_row.username,
        full_name=full_row.full_name,
        role_name=full_row.role_name,
        company_id=full_row.company_id,
        permissions=perms,
    )
