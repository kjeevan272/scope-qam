"""
FX rates: management and lookup.
Used for cross-currency peer benchmarking and reporting.
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.fx import latest_rate, seed_default_rates
from app.models import FXRate
from app.security import require_role

router = APIRouter(prefix="/fx", tags=["FX"])


class FXRateCreate(BaseModel):
    from_ccy: str
    to_ccy: str = "USD"
    rate_date: date
    rate: float
    source: str = "manual"


@router.get("")
def list_rates(
    from_ccy: str | None = None,
    to_ccy: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(FXRate)
    if from_ccy:
        q = q.filter_by(from_ccy=from_ccy)
    if to_ccy:
        q = q.filter_by(to_ccy=to_ccy)
    rows = q.order_by(FXRate.rate_date.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "from_ccy": r.from_ccy,
            "to_ccy": r.to_ccy,
            "rate_date": r.rate_date.isoformat(),
            "rate": float(r.rate),
            "source": r.source,
        }
        for r in rows
    ]


@router.get("/latest")
def latest(from_ccy: str, to_ccy: str = "USD", db: Session = Depends(get_db)):
    rate = latest_rate(db, from_ccy, to_ccy)
    if rate is None:
        raise HTTPException(404, f"No rate available for {from_ccy}->{to_ccy}")
    return {"from_ccy": from_ccy, "to_ccy": to_ccy, "rate": float(rate)}


@router.post("", status_code=201)
def add_rate(body: FXRateCreate, db: Session = Depends(get_db),
              _=Depends(require_role("admin"))):
    db.add(FXRate(
        from_ccy=body.from_ccy, to_ccy=body.to_ccy,
        rate_date=body.rate_date, rate=body.rate, source=body.source,
    ))
    db.commit()
    return {"status": "ok"}


@router.post("/seed-defaults")
def seed(db: Session = Depends(get_db), _=Depends(require_role("admin"))):
    """Seed indicative anchor rates for EUR/CHF/GBP/etc. vs USD."""
    inserted = seed_default_rates(db)
    return {"inserted": inserted}
