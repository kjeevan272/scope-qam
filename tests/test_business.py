"""
Tests for business-level enhancements:
  rating engine, migration matrix, stale detection, estimates flag,
  methodology change, weight drift, cliff detection, sector benchmark,
  approvals workflow, FX, exports.
"""
import pytest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    AnalystAssignment, CreditMetric, FXRate, PipelineRun,
    RatingMigration, Snapshot, SnapshotApproval, Tenant,
)
from app.pipeline import _load_file
from app.rating_engine import (
    apply_notches, derive_anchor, derive_final_rating,
    migration_direction, notches_between, parse_notch_adjustment,
)
from app.analytics import (
    classify_trend, coverage_pct, detect_methodology_change,
    detect_metric_cliffs, detect_stale_metrics, detect_weight_drift,
    mark_estimates, percentile,
)
from app.fx import seed_default_rates, latest_rate
from app.schema_registry import seed_v1

DATA_DIR = Path(__file__).parent.parent / "data"


@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest.fixture(scope="module")
def engine_and_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    return engine, TestSession


@pytest.fixture(scope="module")
def populated(engine_and_session):
    _, TestSession = engine_and_session
    db = TestSession()
    seed_v1(db)
    seed_default_rates(db)
    run = PipelineRun(files_attempted=4, status="running")
    db.add(run)
    db.flush()
    for fname in sorted(DATA_DIR.glob("*.xlsm")):
        _load_file(db, fname, run)
        db.commit()
    return db, TestSession


@pytest.fixture(scope="module")
def client(engine_and_session, populated):
    _, TestSession = engine_and_session

    def override_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with patch.object(app.router, "lifespan_context", _noop_lifespan):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
    app.dependency_overrides.clear()


# ── Rating Engine ─────────────────────────────────────────────────────────────

def test_parse_notch_adjustment():
    assert parse_notch_adjustment("+1 notch") == 1
    assert parse_notch_adjustment("-2 notches") == -2
    assert parse_notch_adjustment("BB+") == 0
    assert parse_notch_adjustment("") == 0
    assert parse_notch_adjustment(None) == 0


def test_derive_anchor_exact_match():
    assert derive_anchor("B+", "C") == "CCC+"
    assert derive_anchor("BBB", "BB+") == "BBB-"


def test_derive_anchor_blend_fallback():
    # AA + B → middle ground
    result = derive_anchor("AA", "B")
    assert result is not None


def test_apply_notches_downgrade():
    # Scale: CCC+, CCC, CCC-, CC. CCC+ - 2 → CCC-
    assert apply_notches("CCC+", -2) == "CCC-"
    assert apply_notches("CCC+", -3) == "CC"


def test_apply_notches_upgrade():
    assert apply_notches("BBB", 2) == "A-"


def test_derive_final_rating_company_a():
    """Company A: BRP=B+, FRP=C, liquidity=-2 notches → CCC+ down 2 = CCC-."""
    result = derive_final_rating("B+", "C", "-2 notches")
    assert result["anchor_rating"] == "CCC+"
    assert result["notches_applied"] == -2
    assert result["final_rating"] == "CCC-"


def test_derive_final_rating_company_b():
    """Company B: BRP=BBB, FRP=BB+, liquidity=+1 notch → final should be solid IG/HY border."""
    result = derive_final_rating("BBB", "BB+", "+1 notch")
    assert result["anchor_rating"] == "BBB-"
    assert result["notches_applied"] == 1
    assert result["final_rating"] == "BBB"


def test_notches_between_downgrade():
    # B+ to B = down 1 notch (negative)
    assert notches_between("B+", "B") == -1


def test_notches_between_upgrade():
    assert notches_between("BB", "BB+") == 1


def test_migration_direction():
    assert migration_direction(None, "BBB") == "new"
    assert migration_direction("BBB", "BBB") == "affirmation"
    assert migration_direction("B+", "B") == "downgrade"
    assert migration_direction("BB", "BB+") == "upgrade"


# ── Snapshot has derived ratings ──────────────────────────────────────────────

def test_snapshot_anchor_and_final_rating(populated):
    db, _ = populated
    snaps = db.query(Snapshot).all()
    # At least one snapshot must have derived ratings populated
    rated = [s for s in snaps if s.final_rating is not None]
    assert len(rated) > 0


def test_company_a_downgrade_recorded(populated):
    db, _ = populated
    # Company A: A_1 (B+/C) and A_2 (B/CC) should produce a downgrade migration
    migrations = (
        db.query(RatingMigration)
        .filter_by(company_name="Company A")
        .order_by(RatingMigration.migrated_at)
        .all()
    )
    assert len(migrations) >= 1


