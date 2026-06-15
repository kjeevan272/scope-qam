"""Integration tests for FastAPI endpoints using TestClient + SQLite."""
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
from app.models import PipelineRun
from app.pipeline import _load_file

DATA_DIR = Path(__file__).parent.parent / "data"


@asynccontextmanager
async def _noop_lifespan(app):
    yield


@pytest.fixture(scope="module")
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    db = TestSession()
    run = PipelineRun(files_attempted=4, status="running")
    db.add(run)
    db.flush()
    for fname in sorted(DATA_DIR.glob("*.xlsm")):
        _load_file(db, fname, run)
        db.commit()
    db.close()

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


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_companies_returns_current_versions(client):
    r = client.get("/companies")
    assert r.status_code == 200
    data = r.json()
    names = {c["company_name"] for c in data}
    assert "Company A" in names
    assert "Company B" in names
    assert all(c["is_current"] for c in data)


def test_company_versions(client):
    r = client.get("/companies")
    company_a = next(c for c in r.json() if c["company_name"] == "Company A")
    cid = company_a["id"]

    r2 = client.get(f"/companies/{cid}/versions")
    assert r2.status_code == 200
    versions = r2.json()
    # Company A metadata unchanged across uploads → single SCD record
    assert len(versions) == 1
    assert versions[0]["is_current"] is True


def test_company_changelog(client):
    r = client.get("/companies")
    company_a = next(c for c in r.json() if c["company_name"] == "Company A")
    cid = company_a["id"]
    r2 = client.get(f"/companies/{cid}/changelog")
    assert r2.status_code == 200
    # No metadata changes for Company A → empty changelog
    assert isinstance(r2.json(), list)


def test_company_history_time_series(client):
    r = client.get("/companies")
    company_a = next(c for c in r.json() if c["company_name"] == "Company A")
    cid = company_a["id"]

    r2 = client.get(f"/companies/{cid}/history")
    assert r2.status_code == 200
    history = r2.json()
    # A_2 has different rating scores from A_1 — both snapshots created
    assert len(history) >= 2
    for snap in history:
        assert "credit_metrics" in snap
        assert len(snap["credit_metrics"]) > 0


def test_compare_companies_point_in_time(client):
    r = client.get("/companies")
    ids = ",".join(str(c["id"]) for c in r.json())
    r2 = client.get(f"/companies/compare?company_ids={ids}")
    assert r2.status_code == 200
    result = r2.json()
    assert "as_of_date" in result
    assert len(result["companies"]) == 2


def test_list_snapshots(client):
    r = client.get("/snapshots")
    assert r.status_code == 200
    # A_1, A_2 (different rating scores), B_1, B_2 → all create snapshots
    assert len(r.json()) == 4


def test_list_snapshots_filter_by_currency(client):
    r = client.get("/snapshots?currency=CHF")
    assert r.status_code == 200
    # Company B (CHF): B_1 and B_2 both create snapshots (different content)
    assert len(r.json()) == 2


def test_snapshots_latest(client):
    r = client.get("/snapshots/latest")
    assert r.status_code == 200
    assert len(r.json()) == 2  # one per company


def test_snapshot_detail(client):
    r = client.get("/snapshots/1")
    assert r.status_code == 200
    snap = r.json()
    assert "industry_risks" in snap
    assert isinstance(snap["industry_risks"], list)
    assert "content_fingerprint" in snap
    assert snap["content_fingerprint"] is not None


def test_snapshot_provenance(client):
    r = client.get("/snapshots/1/provenance")
    assert r.status_code == 200
    detail = r.json()
    assert "provenance" in detail
    assert len(detail["provenance"]) > 0
    # Each provenance entry has cell location
    for p in detail["provenance"]:
        assert "source_sheet" in p
        assert "source_row" in p


def test_list_uploads(client):
    r = client.get("/uploads")
    assert r.status_code == 200
    uploads = r.json()
    # A_1 (processed), A_2 (skipped_no_delta with upload record), B_1, B_2
    assert len(uploads) == 4


def test_upload_quality_score(client):
    uploads = client.get("/uploads").json()
    processed = [u for u in uploads if u["status"] == "processed"]
    for u in processed:
        assert u["quality_score"] is not None
        assert 0 <= u["quality_score"] <= 100


def test_upload_business_key(client):
    uploads = client.get("/uploads").json()
    processed = [u for u in uploads if u["status"] == "processed"]
    for u in processed:
        assert u["business_key"] is not None
        assert len(u["business_key"]) == 64


def test_upload_stats(client):
    r = client.get("/uploads/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["total_uploads"] == 4
    assert stats["processed"] == 4
    assert stats["skipped_no_delta"] == 0
    assert stats["failed"] == 0
    assert stats["companies_tracked"] == 2
    assert stats["avg_quality_score"] is not None


def test_upload_details_with_quality_issues(client):
    uploads = client.get("/uploads").json()
    b2 = next((u for u in uploads if "B_2" in u["filename"]), None)
    assert b2 is not None
    r = client.get(f"/uploads/{b2['id']}/details")
    assert r.status_code == 200
    detail = r.json()
    assert "quality_issues" in detail
    assert len(detail["quality_issues"]) >= 1
    # Issues now include cell location
    for issue in detail["quality_issues"]:
        assert "source_sheet" in issue


def test_upload_schema_audit(client):
    r = client.get("/uploads/1/schema-audit")
    assert r.status_code == 200
    audit = r.json()
    assert "labels_seen" in audit
    assert "unknown_labels" in audit
    assert isinstance(audit["labels_seen"], list)
    assert len(audit["labels_seen"]) > 0


def test_download_original_file(client):
    r = client.get("/uploads/1/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.ms-excel")
    assert len(r.content) > 0


def test_prometheus_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "uploads_total" in r.text
    assert "upload_quality_score_avg" in r.text
    assert "snapshots_total" in r.text


def test_rules_crud(client):
    # Create a rule
    r = client.post("/rules", json={
        "field_name": "currency",
        "rule_type": "allowed_values",
        "params": ["EUR", "USD", "CHF"],
        "severity": "warning",
        "description": "Only accepted currencies",
    })
    assert r.status_code == 201
    rule = r.json()
    assert rule["id"] is not None
    assert rule["is_active"] is True

    # List rules
    r2 = client.get("/rules")
    assert r2.status_code == 200
    assert any(ru["field_name"] == "currency" for ru in r2.json())

    # Deactivate
    r3 = client.patch(f"/rules/{rule['id']}/deactivate")
    assert r3.status_code == 200
    assert r3.json()["is_active"] is False


def test_404_on_missing_company(client):
    r = client.get("/companies/99999")
    assert r.status_code == 404


def test_404_on_missing_snapshot(client):
    r = client.get("/snapshots/99999")
    assert r.status_code == 404
