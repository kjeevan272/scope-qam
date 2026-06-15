"""
Business analytics endpoints:
  - Rating migration matrix (regulatory deliverable)
  - Sector benchmarking with percentiles
  - Per-company metric trend with cliff detection
  - Methodology change & weight drift history
"""
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, selectinload

from app.analytics import classify_trend, percentile
from app.db import get_db
from app.models import (
    Company, CreditMetric, DataQualityIssue, RatingMigration,
    Snapshot, Upload,
)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


# ── Rating Migration Matrix ───────────────────────────────────────────────────

@router.get("/migration-matrix")
def migration_matrix(
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
):
    """
    Aggregate all RatingMigration rows in window into a transition matrix.
    Output format matches ESMA/Basel-style migration matrices.
    """
    q = db.query(RatingMigration)
    if from_date:
        q = q.filter(RatingMigration.migrated_at >= from_date)
    if to_date:
        q = q.filter(RatingMigration.migrated_at <= to_date)

    migrations = q.all()

    # Build cell counts: matrix[from_rating][to_rating] = count
    matrix: dict[str, dict[str, int]] = {}
    row_totals: dict[str, int] = {}
    for m in migrations:
        from_r = m.from_rating or "NEW"
        to_r = m.to_rating
        matrix.setdefault(from_r, {})
        matrix[from_r][to_r] = matrix[from_r].get(to_r, 0) + 1
        row_totals[from_r] = row_totals.get(from_r, 0) + 1

    # Convert counts to percentages within each from-rating row
    percentage_matrix = {
        from_r: {
            to_r: round((count / row_totals[from_r]) * 100, 2)
            for to_r, count in cells.items()
        }
        for from_r, cells in matrix.items()
    }

    return {
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "total_migrations": len(migrations),
        "upgrades": sum(1 for m in migrations if m.direction == "upgrade"),
        "downgrades": sum(1 for m in migrations if m.direction == "downgrade"),
        "affirmations": sum(1 for m in migrations if m.direction == "affirmation"),
        "new_ratings": sum(1 for m in migrations if m.direction == "new"),
        "count_matrix": matrix,
        "percentage_matrix": percentage_matrix,
    }


