from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Numeric, Boolean, Date,
    DateTime, ForeignKey, LargeBinary, UniqueConstraint, Index, JSON,
)
from sqlalchemy.orm import relationship

from app.db import Base


def utcnow():
    return datetime.now(timezone.utc)


# ── Tenant (Multi-tenancy) ────────────────────────────────────────────────────

class Tenant(Base):
    """
    Logical tenant boundary. Every data entity is scoped to a tenant.
    PostgreSQL Row-Level Security policies enforce isolation at the DB layer.
    """
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    slug = Column(String(100), unique=True, nullable=False)   # e.g. "acme-ratings"
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    api_keys = relationship("ApiKey", back_populates="tenant")
    uploads = relationship("Upload", back_populates="tenant")
    companies = relationship("Company", back_populates="tenant")


# ── RBAC ─────────────────────────────────────────────────────────────────────

class ApiKey(Base):
    """
    Hashed API keys scoped to a tenant + role.
    Roles: admin (full access), analyst (read+write snapshots), viewer (read-only).
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True)  # SHA-256 of the raw key
    role = Column(String(20), nullable=False, default="analyst")  # admin/analyst/viewer
    description = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_used_at = Column(DateTime(timezone=True))

    tenant = relationship("Tenant", back_populates="api_keys")

    __table_args__ = (Index("ix_api_keys_hash", "key_hash"),)


# ── Pipeline Run ──────────────────────────────────────────────────────────────

class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    run_at = Column(DateTime(timezone=True), default=utcnow)
    files_attempted = Column(Integer, default=0)
    files_processed = Column(Integer, default=0)
    files_skipped = Column(Integer, default=0)
    files_failed = Column(Integer, default=0)
    duration_seconds = Column(Numeric(10, 3))
    status = Column(String(20))  # running/completed/partial/failed
    error_detail = Column(Text)
    openlineage_run_id = Column(String(36))   # UUID used in OpenLineage events

    uploads = relationship("Upload", back_populates="pipeline_run")


# ── Schema Registry ───────────────────────────────────────────────────────────

class SchemaVersion(Base):
    """
    Tracks known Excel template versions.
    Compatibility modes follow Confluent Schema Registry conventions:
      BACKWARD  — new schema reads data written by old schema
      FORWARD   — old schema reads data written by new schema
      FULL      — both directions compatible
      NONE      — no compatibility check
    """
    __tablename__ = "schema_versions"

    id = Column(Integer, primary_key=True)
    subject = Column(String(100), nullable=False, default="MASTER")  # schema subject
    version = Column(Integer, nullable=False)
    schema_str = Column(JSON, nullable=False)    # {label: {type, required, description}}
    compatibility = Column(String(20), default="BACKWARD")
    fingerprint = Column(String(64))             # SHA-256 of canonical schema JSON
    introduced_at = Column(DateTime(timezone=True), default=utcnow)
    deprecated_at = Column(DateTime(timezone=True))
    breaking_change = Column(Boolean, default=False)
    notes = Column(Text)

    __table_args__ = (
        UniqueConstraint("subject", "version", name="uq_schema_subject_version"),
        Index("ix_schema_version_subject", "subject", "version"),
    )


# ── Data Retention Policy ─────────────────────────────────────────────────────

class RetentionPolicy(Base):
    """
    Configurable per-table retention rules.
    Enforcement: a scheduled cleanup job reads active policies and deletes/archives.
    """
    __tablename__ = "retention_policies"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    table_name = Column(String(100), nullable=False)
    retain_days = Column(Integer, nullable=False)   # delete records older than this
    archive_before_delete = Column(Boolean, default=False)
    archive_location = Column(String(500))          # s3://bucket/prefix or file path
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    last_enforced_at = Column(DateTime(timezone=True))
    rows_deleted_last_run = Column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("tenant_id", "table_name", name="uq_retention_tenant_table"),
    )


# ── Upload / Ingestion Audit ──────────────────────────────────────────────────

class Upload(Base):
    """One row per file ingestion attempt — full audit trail."""
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"))
    filename = Column(String(255), nullable=False)
    file_hash = Column(String(64), nullable=False)   # content-hash idempotency key
    business_key = Column(String(64), nullable=True)  # company|period hash
    raw_file = Column(LargeBinary, nullable=False)
    uploaded_at = Column(DateTime(timezone=True), default=utcnow)
    company_name = Column(String(255))
    status = Column(String(20))  # processed/skipped/skipped_no_delta/failed
    error_detail = Column(Text)
    quality_score = Column(Numeric(5, 2))
    reprocessed_from_id = Column(Integer, ForeignKey("uploads.id"), nullable=True)
    reprocess_reason = Column(Text)
    openlineage_run_id = Column(String(36))   # links to OpenLineage job run

    tenant = relationship("Tenant", back_populates="uploads")
    pipeline_run = relationship("PipelineRun", back_populates="uploads")
    snapshots = relationship("Snapshot", back_populates="upload")
    quality_issues = relationship("DataQualityIssue", back_populates="upload")
    schema_audit = relationship("SchemaAudit", back_populates="upload", uselist=False)
    reprocessed_from = relationship(
        "Upload", remote_side="Upload.id", foreign_keys="Upload.reprocessed_from_id"
    )

    __table_args__ = (
        # Per-tenant file-hash uniqueness (different tenants may share files)
        UniqueConstraint("tenant_id", "file_hash", name="uq_upload_tenant_hash"),
        Index("ix_uploads_business_key", "business_key"),
        Index("ix_uploads_company_status", "company_name", "status"),
        # Partition-friendly index on upload date
        Index("ix_uploads_tenant_date", "tenant_id", "uploaded_at"),
    )


# ── CDC Event Log ─────────────────────────────────────────────────────────────

class CDCEvent(Base):
    """
    Change Data Capture log — every INSERT/UPDATE/DELETE on core tables.
    Enables downstream consumers (Kafka, event-driven pipelines) to subscribe
    to changes without polling. Pattern: outbox table drained by a relay worker.
    """
    __tablename__ = "cdc_events"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    event_id = Column(String(36), nullable=False, unique=True)   # UUID
    event_type = Column(String(20), nullable=False)  # INSERT/UPDATE/DELETE
    table_name = Column(String(100), nullable=False)
    record_id = Column(Integer, nullable=False)
    before_state = Column(JSON)   # NULL for INSERT
    after_state = Column(JSON)    # NULL for DELETE
    changed_fields = Column(JSON) # list of field names that changed (UPDATE only)
    occurred_at = Column(DateTime(timezone=True), default=utcnow)
    published_at = Column(DateTime(timezone=True))  # NULL until relay sends it
    aggregate_type = Column(String(50))  # domain aggregate: Company/Snapshot/Upload
    aggregate_id = Column(Integer)       # business key of the aggregate

    __table_args__ = (
        Index("ix_cdc_events_unpublished", "published_at", "occurred_at"),
        Index("ix_cdc_events_tenant_table", "tenant_id", "table_name", "occurred_at"),
    )


# ── Queue (DB-backed Async Ingestion) ─────────────────────────────────────────

class IngestionTask(Base):
    """
    DB-backed task queue for async/queue-based ingestion.
    Workers poll for pending tasks; supports at-least-once delivery with
    visibility timeout (claimed_until) to prevent double-processing.
    """
    __tablename__ = "ingestion_tasks"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(64))
    priority = Column(Integer, default=5)          # 1=highest, 10=lowest
    status = Column(String(20), default="pending") # pending/claimed/done/failed
    claimed_by = Column(String(100))               # worker ID
    claimed_until = Column(DateTime(timezone=True)) # visibility timeout
    attempts = Column(Integer, default=0)
    last_error = Column(Text)
    enqueued_at = Column(DateTime(timezone=True), default=utcnow)
    completed_at = Column(DateTime(timezone=True))
    upload_id = Column(Integer, ForeignKey("uploads.id"), nullable=True)

    __table_args__ = (
        Index("ix_ingestion_tasks_pending", "status", "priority", "enqueued_at"),
        Index("ix_ingestion_tasks_tenant", "tenant_id", "status"),
    )


# ── Schema Audit ─────────────────────────────────────────────────────────────

class SchemaAudit(Base):
    """Records schema-level observations for each ingested file."""
    __tablename__ = "schema_audit"

    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey("uploads.id"), unique=True, nullable=False)
    observed_at = Column(DateTime(timezone=True), default=utcnow)
    schema_version_id = Column(Integer, ForeignKey("schema_versions.id"), nullable=True)
    labels_seen = Column(JSON)
    unknown_labels = Column(JSON)
    missing_required_labels = Column(JSON)
    breaking_change_detected = Column(Boolean, default=False)
    compatibility_status = Column(String(20))   # COMPATIBLE/BREAKING/UNKNOWN

    upload = relationship("Upload", back_populates="schema_audit")


# ── Company (SCD Type 2) ─────────────────────────────────────────────────────

class Company(Base):
    """Slowly Changing Dimension Type 2 — one row per version per tenant."""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    company_name = Column(String(255), nullable=False)
    sector = Column(String(255))
    country = Column(String(255))
    currency = Column(String(10))
    accounting_principles = Column(String(50))
    business_year_end = Column(String(20))

    version = Column(Integer, nullable=False, default=1)
    valid_from = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    valid_to = Column(DateTime(timezone=True))
    is_current = Column(Boolean, nullable=False, default=True)

    tenant = relationship("Tenant", back_populates="companies")
    snapshots = relationship("Snapshot", back_populates="company")
    change_log = relationship("CompanyChangeLog", back_populates="company")

    __table_args__ = (
        Index("ix_companies_tenant_name_current", "tenant_id", "company_name", "is_current"),
        Index("ix_companies_name_version", "company_name", "version"),
    )


# ── Company Change Log ────────────────────────────────────────────────────────

class CompanyChangeLog(Base):
    """Field-level audit trail for SCD Type 2 transitions."""
    __tablename__ = "company_change_log"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    company_name = Column(String(255), nullable=False)
    changed_at = Column(DateTime(timezone=True), default=utcnow)
    changed_by_upload_id = Column(Integer, ForeignKey("uploads.id"))
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text)
    new_value = Column(Text)

    company = relationship("Company", back_populates="change_log")


# ── Snapshot (Analytical Fact) ────────────────────────────────────────────────

class Snapshot(Base):
    """
    One analytical fact record per processed upload.
    Partitioning strategy (PostgreSQL): RANGE on snapshot_at by year.
    Each year is a child table: snapshots_2023, snapshots_2024, etc.
    This keeps query plans efficient on large datasets (prune by year).
    """
    __tablename__ = "snapshots"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    upload_id = Column(Integer, ForeignKey("uploads.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    snapshot_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    version = Column(Integer, nullable=False)

    industry_risks = Column(JSON, nullable=False, default=list)
    methodologies = Column(JSON, nullable=False, default=list)
    segmentation_criteria = Column(String(100))

    business_risk_profile = Column(String(20))
    blended_industry_risk = Column(String(20))
    competitive_positioning = Column(String(20))
    market_share = Column(String(20))
    diversification = Column(String(20))
    operating_profitability = Column(String(20))
    sector_specific_factor_1 = Column(String(20))
    sector_specific_factor_2 = Column(String(20))

    financial_risk_profile = Column(String(20))
    leverage = Column(String(20))
    interest_cover = Column(String(20))
    cash_flow_cover = Column(String(20))
    liquidity_adjustment = Column(String(30))

    content_fingerprint = Column(String(64))

    # Derived rating (computed from BRP + FRP + notch adjustments)
    anchor_rating = Column(String(10))           # before liquidity notch
    final_rating = Column(String(10))            # after notch adjustment
    rating_methodology_version = Column(String(20))

    # Quality KPIs separate from pass/fail
    metric_coverage_pct = Column(Numeric(5, 2))  # % of credit_metric cells non-null

    # Approval workflow
    approval_status = Column(String(20), default="pending")  # pending/approved/rejected

    upload = relationship("Upload", back_populates="snapshots")
    company = relationship("Company", back_populates="snapshots")
    credit_metrics = relationship(
        "CreditMetric", back_populates="snapshot", cascade="all, delete-orphan"
    )
    provenance = relationship(
        "FieldProvenance", back_populates="snapshot", cascade="all, delete-orphan"
    )
    approvals = relationship(
        "SnapshotApproval", back_populates="snapshot", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_snapshots_tenant_company_version", "tenant_id", "company_id", "version"),
        Index("ix_snapshots_at", "snapshot_at"),
        Index("ix_snapshots_fingerprint", "content_fingerprint"),
    )


# ── Credit Metrics ────────────────────────────────────────────────────────────

class CreditMetric(Base):
    """
    Annual time-series metrics.
    Partitioning strategy: RANGE on metric_year.
    """
    __tablename__ = "credit_metrics"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=False)
    metric_year = Column(Integer, nullable=False)

    ebitda_interest_cover = Column(Numeric(18, 6))
    debt_ebitda = Column(Numeric(18, 6))
    ffo_debt = Column(Numeric(18, 6))
    loan_value = Column(Numeric(18, 6))
    focf_debt = Column(Numeric(18, 6))
    liquidity = Column(Numeric(18, 6))

    # Actuals vs estimates: True if metric_year > submission year
    is_estimate = Column(Boolean, default=False)
    # Stale data: True if values match a prior year exactly (likely copy-paste)
    is_stale = Column(Boolean, default=False)

    snapshot = relationship("Snapshot", back_populates="credit_metrics")

    __table_args__ = (
        UniqueConstraint("snapshot_id", "metric_year", name="uq_metric_snapshot_year"),
        Index("ix_credit_metrics_year", "metric_year"),
    )


# ── Field Provenance (Cell-level Lineage / OpenLineage) ───────────────────────

class FieldProvenance(Base):
    """
    Cell-level lineage: every field → exact Excel cell.
    Also used to emit OpenLineage column-level lineage facets.
    """
    __tablename__ = "field_provenance"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=False)
    field_name = Column(String(100), nullable=False)
    source_sheet = Column(String(50), nullable=False, default="MASTER")
    source_row = Column(Integer)
    source_col = Column(String(5))
    raw_value = Column(Text)
    extracted_value = Column(Text)

    snapshot = relationship("Snapshot", back_populates="provenance")

    __table_args__ = (
        Index("ix_provenance_snapshot_field", "snapshot_id", "field_name"),
    )


# ── Data Quality Issues ───────────────────────────────────────────────────────

class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"

    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey("uploads.id"), nullable=False)
    field_name = Column(String(100))
    issue_type = Column(String(50))
    issue_detail = Column(Text)
    severity = Column(String(20))
    source_sheet = Column(String(50))
    source_row = Column(Integer)
    source_col = Column(String(5))
    # GE expectation reference
    expectation_type = Column(String(100))   # e.g. "expect_column_values_to_be_in_set"
    expectation_kwargs = Column(JSON)

    upload = relationship("Upload", back_populates="quality_issues")


# ── Ingestion Watermarks ──────────────────────────────────────────────────────

class IngestionWatermark(Base):
    """Per-tenant per-company watermark for incremental loading."""
    __tablename__ = "ingestion_watermarks"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    company_name = Column(String(255), nullable=False)
    last_processed_at = Column(DateTime(timezone=True), nullable=False)
    last_upload_id = Column(Integer, ForeignKey("uploads.id"))
    last_business_key = Column(String(64))

    __table_args__ = (
        UniqueConstraint("tenant_id", "company_name", name="uq_watermark_tenant_company"),
        Index("ix_watermarks_tenant_company", "tenant_id", "company_name"),
    )


# ── Validation Rules (Externalized Rule Engine) ───────────────────────────────

class ValidationRule(Base):
    """Runtime-configurable validation rules per tenant."""
    __tablename__ = "validation_rules"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    field_name = Column(String(100), nullable=False)
    rule_type = Column(String(50), nullable=False)  # required/allowed_values/range/regex/ge_expectation
    params = Column(JSON)
    severity = Column(String(20), nullable=False, default="error")
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_validation_rules_tenant_field", "tenant_id", "field_name", "is_active"),)


# ── Metadata Catalog ──────────────────────────────────────────────────────────

class MetadataCatalog(Base):
    """
    Lightweight data catalog with OpenLineage-compatible lineage upstream.
    Compatible with DataHub/Collibra export via REST.
    """
    __tablename__ = "metadata_catalog"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    table_name = Column(String(100), nullable=False)
    field_name = Column(String(100), nullable=False)
    data_type = Column(String(50))
    description = Column(Text)
    source_label = Column(String(200))
    source_sheet = Column(String(50))
    pii = Column(Boolean, default=False)
    owner = Column(String(100))
    sla_freshness_hours = Column(Integer)
    lineage_upstream = Column(JSON)   # OpenLineage ColumnLineageDatasetFacet format
    data_classification = Column(String(50))  # public/internal/confidential/restricted
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "table_name", "field_name", name="uq_catalog_tenant_table_field"),
    )


# ── FX Rates (Currency Normalisation) ─────────────────────────────────────────

class FXRate(Base):
    """
    Daily FX rates anchored to USD (base currency).
    Enables cross-currency peer benchmarking (EUR Company A vs CHF Company B).
    Sourced from ECB Statistical Data Warehouse or similar.
    """
    __tablename__ = "fx_rates"

    id = Column(Integer, primary_key=True)
    from_ccy = Column(String(3), nullable=False)
    to_ccy = Column(String(3), nullable=False, default="USD")
    rate_date = Column(Date, nullable=False)
    rate = Column(Numeric(18, 8), nullable=False)
    source = Column(String(50), default="ECB")
    created_at = Column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("from_ccy", "to_ccy", "rate_date", name="uq_fx_triple"),
        Index("ix_fx_lookup", "from_ccy", "to_ccy", "rate_date"),
    )


# ── Rating Migration (Regulatory Transition Matrix) ───────────────────────────

class RatingMigration(Base):
    """
    Records every rating change for a company.
    Aggregated into ESMA/Basel-compliant migration matrices.
    """
    __tablename__ = "rating_migrations"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    company_name = Column(String(255), nullable=False)
    from_snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=True)
    to_snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=False)
    from_rating = Column(String(10))    # NULL = first rating ever
    to_rating = Column(String(10), nullable=False)
    notches_moved = Column(Integer)     # negative = downgrade, positive = upgrade
    direction = Column(String(20))      # downgrade/upgrade/affirmation/new
    migrated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_migrations_company_at", "company_name", "migrated_at"),
        Index("ix_migrations_tenant_direction", "tenant_id", "direction", "migrated_at"),
    )


# ── Analyst Assignment & Sign-off Workflow ────────────────────────────────────

class AnalystAssignment(Base):
    """
    Which analyst is responsible for which company.
    Roles: primary (lead analyst), secondary (back-up), reviewer (committee).
    """
    __tablename__ = "analyst_assignments"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    company_name = Column(String(255), nullable=False)
    analyst_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False)
    role = Column(String(20), nullable=False)   # primary / secondary / reviewer
    assigned_at = Column(DateTime(timezone=True), default=utcnow)
    unassigned_at = Column(DateTime(timezone=True))
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        Index("ix_assignments_company", "company_name", "is_active"),
        Index("ix_assignments_analyst", "analyst_key_id", "is_active"),
    )


class SnapshotApproval(Base):
    """
    Sign-off audit trail for each rating snapshot.
    A snapshot may require multiple approvals (primary + reviewer).
    """
    __tablename__ = "snapshot_approvals"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("snapshots.id"), nullable=False)
    approved_by_key_id = Column(Integer, ForeignKey("api_keys.id"))
    approval_role = Column(String(20))  # analyst / committee / chief_credit_officer
    decision = Column(String(20), nullable=False)  # approved / rejected / pending
    decision_at = Column(DateTime(timezone=True), default=utcnow)
    comment = Column(Text)

    snapshot = relationship("Snapshot", back_populates="approvals")

    __table_args__ = (
        Index("ix_approvals_snapshot", "snapshot_id"),
    )


# ── Rating Methodology (Decision Matrix) ──────────────────────────────────────

class RatingMethodology(Base):
    """
    Versioned BRP×FRP → anchor rating combination matrix.
    Allows runtime updates to the rating derivation logic.
    """
    __tablename__ = "rating_methodologies"

    id = Column(Integer, primary_key=True)
    version = Column(String(20), nullable=False, unique=True)
    combination_matrix = Column(JSON, nullable=False)  # {"B+_C": "CCC+", ...}
    notch_scale = Column(JSON, nullable=False)         # rating ladder order
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date)
    is_active = Column(Boolean, default=True)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), default=utcnow)
