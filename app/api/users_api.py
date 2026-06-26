import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require
from app.core.security import hash_password
from app.models.user import User
from app.schemas.user_schema import UserCreate, UserOut, UserPasswordChange, UserUpdate

# Fixed UUID seeded in migration a1b2c3d4e5f6
_SUPER_ADMIN_ROLE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

router = APIRouter(
    prefix="/api/users",
    tags=["Users"],
    dependencies=[Depends(require("users", "read"))],
)


@router.get("/", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    query = db.query(User).filter(
        (User.role_id != _SUPER_ADMIN_ROLE_ID) | (User.role_id.is_(None))
    )
    # admin with company_id scoping: show only users of same company
    if current_user.role_name != "super_admin" and current_user.company_id:
        query = query.filter(User.company_id == current_user.company_id)
    return query.order_by(User.username).all()


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: uuid.UUID, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require("users", "create")),
):
    if payload.role_id == _SUPER_ADMIN_ROLE_ID:
        raise HTTPException(status_code=403, detail="super_admin users can only be created via CLI")

    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    data = payload.model_dump(exclude={"password"})
    data["hashed_password"] = hash_password(payload.password)
    data["created_by_id"] = str(current_user.id)

    user = User(**data)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require("users", "write")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Block editing super_admin users or assigning super_admin role
    if user.role_id == _SUPER_ADMIN_ROLE_ID:
        raise HTTPException(status_code=403, detail="super_admin users cannot be edited via UI")
    if payload.role_id == _SUPER_ADMIN_ROLE_ID:
        raise HTTPException(status_code=403, detail="super_admin role can only be assigned via CLI")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@router.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def change_user_password(
    user_id: uuid.UUID,
    payload: UserPasswordChange,
    db: Session = Depends(get_db),
    _user=Depends(require("users", "write")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.hashed_password = hash_password(payload.new_password)
    user.must_change_password = False
    db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(require("users", "delete")),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role_id == _SUPER_ADMIN_ROLE_ID:
        raise HTTPException(status_code=403, detail="super_admin users cannot be deleted via UI")
    db.delete(user)
    db.commit()