@router.get("/migrations")
def list_migrations(
    company_name: str | None = Query(None),
    direction: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List individual rating migration events."""
    q = db.query(RatingMigration)
    if company_name:
        q = q.filter(RatingMigration.company_name == company_name)
    if direction:
        q = q.filter(RatingMigration.direction == direction)
    rows = q.order_by(RatingMigration.migrated_at.desc()).limit(500).all()
    return [
        {
            "id": r.id,
            "company_name": r.company_name,
            "from_rating": r.from_rating,
            "to_rating": r.to_rating,
            "notches_moved": r.notches_moved,
            "direction": r.direction,
            "migrated_at": r.migrated_at.isoformat() if r.migrated_at else None,
        }
        for r in rows
    ]


# ── Sector Benchmark ──────────────────────────────────────────────────────────

VALID_METRICS = {
    "ebitda_interest_cover", "debt_ebitda", "ffo_debt",
    "loan_value", "focf_debt", "liquidity",
}


@router.get("/sector-benchmark")
def sector_benchmark(
    sector: str = Query(..., description="Sector name (e.g. 'Automobiles & Parts')"),
    metric: str = Query("debt_ebitda"),
    metric_year: int | None = Query(None, description="Year (default: latest available)"),
    db: Session = Depends(get_db),
):
    """
    Median / p25 / p75 of a credit metric across the sector.
    Returns each company's value and its percentile rank.
    """
    if metric not in VALID_METRICS:
        raise HTTPException(422, f"metric must be one of {sorted(VALID_METRICS)}")

    # Latest snapshot per company in sector
    latest_ids = (
        db.query(func.max(Snapshot.id).label("id"))
        .join(Company, Snapshot.company_id == Company.id)
        .filter(Company.sector.ilike(f"%{sector}%"))
        .group_by(Snapshot.company_id)
        .subquery()
    )

    snaps = (
        db.query(Snapshot)
        .options(selectinload(Snapshot.credit_metrics), selectinload(Snapshot.company))
        .join(latest_ids, Snapshot.id == latest_ids.c.id)
        .all()
    )

    if not snaps:
        raise HTTPException(404, f"No companies found for sector '{sector}'")

    company_values = []
    for s in snaps:
        # Choose target year: requested, or latest available, or skip
        candidates = sorted(s.credit_metrics, key=lambda m: m.metric_year, reverse=True)
        target = None
        if metric_year:
            target = next((m for m in s.credit_metrics if m.metric_year == metric_year), None)
        else:
            target = candidates[0] if candidates else None
        if target is None:
            continue
        val = getattr(target, metric)
        if val is None:
            continue
        company_values.append({
            "company_id": s.company_id,
            "company_name": s.company.company_name if s.company else "",
            "metric_year": target.metric_year,
            "value": float(val),
        })

    values = [c["value"] for c in company_values]
    p25 = percentile(values, 25)
    p50 = percentile(values, 50)
    p75 = percentile(values, 75)

    # Assign percentile rank to each company
    for c in company_values:
        rank = sum(1 for v in values if v <= c["value"])
        c["percentile_rank"] = round((rank / len(values)) * 100, 1) if values else None

    return {
        "sector": sector,
        "metric": metric,
        "metric_year": metric_year,
        "sample_size": len(values),
        "p25": p25, "median": p50, "p75": p75,
        "companies": sorted(company_values, key=lambda c: c["value"]),
    }


# ── Trend & Cliff Analysis ────────────────────────────────────────────────────

@router.get("/trend/{company_id}")
def metric_trend(
    company_id: int,
    metric: str = Query("ebitda_interest_cover"),
    db: Session = Depends(get_db),
):
    """
    Year-over-year time series for a single metric with cliff detection.
    """
    if metric not in VALID_METRICS:
        raise HTTPException(422, f"metric must be one of {sorted(VALID_METRICS)}")

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")

    # All historical company versions (SCD)
    related_ids = [
        r.id for r in db.query(Company.id)
        .filter_by(company_name=company.company_name).all()
    ]
    metrics_rows = (
        db.query(CreditMetric)
        .join(Snapshot, Snapshot.id == CreditMetric.snapshot_id)
        .filter(Snapshot.company_id.in_(related_ids))
        .order_by(CreditMetric.metric_year)
        .all()
    )
    # Latest value per year (most recent snapshot wins)
    by_year: dict[int, dict[str, Any]] = {}
    for m in metrics_rows:
        val = getattr(m, metric)
        if val is None:
            continue
        by_year[m.metric_year] = {
            "year": m.metric_year,
            "value": float(val),
            "is_estimate": bool(m.is_estimate),
            "is_stale": bool(m.is_stale),
        }

    series = sorted(by_year.values(), key=lambda r: r["year"])
    # YoY changes & cliffs
    cliffs: list[dict] = []
    for i in range(1, len(series)):
        prev_v = series[i - 1]["value"]
        curr_v = series[i]["value"]
        if prev_v == 0:
            continue
        delta_pct = round(((curr_v - prev_v) / prev_v) * 100, 2)
        series[i]["yoy_pct"] = delta_pct
        if delta_pct <= -50:
            cliffs.append({
                "year": series[i]["year"],
                "drop_pct": delta_pct,
                "from": prev_v,
                "to": curr_v,
            })

    trend = classify_trend([r["value"] for r in series])

    return {
        "company_id": company_id,
        "company_name": company.company_name,
        "metric": metric,
        "trend": trend,
        "cliffs_detected": cliffs,
        "series": series,
    }


# ── Methodology Change History ────────────────────────────────────────────────

@router.get("/methodology-changes")
def methodology_changes(company_name: str | None = None, db: Session = Depends(get_db)):
    """List all detected methodology changes (from data_quality_issues)."""
    q = (
        db.query(DataQualityIssue, Upload)
        .join(Upload, DataQualityIssue.upload_id == Upload.id)
        .filter(DataQualityIssue.issue_type == "methodology_change")
    )
    if company_name:
        q = q.filter(Upload.company_name == company_name)
    rows = q.order_by(Upload.uploaded_at.desc()).limit(200).all()
    return [
        {
            "upload_id": u.id,
            "company_name": u.company_name,
            "uploaded_at": u.uploaded_at.isoformat(),
            "detail": i.issue_detail,
        }
        for i, u in rows
    ]


# ── Weight Drift History ──────────────────────────────────────────────────────

@router.get("/weight-drift")
def weight_drift_history(company_name: str | None = None, db: Session = Depends(get_db)):
    """List industry-weight drift events flagged during ingestion."""
    q = (
        db.query(DataQualityIssue, Upload)
        .join(Upload, DataQualityIssue.upload_id == Upload.id)
        .filter(DataQualityIssue.issue_type.in_(["weight_drift", "segment_removed"]))
    )
    if company_name:
        q = q.filter(Upload.company_name == company_name)
    rows = q.order_by(Upload.uploaded_at.desc()).limit(200).all()
    return [
        {
            "upload_id": u.id,
            "company_name": u.company_name,
            "issue_type": i.issue_type,
            "field_name": i.field_name,
            "detail": i.issue_detail,
            "severity": i.severity,
            "uploaded_at": u.uploaded_at.isoformat(),
        }
        for i, u in rows
    ]
