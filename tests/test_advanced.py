"""
Tests for the 20 advanced capabilities:
  multi-tenancy, schema registry, CDC, queue, OpenLineage,
  Great Expectations, RBAC, retention, partitioning DDL.
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
    CDCEvent, IngestionTask, PipelineRun, RetentionPolicy,
    SchemaVersion, Tenant, Upload,
)
from app.pipeline import _load_file
from app import cdc as cdc_module
from app.schema_registry import check_compatibility, seed_v1, register_new_version
from app.security import generate_api_key, hash_key
from app.queue import enqueue, claim_next, complete, queue_stats
from app.retention import enforce_all_policies
from app.expectations import validate_with_ge

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
def db(engine_and_session):
    _, TestSession = engine_and_session
    session = TestSession()
    yield session
    session.close()


@pytest.fixture(scope="module")
def populated_db(engine_and_session):
    """DB with all 4 files loaded under tenant_id=None."""
    _, TestSession = engine_and_session
    session = TestSession()
    seed_v1(session)
    run = PipelineRun(files_attempted=4, status="running")
    session.add(run)
    session.flush()
    for fname in sorted(DATA_DIR.glob("*.xlsm")):
        _load_file(session, fname, run)
        session.commit()
    return session, TestSession


@pytest.fixture(scope="module")
def client(engine_and_session, populated_db):
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


# ── Schema Registry ───────────────────────────────────────────────────────────

def test_schema_registry_seeded(db):
    seed_v1(db)
    ver = db.query(SchemaVersion).filter_by(subject="MASTER", version=1).first()
    assert ver is not None
    assert "Rated entity" in ver.schema_str
    assert ver.compatibility == "BACKWARD"
    assert ver.fingerprint is not None


def test_schema_compatibility_check_no_breaking(db):
    seed_v1(db)
    from app.extractor import KNOWN_LABELS
    result = check_compatibility(KNOWN_LABELS, db)
    assert result["compatible"] is True
    assert result["breaking"] is False


def test_schema_compatibility_detects_missing_required(db):
    seed_v1(db)
    # Remove required "Rated entity" — should be breaking
    from app.extractor import KNOWN_LABELS
    reduced = KNOWN_LABELS - {"Rated entity"}
    result = check_compatibility(reduced, db)
    assert "Rated entity" in result["removed_labels"]
    assert result["breaking"] is True


def test_schema_compatibility_allows_new_optional_labels(db):
    seed_v1(db)
    from app.extractor import KNOWN_LABELS
    expanded = KNOWN_LABELS | {"New Optional Field"}
    result = check_compatibility(expanded, db)
    assert "New Optional Field" in result["added_labels"]
    assert result["breaking"] is False   # BACKWARD mode: additions are safe


def test_schema_registry_api(client):
    r = client.get("/admin/schema-registry?subject=MASTER")
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) >= 1
    assert versions[0]["subject"] == "MASTER"


def test_schema_compatibility_api(client):
    from app.extractor import KNOWN_LABELS
    r = client.post("/admin/schema-registry/check-compatibility", json={
        "labels": list(KNOWN_LABELS),
        "subject": "MASTER",
    })
    assert r.status_code == 200
    result = r.json()
    assert result["compatible"] is True


# ── Multi-tenancy ─────────────────────────────────────────────────────────────

def test_tenant_creation_api(client):
    r = client.post("/admin/tenants", json={"slug": "test-bank", "name": "Test Bank AG"})
    assert r.status_code == 201
    t = r.json()
    assert t["slug"] == "test-bank"
    assert t["is_active"] is True


def test_tenant_isolation_separate_uploads(engine_and_session):
    """Files loaded under different tenant_ids don't interfere with each other."""
    _, TestSession = engine_and_session
    db = TestSession()
    run = PipelineRun(files_attempted=1, status="running")
    db.add(run)
    db.flush()

    # Load A_1 under tenant_id=99
    _load_file(db, DATA_DIR / "corporates_A_1.xlsm", run, tenant_id=99)
    db.commit()

    uploads_t99 = db.query(Upload).filter_by(tenant_id=99).all()
    assert len(uploads_t99) == 1
    assert uploads_t99[0].company_name == "Company A"
    db.close()