# ── Migration Matrix API ──────────────────────────────────────────────────────

def test_migration_matrix_endpoint(client):
    r = client.get("/analytics/migration-matrix")
    assert r.status_code == 200
    matrix = r.json()
    assert "total_migrations" in matrix
    assert "count_matrix" in matrix
    assert "percentage_matrix" in matrix
    assert matrix["total_migrations"] >= 1


def test_list_migrations_endpoint(client):
    r = client.get("/analytics/migrations")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)


# ── Analytics utilities ───────────────────────────────────────────────────────

def test_coverage_pct_full():
    from app.extractor import CreditMetrics
    metrics = [
        CreditMetrics(year=2020, ebitda_interest_cover=5, debt_ebitda=2,
                       ffo_debt=0.3, loan_value=0.5, focf_debt=0.1, liquidity=2),
    ]
    assert coverage_pct(metrics) == 100.0


def test_coverage_pct_partial():
    from app.extractor import CreditMetrics
    metrics = [
        CreditMetrics(year=2020, ebitda_interest_cover=5, debt_ebitda=2),
    ]
    # 2 of 6 = 33.33%
    assert coverage_pct(metrics) == 33.33


def test_classify_trend_improving():
    assert classify_trend([1.0, 2.0, 3.0, 4.0]) == "improving"


def test_classify_trend_deteriorating():
    assert classify_trend([4.0, 3.0, 2.0, 1.0]) == "deteriorating"


def test_classify_trend_volatile():
    assert classify_trend([1.0, 5.0, 2.0, 6.0]) == "volatile"


def test_percentile():
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert percentile([], 50) is None


# ── Stale Detection ───────────────────────────────────────────────────────────

def test_stale_metrics_detection():
    from app.extractor import CreditMetrics
    # Years 2019 and 2020 have identical values — should flag 2020 as stale
    metrics = [
        CreditMetrics(year=2018, ebitda_interest_cover=27.3, debt_ebitda=18.5),
        CreditMetrics(year=2019, ebitda_interest_cover=27.3, debt_ebitda=18.5),
        CreditMetrics(year=2020, ebitda_interest_cover=27.3, debt_ebitda=18.5),
    ]
    issues = detect_stale_metrics(metrics)
    assert len(issues) >= 1
    assert any("stale" in i["issue_type"] for i in issues)


def test_stale_detection_in_pipeline(populated):
    """Company A's 2018-2020 have copy-paste values — should be flagged."""
    db, _ = populated
    from app.models import DataQualityIssue
    stale_issues = db.query(DataQualityIssue).filter_by(issue_type="stale_data").all()
    assert len(stale_issues) >= 1


# ── Estimates Flag ────────────────────────────────────────────────────────────

def test_mark_estimates():
    from app.extractor import CreditMetrics
    metrics = [
        CreditMetrics(year=2023),
        CreditMetrics(year=2024),
        CreditMetrics(year=2030),
    ]
    mark_estimates(metrics, submission_year=2025)
    assert metrics[0].is_estimate is False
    assert metrics[1].is_estimate is False
    assert metrics[2].is_estimate is True


# ── Methodology Change ────────────────────────────────────────────────────────

def test_methodology_change_detected():
    issues = detect_methodology_change(
        ["General Methodology", "Consumer Products Methodology"],
        ["General Methodology"],
    )
    assert len(issues) == 1
    assert issues[0]["issue_type"] == "methodology_change"
    assert "removed" in issues[0]["issue_detail"]


def test_methodology_change_no_diff():
    issues = detect_methodology_change(["A"], ["A"])
    assert issues == []


# ── Weight Drift ──────────────────────────────────────────────────────────────

def test_weight_drift_detected():
    """B_1 has Suppliers 15%; B_2 has Suppliers 25% — 10pp shift → warning."""
    prev = [
        {"risk": "Automotive Suppliers", "score": "BBB", "weight": 0.15},
        {"risk": "Automotive and Commercial Vehicle Manufacturers", "score": "BB", "weight": 0.85},
    ]
    new = [
        {"risk": "Automotive Suppliers", "score": "BBB", "weight": 0.25},
        {"risk": "Automotive and Commercial Vehicle Manufacturers", "score": "BB", "weight": 0.75},
    ]
    issues = detect_weight_drift(prev, new)
    assert len(issues) >= 2
    assert any(i["issue_type"] == "weight_drift" for i in issues)


