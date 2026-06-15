from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Company, CompanyChangeLog, Snapshot
from app.schemas import CompanyChangeLogOut, CompanyOut, CompareOut, SnapshotOut

router = APIRouter(prefix="/companies", tags=["Companies"])


@router.get("", response_model=list[CompanyOut])
def list_companies(db: Session = Depends(get_db)):
    """All companies at their current (latest) SCD version."""
    return db.query(Company).filter_by(is_current=True).all()


@router.get("/compare", response_model=CompareOut)
def compare_companies(
    company_ids: str = Query(..., description="Comma-separated company IDs"),
    as_of_date: datetime | None = Query(None, description="ISO 8601 point-in-time"),
    db: Session = Depends(get_db),
):
    """Point-in-time comparison: latest snapshot per company at or before as_of_date."""
    ids = [int(i.strip()) for i in company_ids.split(",")]
    cutoff = as_of_date or datetime.now(timezone.utc)

    snapshots = []
    for company_id in ids:
        snap = (
            db.query(Snapshot)
            .options(selectinload(Snapshot.credit_metrics))
            .filter(
                Snapshot.company_id == company_id,
                Snapshot.snapshot_at <= cutoff,
            )
            .order_by(Snapshot.snapshot_at.desc())
            .first()
        )
        if snap:
            snapshots.append(snap)

    return CompareOut(as_of_date=cutoff, companies=snapshots)


@router.get("/{company_id}", response_model=CompanyOut)
def get_company(company_id: int, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")
    return company


@router.get("/{company_id}/versions", response_model=list[CompanyOut])
def company_versions(company_id: int, db: Session = Depends(get_db)):
    """All SCD Type 2 versions for a company, oldest first."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")
    return (
        db.query(Company)
        .filter_by(company_name=company.company_name)
        .order_by(Company.version)
        .all()
    )


@router.get("/{company_id}/changelog", response_model=list[CompanyChangeLogOut])
def company_changelog(company_id: int, db: Session = Depends(get_db)):
    """Field-level audit trail: every metadata change and which upload caused it."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")
    return (
        db.query(CompanyChangeLog)
        .filter_by(company_name=company.company_name)
        .order_by(CompanyChangeLog.changed_at)
        .all()
    )


@router.get("/{company_id}/history", response_model=list[SnapshotOut])
def company_history(company_id: int, db: Session = Depends(get_db)):
    """All snapshots across all SCD versions — full time-series for trend analysis."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")

    all_ids = [
        r.id
        for r in db.query(Company.id)
        .filter_by(company_name=company.company_name)
        .all()
    ]

    return (
        db.query(Snapshot)
        .options(selectinload(Snapshot.credit_metrics))
        .filter(Snapshot.company_id.in_(all_ids))
        .order_by(Snapshot.snapshot_at)
        .all()
    )