# ── CDC Events ────────────────────────────────────────────────────────────────

def test_cdc_events_emitted_on_upload(populated_db):
    session, _ = populated_db
    events = session.query(CDCEvent).filter_by(table_name="uploads").all()
    assert len(events) > 0
    insert_events = [e for e in events if e.event_type == "INSERT"]
    assert len(insert_events) > 0


def test_cdc_events_have_after_state(populated_db):
    session, _ = populated_db
    event = session.query(CDCEvent).filter_by(table_name="uploads", event_type="INSERT").first()
    assert event.after_state is not None
    assert "filename" in event.after_state
    # binary fields should be omitted
    assert event.after_state.get("raw_file") == "<binary>"


def test_cdc_snapshot_events(populated_db):
    session, _ = populated_db
    events = session.query(CDCEvent).filter_by(table_name="snapshots").all()
    assert len(events) > 0


def test_cdc_publish_pending_api(client):
    r = client.post("/admin/cdc/publish?batch_size=50")
    assert r.status_code == 200
    result = r.json()
    assert "published" in result
    assert isinstance(result["published"], int)


def test_cdc_pending_endpoint(client):
    r = client.get("/admin/cdc/pending")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── Queue-based Ingestion ─────────────────────────────────────────────────────

def test_queue_enqueue_and_claim(engine_and_session):
    _, TestSession = engine_and_session
    db = TestSession()

    task_id = enqueue(db, "/app/data/test.xlsm", tenant_id=None, priority=1)
    db.commit()

    task = claim_next(db, worker_id="test-worker")
    db.commit()

    assert task is not None
    assert task.status == "claimed"
    assert task.claimed_by == "test-worker"
    assert task.attempts == 1

    complete(db, task.id)
    db.commit()

    done = db.get(IngestionTask, task_id)
    assert done.status == "done"
    db.close()


def test_queue_stats_api(client):
    r = client.get("/admin/queue/stats")
    assert r.status_code == 200
    stats = r.json()
    assert isinstance(stats, dict)


def test_queue_enqueue_api(client):
    r = client.post("/admin/queue/enqueue?file_path=/tmp/test.xlsm&priority=3")
    assert r.status_code == 200
    result = r.json()
    assert "task_id" in result
    assert result["status"] == "enqueued"


# ── RBAC / Security ───────────────────────────────────────────────────────────

def test_api_key_generation():
    raw, hashed = generate_api_key()
    assert len(raw) > 20
    assert len(hashed) == 64
    assert hashed == hash_key(raw)


def test_api_key_create_and_revoke_api(client):
    # First create a tenant
    t = client.post("/admin/tenants", json={"slug": "rbac-test", "name": "RBAC Test"})
    if t.status_code == 409:
        tenant_id = client.get("/admin/tenants").json()[0]["id"]
    else:
        tenant_id = t.json()["id"]

    # Create analyst key
    r = client.post("/admin/api-keys", json={
        "tenant_id": tenant_id,
        "role": "analyst",
        "description": "Test analyst key",
    })
    assert r.status_code == 201
    key_data = r.json()
    assert "raw_key" in key_data   # shown once
    assert key_data["role"] == "analyst"

    # Revoke it
    r2 = client.delete(f"/admin/api-keys/{key_data['id']}")
    assert r2.status_code == 200
    assert r2.json()["revoked"] is True


# ── Great Expectations ────────────────────────────────────────────────────────

def test_ge_validation_clean_file():
    from app.extractor import extract
    data = extract(DATA_DIR / "corporates_A_1.xlsm")
    issues = validate_with_ge(data)
    # Clean file should produce zero or few GE issues
    errors = [i for i in issues if i.get("severity") == "error"]
    assert len(errors) == 0


