"""
Regulatory and analytical export endpoints.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.exports import snapshot_to_esma_xml, snapshots_to_csv
from app.models import Company, Snapshot

router = APIRouter(prefix="/exports", tags=["Exports"])


@router.get("/esma-xml/{snapshot_id}")
def esma_xml_export(snapshot_id: int, db: Session = Depends(get_db)):
    """
    ESMA CEREP-style XML for a rating snapshot.
    Use as a starting point for ESMA Article 11 reporting.
    """
    snap = (
        db.query(Snapshot)
        .options(
            selectinload(Snapshot.credit_metrics),
            selectinload(Snapshot.company),
            selectinload(Snapshot.upload),
        )
        .filter(Snapshot.id == snapshot_id)
        .first()
    )
    if not snap:
        raise HTTPException(404, f"Snapshot {snapshot_id} not found")

    xml_bytes = snapshot_to_esma_xml(snap, snap.company, snap.credit_metrics)
    return Response(
        content=xml_bytes,
        media_type="application/xml",
        headers={
            "Content-Disposition":
                f'attachment; filename="esma_snapshot_{snapshot_id}.xml"'
        },
    )


@router.get("/snapshots-csv")
def snapshots_csv_export(
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    sector: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Flat CSV of snapshots in window — for analyst Excel workflows."""
    q = (
        db.query(Snapshot)
        .options(selectinload(Snapshot.company), selectinload(Snapshot.upload))
    )
    if from_date:
        q = q.filter(Snapshot.snapshot_at >= from_date)
    if to_date:
        q = q.filter(Snapshot.snapshot_at <= to_date)
    if sector:
        q = q.join(Company, Snapshot.company_id == Company.id).filter(
            Company.sector.ilike(f"%{sector}%")
        )

    snaps = q.order_by(Snapshot.snapshot_at.desc()).all()
    csv_str = snapshots_to_csv(snaps)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="snapshots.csv"'},
    )
