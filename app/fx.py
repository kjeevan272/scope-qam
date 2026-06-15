"""
FX rate management for cross-currency peer benchmarking.

In production, rates are fetched from the ECB Statistical Data Warehouse
(https://sdw-wsrest.ecb.europa.eu/) or any other authoritative source.
For testing and demo, a `seed_default_rates()` helper inserts representative
rates so multi-currency comparisons work out-of-the-box.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session


# Indicative anchor rates (vs USD) for demo / test seeding
DEFAULT_RATES_USD = {
    "EUR": Decimal("1.08"),
    "CHF": Decimal("1.12"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("0.0065"),
    "AUD": Decimal("0.66"),
    "CAD": Decimal("0.73"),
    "USD": Decimal("1.00"),
}


def seed_default_rates(db: Session, rate_date: Optional[date] = None) -> int:
    """Seed FX rates for testing / first-run. Returns rows inserted."""
    from app.models import FXRate

    rate_date = rate_date or date.today()
    inserted = 0
    for ccy, rate in DEFAULT_RATES_USD.items():
        existing = (
            db.query(FXRate)
            .filter_by(from_ccy=ccy, to_ccy="USD", rate_date=rate_date)
            .first()
        )
        if existing:
            continue
        db.add(FXRate(
            from_ccy=ccy, to_ccy="USD",
            rate_date=rate_date, rate=rate, source="seed",
        ))
        inserted += 1
    db.commit()
    return inserted


def latest_rate(db: Session, from_ccy: str, to_ccy: str = "USD") -> Optional[Decimal]:
    from app.models import FXRate

    if from_ccy == to_ccy:
        return Decimal("1")

    row = (
        db.query(FXRate)
        .filter_by(from_ccy=from_ccy, to_ccy=to_ccy)
        .order_by(FXRate.rate_date.desc())
        .first()
    )
    return row.rate if row else None


def normalise_to_usd(db: Session, amount: float | Decimal, from_ccy: str) -> Optional[float]:
    """Convert an amount in `from_ccy` to USD using the latest available rate."""
    rate = latest_rate(db, from_ccy, "USD")
    if rate is None:
        return None
    return float(Decimal(str(amount)) * rate)
