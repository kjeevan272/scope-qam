from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Company, Snapshot
from app.schemas import SnapshotDetailOut, SnapshotOut

router = APIRouter(prefix="/snapshots", tags=["Snapshots"])


@router.get("", response_model=list[SnapshotOut])
def list_snapshots(
    company_id: int | None = Query(None),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    sector: str | None = Query(None),
    country: str | None = Query(None),
    currency: str | None = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(Snapshot).options(selectinload(Snapshot.credit_metrics))

    if company_id is not None:
        q = q.filter(Snapshot.company_id == company_id)
    if from_date:
        q = q.filter(Snapshot.snapshot_at >= from_date)
    if to_date:
        q = q.filter(Snapshot.snapshot_at <= to_date)

    if sector or country or currency:
        q = q.join(Company, Snapshot.company_id == Company.id)
        if sector:
            q = q.filter(Company.sector.ilike(f"%{sector}%"))
        if country:
            q = q.filter(Company.country.ilike(f"%{country}%"))
        if currency:
            q = q.filter(Company.currency == currency)

    return q.order_by(Snapshot.snapshot_at.desc()).all()


@router.get("/latest", response_model=list[SnapshotOut])
def latest_snapshots(db: Session = Depends(get_db)):
    """Latest snapshot per company — for BI dashboards."""
    latest_ids = (
        db.query(func.max(Snapshot.id).label("id"))
        .group_by(Snapshot.company_id)
        .subquery()
    )
    return (
        db.query(Snapshot)
        .options(selectinload(Snapshot.credit_metrics))
        .join(latest_ids, Snapshot.id == latest_ids.c.id)
        .all()
    )


@router.get("/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(snapshot_id: int, db: Session = Depends(get_db)):
    snap = (
        db.query(Snapshot)
        .options(selectinload(Snapshot.credit_metrics))
        .filter(Snapshot.id == snapshot_id)
        .first()
    )
    if not snap:
        raise HTTPException(404, f"Snapshot {snapshot_id} not found")
    return snap


@router.get("/{snapshot_id}/provenance", response_model=SnapshotDetailOut)
def snapshot_provenance(snapshot_id: int, db: Session = Depends(get_db)):
    """Full cell-level lineage: every field traced to its exact Excel cell."""
    snap = (
        db.query(Snapshot)
        .options(
            selectinload(Snapshot.credit_metrics),
            selectinload(Snapshot.provenance),
        )
        .filter(Snapshot.id == snapshot_id)
        .first()
    )
    if not snap:
        raise HTTPException(404, f"Snapshot {snapshot_id} not found")
    return snap
