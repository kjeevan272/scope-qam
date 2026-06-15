"""
Admin endpoints:
  - Tenant management (multi-tenancy)
  - API key management (RBAC)
  - Schema registry (evolution, compatibility)
  - Retention policy management + enforcement
  - CDC event relay
  - Queue status
  - Partitioning DDL guidance
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey, CDCEvent, IngestionTask, RetentionPolicy, SchemaVersion, Tenant
from app.schema_registry import check_compatibility, register_new_version, get_latest_version
from app.retention import enforce_all_policies
from app import cdc, queue as task_queue
from app.security import generate_api_key, hash_key, require_role

router = APIRouter(prefix="/admin", tags=["Admin"])


# ── Tenant Management ─────────────────────────────────────────────────────────

class TenantCreate(BaseModel):
    slug: str
    name: str


class TenantOut(BaseModel):
    id: int
    slug: str
    name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(db: Session = Depends(get_db), _=Depends(require_role("admin"))):
    return db.query(Tenant).order_by(Tenant.slug).all()


@router.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(body: TenantCreate, db: Session = Depends(get_db),
                  _=Depends(require_role("admin"))):
    existing = db.query(Tenant).filter_by(slug=body.slug).first()
    if existing:
        raise HTTPException(409, f"Tenant '{body.slug}' already exists")
    tenant = Tenant(slug=body.slug, name=body.name)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


# ── API Key Management (RBAC) ─────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    tenant_id: int
    role: str = "analyst"
    description: str | None = None


class ApiKeyOut(BaseModel):
    id: int
    tenant_id: int
    role: str
    description: str | None
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None

    class Config:
        from_attributes = True


class ApiKeyCreated(ApiKeyOut):
    raw_key: str   # shown ONCE on creation — not stored


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
def create_api_key(body: ApiKeyCreate, db: Session = Depends(get_db),
                   _=Depends(require_role("admin"))):
    tenant = db.get(Tenant, body.tenant_id)
    if not tenant:
        raise HTTPException(404, f"Tenant {body.tenant_id} not found")

    if body.role not in ("admin", "analyst", "viewer"):
        raise HTTPException(422, "role must be admin, analyst, or viewer")

    raw_key, hashed = generate_api_key()
    key = ApiKey(
        tenant_id=body.tenant_id,
        key_hash=hashed,
        role=body.role,
        description=body.description,
    )
    db.add(key)
    db.commit()
    db.refresh(key)

    return ApiKeyCreated(
        id=key.id,
        tenant_id=key.tenant_id,
        role=key.role,
        description=key.description,
        is_active=key.is_active,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        raw_key=raw_key,   # only time the raw key is visible
    )


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: int, db: Session = Depends(get_db),
                   _=Depends(require_role("admin"))):
    key = db.get(ApiKey, key_id)
    if not key:
        raise HTTPException(404, f"Key {key_id} not found")
    key.is_active = False
    db.commit()
    return {"revoked": True}


# ── Schema Registry ───────────────────────────────────────────────────────────

class SchemaVersionOut(BaseModel):
    id: int
    subject: str
    version: int
    compatibility: str
    fingerprint: str | None
    breaking_change: bool
    introduced_at: datetime
    notes: str | None

    class Config:
        from_attributes = True


class CompatibilityCheckRequest(BaseModel):
    labels: list[str]
    subject: str = "MASTER"


class NewSchemaRequest(BaseModel):
    schema_str: dict[str, Any]
    subject: str = "MASTER"
    notes: str = ""


@router.get("/schema-registry", response_model=list[SchemaVersionOut])
def list_schema_versions(subject: str = "MASTER", db: Session = Depends(get_db)):
    return (
        db.query(SchemaVersion)
        .filter_by(subject=subject)
        .order_by(SchemaVersion.version.desc())
        .all()
    )


@router.post("/schema-registry/check-compatibility")
def check_compat(body: CompatibilityCheckRequest, db: Session = Depends(get_db)):
    """Check if a new set of labels is backward-compatible with the registered schema."""
    return check_compatibility(set(body.labels), db, subject=body.subject)


@router.post("/schema-registry/register", status_code=201)
def register_schema(body: NewSchemaRequest, db: Session = Depends(get_db),
                    _=Depends(require_role("admin"))):
    """Register a new schema version after compatibility validation."""
    result = register_new_version(db, body.schema_str, subject=body.subject, notes=body.notes)
    if result.get("breaking") and result.get("mode") != "NONE":
        raise HTTPException(
            409,
            detail={
                "message": "Schema is not backward-compatible — registration rejected",
                **result,
            },
        )
    return result


# ── Retention Policies ────────────────────────────────────────────────────────

class RetentionPolicyCreate(BaseModel):
    table_name: str
    retain_days: int
    archive_before_delete: bool = False
    archive_location: str | None = None
    tenant_id: int | None = None


class RetentionPolicyOut(BaseModel):
    id: int
    table_name: str
    retain_days: int
    archive_before_delete: bool
    is_active: bool
    last_enforced_at: datetime | None
    rows_deleted_last_run: int | None

    class Config:
        from_attributes = True


@router.get("/retention", response_model=list[RetentionPolicyOut])
def list_retention_policies(db: Session = Depends(get_db)):
    return db.query(RetentionPolicy).order_by(RetentionPolicy.table_name).all()


@router.post("/retention", response_model=RetentionPolicyOut, status_code=201)
def create_retention_policy(body: RetentionPolicyCreate, db: Session = Depends(get_db),
                             _=Depends(require_role("admin"))):
    policy = RetentionPolicy(**body.model_dump())
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


@router.post("/retention/enforce")
def enforce_retention(db: Session = Depends(get_db), _=Depends(require_role("admin"))):
    """Run all active retention policies now."""
    results = enforce_all_policies(db)
    return {"enforced": results}


# ── CDC Event Relay ───────────────────────────────────────────────────────────

@router.post("/cdc/publish")
def publish_cdc_events(batch_size: int = 100, db: Session = Depends(get_db),
                        _=Depends(require_role("admin"))):
    """Relay pending CDC outbox events to downstream consumers."""
    count = cdc.publish_pending(db, batch_size=batch_size)
    return {"published": count}


@router.get("/cdc/pending")
def pending_cdc_events(limit: int = 20, db: Session = Depends(get_db)):
    """Inspect unpublished CDC events (for monitoring)."""
    events = (
        db.query(CDCEvent)
        .filter(CDCEvent.published_at.is_(None))
        .order_by(CDCEvent.occurred_at)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "table_name": e.table_name,
            "record_id": e.record_id,
            "occurred_at": e.occurred_at.isoformat(),
            "aggregate_type": e.aggregate_type,
        }
        for e in events
    ]


# ── Queue Status ──────────────────────────────────────────────────────────────

@router.get("/queue/stats")
def queue_stats(db: Session = Depends(get_db)):
    """Current ingestion queue status by task state."""
    return task_queue.queue_stats(db)


@router.post("/queue/enqueue")
def enqueue_file(
    file_path: str,
    priority: int = 5,
    tenant_id: int | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_role("analyst")),
):
    """Enqueue a file path for async ingestion by a worker."""
    task_id = task_queue.enqueue(db, file_path, tenant_id=tenant_id, priority=priority)
    db.commit()
    return {"task_id": task_id, "status": "enqueued"}


# ── Partitioning Strategy DDL ─────────────────────────────────────────────────

PARTITIONING_DDL = """
-- PostgreSQL RANGE partitioning for snapshots by year
-- Run once after CREATE TABLE snapshots (before inserting data)

CREATE TABLE snapshots_2023 PARTITION OF snapshots
  FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');

CREATE TABLE snapshots_2024 PARTITION OF snapshots
  FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

CREATE TABLE snapshots_2025 PARTITION OF snapshots
  FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

-- RANGE partitioning for credit_metrics by metric_year
CREATE TABLE credit_metrics_2018 PARTITION OF credit_metrics
  FOR VALUES FROM (2018) TO (2019);
-- ... repeat per year

-- List partitioning for uploads by tenant_id (multi-tenant isolation)
-- Each tenant's data lives in its own physical partition:
CREATE TABLE uploads_tenant_1 PARTITION OF uploads
  FOR VALUES IN (1);

-- Add a new year partition in advance (run via pg_cron annually):
-- CREATE TABLE snapshots_{YEAR} PARTITION OF snapshots
--   FOR VALUES FROM ('{YEAR}-01-01') TO ('{YEAR+1}-01-01');
"""


@router.get("/partitioning/ddl", response_class=__import__("fastapi").responses.PlainTextResponse)
def partitioning_ddl():
    """Returns the recommended PostgreSQL partitioning DDL for production deployment."""
    return PARTITIONING_DDL
