import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import company_filter, require
from app.models.company import Company
from app.schemas.company import CompanyCreate, CompanyOut, CompanyUpdate

router = APIRouter(
    prefix="/api/companies",
    tags=["Companies"],
    dependencies=[Depends(require("companies", "read"))],
)


@router.get("/", response_model=list[CompanyOut])
def list_companies(
    db: Session = Depends(get_db),
    user=Depends(require("companies", "read")),
):
    cid = company_filter(user)
    q = db.query(Company)
    if cid:
        q = q.filter(Company.id == cid)
    return q.order_by(Company.name).all()


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    user=Depends(require("companies", "read")),
):
    cid = company_filter(user)
    if cid and str(company_id) != cid:
        raise HTTPException(status_code=403, detail="Access denied")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.post("/", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
def create_company(
    payload: CompanyCreate,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "create")),
):
    if db.query(Company).filter(Company.company_code == payload.company_code).first():
        raise HTTPException(status_code=409, detail="Company code already exists")
    company = Company(**payload.model_dump())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.put("/{company_id}", response_model=CompanyOut)
def update_company(
    company_id: uuid.UUID,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "write")),
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(company, field, value)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(require("companies", "delete")),
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    db.delete(company)
    db.commit()