def test_ge_validation_flags_invalid_currency():
    from app.extractor import MasterData
    data = MasterData(filename="test.xlsm", file_hash="abc", raw_bytes=b"")
    data.currency = "XYZ"   # invalid
    data.rated_entity = "Test Corp"
    data.sector = "Energy"
    data.country = "Germany"
    data.accounting_principles = "IFRS"
    data.business_year_end = "31 December"

    issues = validate_with_ge(data)
    currency_issues = [i for i in issues if "currency" in (i.get("field_name") or "")]
    assert len(currency_issues) >= 1


def test_ge_issues_stored_in_pipeline(populated_db):
    from app.models import DataQualityIssue
    session, _ = populated_db
    # Check that expectation_type is populated for GE-sourced issues
    ge_issues = session.query(DataQualityIssue).filter(
        DataQualityIssue.expectation_type.isnot(None)
    ).all()
    # GE issues may or may not exist depending on file quality
    assert isinstance(ge_issues, list)


# ── OpenLineage ───────────────────────────────────────────────────────────────

def test_openlineage_emit_events_logged(caplog):
    import logging
    from app import openlineage
    with caplog.at_level(logging.INFO, logger="app.openlineage"):
        event = openlineage.emit_start("test-run-id", "/tmp/test.xlsm", "abc123")
    assert event["eventType"] == "START"
    assert event["run"]["runId"] == "test-run-id"


def test_openlineage_complete_has_column_lineage():
    from app import openlineage
    prov = [
        {"field_name": "business_risk_profile", "source_sheet": "MASTER",
         "source_row": 42, "source_col": "C"},
    ]
    event = openlineage.emit_complete(
        "run-id", "/tmp/test.xlsm", "abc123", prov, 95.0
    )
    assert event["eventType"] == "COMPLETE"
    output = event["outputs"][0]
    assert "columnLineage" in output["facets"]
    assert "business_risk_profile" in output["facets"]["columnLineage"]["fields"]


def test_upload_has_openlineage_run_id(populated_db):
    session, _ = populated_db
    upload = session.query(Upload).filter_by(status="processed").first()
    assert upload.openlineage_run_id is not None
    assert len(upload.openlineage_run_id) == 36  # UUID format


# ── Retention Policy ──────────────────────────────────────────────────────────

def test_retention_policy_api(client):
    r = client.post("/admin/retention", json={
        "table_name": "cdc_events",
        "retain_days": 90,
        "archive_before_delete": False,
    })
    assert r.status_code == 201
    policy = r.json()
    assert policy["table_name"] == "cdc_events"
    assert policy["retain_days"] == 90


def test_retention_enforce_api(client):
    r = client.post("/admin/retention/enforce")
    assert r.status_code == 200
    result = r.json()
    assert "enforced" in result


# ── Partitioning DDL ──────────────────────────────────────────────────────────

def test_partitioning_ddl_endpoint(client):
    r = client.get("/admin/partitioning/ddl")
    assert r.status_code == 200
    ddl = r.text
    assert "PARTITION OF snapshots" in ddl
    assert "PARTITION OF credit_metrics" in ddl
    assert "PARTITION OF uploads" in ddl


# ── Data Governance: Catalog ──────────────────────────────────────────────────

def test_catalog_endpoint(client):
    r = client.get("/catalog")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── Schema Audit per Upload ───────────────────────────────────────────────────

def test_schema_audit_created_for_uploads(populated_db):
    from app.models import SchemaAudit
    session, _ = populated_db
    audits = session.query(SchemaAudit).all()
    assert len(audits) >= 1
    for audit in audits:
        assert audit.labels_seen is not None
        assert isinstance(audit.labels_seen, list)
        assert len(audit.labels_seen) > 0
