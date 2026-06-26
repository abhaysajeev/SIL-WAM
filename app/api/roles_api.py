import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require, require_super_admin
from app.core.resources import VALID_ACTIONS
from app.models.role import Role, RolePagePermission
from app.schemas.role import PermissionMatrixUpdate, RoleCreate, RoleOut, RoleUpdate

router = APIRouter(
    prefix="/api/roles",
    tags=["Roles"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("/", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db)):
    return db.query(Role).order_by(Role.name).all()


@router.get("/{role_id}", response_model=RoleOut)
def get_role(role_id: uuid.UUID, db: Session = Depends(get_db)):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.post("/", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(payload: RoleCreate, db: Session = Depends(get_db)):
    if db.query(Role).filter(Role.name == payload.name).first():
        raise HTTPException(status_code=409, detail="Role name already exists")
    role = Role(**payload.model_dump())
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@router.put("/{role_id}", response_model=RoleOut)
def update_role(role_id: uuid.UUID, payload: RoleUpdate, db: Session = Depends(get_db)):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(status_code=403, detail="System roles cannot be modified")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(role, field, value)
    db.commit()
    db.refresh(role)
    return role


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(role_id: uuid.UUID, db: Session = Depends(get_db)):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(status_code=403, detail="System roles cannot be deleted")
    db.delete(role)
    db.commit()


@router.get("/{role_id}/permissions")
def get_permissions(role_id: uuid.UUID, db: Session = Depends(get_db)):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    rows = db.query(RolePagePermission).filter(
        RolePagePermission.role_id == role_id
    ).all()
    return {
        "role": {"id": str(role.id), "name": role.name, "display_name": role.display_name},
        "permissions": [
            {
                "page_name":  r.page_name,
                "can_read":   r.can_read,
                "can_create": r.can_create,
                "can_write":  r.can_write,
                "can_delete": r.can_delete,
            }
            for r in rows
        ],
    }


@router.put("/{role_id}/permissions", status_code=status.HTTP_204_NO_CONTENT)
def update_permissions(
    role_id: uuid.UUID,
    payload: PermissionMatrixUpdate,
    db: Session = Depends(get_db),
):
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    # super_admin permissions are hardcoded in code — editing them is meaningless
    if role.name == "super_admin":
        raise HTTPException(status_code=403, detail="super_admin permissions are managed by the application")

    # Validate page names and actions against known resources
    for perm in payload.permissions:
        if perm.page_name not in VALID_ACTIONS:
            raise HTTPException(status_code=422, detail=f"Unknown page: {perm.page_name}")
        allowed = VALID_ACTIONS[perm.page_name]
        if perm.can_create and "create" not in allowed:
            raise HTTPException(status_code=422, detail=f"{perm.page_name} does not support create")
        if perm.can_write and "write" not in allowed:
            raise HTTPException(status_code=422, detail=f"{perm.page_name} does not support write")
        if perm.can_delete and "delete" not in allowed:
            raise HTTPException(status_code=422, detail=f"{perm.page_name} does not support delete")
        # Enforce: any write action implies read
        if perm.can_create or perm.can_write or perm.can_delete:
            perm.can_read = True

    # Replace all permissions for this role atomically
    db.query(RolePagePermission).filter(
        RolePagePermission.role_id == role_id
    ).delete()

    for perm in payload.permissions:
        # Skip all-False rows — missing row and all-False row are treated identically by require()
        if not any([perm.can_read, perm.can_create, perm.can_write, perm.can_delete]):
            continue
        db.add(RolePagePermission(
            role_id=role_id,
            page_name=perm.page_name,
            can_read=perm.can_read,
            can_create=perm.can_create,
            can_write=perm.can_write,
            can_delete=perm.can_delete,
        ))

    db.commit()