def test_weight_drift_segment_removed():
    prev = [{"risk": "A", "weight": 0.5}, {"risk": "B", "weight": 0.5}]
    new = [{"risk": "A", "weight": 1.0}]
    issues = detect_weight_drift(prev, new)
    removed = [i for i in issues if i["issue_type"] == "segment_removed"]
    assert len(removed) == 1


# ── Cliff Detection ───────────────────────────────────────────────────────────

def test_cliff_detection():
    from app.extractor import CreditMetrics
    metrics = [
        CreditMetrics(year=2020, ebitda_interest_cover=27.3),
        CreditMetrics(year=2021, ebitda_interest_cover=4.9),   # 82% drop → cliff
    ]
    issues = detect_metric_cliffs(metrics, metric="ebitda_interest_cover")
    assert len(issues) >= 1
    assert any("cliff" in i["issue_type"] for i in issues)


# ── Sector Benchmark ──────────────────────────────────────────────────────────

def test_sector_benchmark_api(client):
    r = client.get("/analytics/sector-benchmark?sector=Automobiles&metric=debt_ebitda")
    assert r.status_code == 200
    result = r.json()
    assert result["sector"] == "Automobiles"
    assert "median" in result
    assert "p25" in result
    assert "p75" in result
    assert result["sample_size"] >= 1


def test_sector_benchmark_unknown_metric(client):
    r = client.get("/analytics/sector-benchmark?sector=Automobiles&metric=invalid")
    assert r.status_code == 422


# ── Trend Endpoint ────────────────────────────────────────────────────────────

def test_trend_endpoint_company_a(client):
    companies = client.get("/companies").json()
    company_a = next(c for c in companies if c["company_name"] == "Company A")
    r = client.get(f"/analytics/trend/{company_a['id']}?metric=ebitda_interest_cover")
    assert r.status_code == 200
    result = r.json()
    assert "trend" in result
    assert "series" in result
    assert "cliffs_detected" in result
    # Company A had a known cliff (27.3 → 4.9)
    assert len(result["cliffs_detected"]) >= 1


# ── Methodology Changes Endpoint ──────────────────────────────────────────────

def test_methodology_changes_endpoint(client):
    r = client.get("/analytics/methodology-changes")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── Weight Drift Endpoint ─────────────────────────────────────────────────────

def test_weight_drift_endpoint(client):
    r = client.get("/analytics/weight-drift")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)


# ── Workflow: Approvals & Assignments ─────────────────────────────────────────

def test_pending_approvals_endpoint(client):
    r = client.get("/workflow/pending-approvals")
    assert r.status_code == 200
    pending = r.json()
    assert isinstance(pending, list)
    # All newly loaded snapshots should be pending
    assert len(pending) >= 1


def test_approve_snapshot_endpoint(client):
    r = client.get("/workflow/pending-approvals")
    pending = r.json()
    if not pending:
        pytest.skip("no pending snapshots to approve")
    snap_id = pending[0]["snapshot_id"]
    r2 = client.post(
        f"/workflow/snapshots/{snap_id}/approve",
        json={"decision": "approved", "approval_role": "analyst", "comment": "OK"},
    )
    assert r2.status_code == 201
    body = r2.json()
    assert body["decision"] == "approved"

    # Confirm approval is visible
    r3 = client.get(f"/workflow/snapshots/{snap_id}/approvals")
    assert r3.status_code == 200
    approvals = r3.json()
    assert len(approvals) >= 1


# ── FX ────────────────────────────────────────────────────────────────────────

def test_fx_seed_and_latest_rate(populated):
    db, _ = populated
    assert latest_rate(db, "EUR", "USD") is not None
    assert latest_rate(db, "CHF", "USD") is not None
    assert latest_rate(db, "USD", "USD") == 1


def test_fx_list_endpoint(client):
    r = client.get("/fx")
    assert r.status_code == 200
    rates = r.json()
    assert len(rates) >= 1


def test_fx_latest_endpoint(client):
    r = client.get("/fx/latest?from_ccy=EUR&to_ccy=USD")
    assert r.status_code == 200
    assert r.json()["rate"] > 0


# ── Exports ───────────────────────────────────────────────────────────────────

def test_esma_xml_export(client):
    r = client.get("/exports/esma-xml/1")
    assert r.status_code == 200
    assert "xml" in r.headers["content-type"].lower()
    assert b"<CEREPSubmission" in r.content
    assert b"<RatedEntity>" in r.content


def test_csv_export(client):
    r = client.get("/exports/snapshots-csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().split("\n")
    assert len(lines) >= 2   # header + at least one row
    assert "company_name" in lines[0]
    assert "final_rating" in lines[0]
