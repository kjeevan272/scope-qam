"""
ETL pipeline: Extract → Validate (static + GE) → Transform → Load

Production features:
  - Content-hash idempotency (SHA-256 of raw bytes)
  - Business-hash idempotency (SHA-256 of company + period, soft detection)
  - Delta detection via content fingerprint — skips unchanged snapshots
  - Incremental loading via per-company/tenant watermarks
  - SCD Type 2 with SELECT FOR UPDATE — concurrency-safe
  - Field-level company change log on every SCD transition
  - Cell-level provenance (MASTER sheet row/col for every field)
  - Schema drift detection + schema audit per upload
  - Schema registry compatibility check
  - Static + DB-driven validation rule engine
  - Great Expectations validation suite
  - Quality scoring (0–100)
  - CDC outbox events on INSERT/UPDATE
  - OpenLineage START/COMPLETE/FAIL events per file
  - Queue-based ingestion support
  - Replay/reprocess with lineage back-reference
  - Per-file transactions with exponential backoff retry
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db import SessionLocal
from app.extractor import MasterData, extract
from app.models import (
    Company, CompanyChangeLog, CreditMetric, DataQualityIssue,
    FieldProvenance, IngestionWatermark, PipelineRun, RatingMigration,
    SchemaAudit, Snapshot, Upload, ValidationRule,
)
from app.validator import compute_quality_score, validate
from app import cdc, openlineage, analytics
from app.expectations import validate_with_ge
from app.schema_registry import check_compatibility
from app.rating_engine import derive_final_rating, migration_direction, notches_between

log = get_logger(__name__)

SCD_FIELDS = ("sector", "country", "currency", "accounting_principles", "business_year_end")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Retry ─────────────────────────────────────────────────────────────────────

def _with_retry(fn, retries: int = 4):
    delay = 2
    for attempt in range(retries + 1):
        try:
            return fn()
        except OperationalError as exc:
            if attempt == retries:
                raise
            log.warning("DB transient error (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, retries, exc, delay)
            time.sleep(delay)
            delay *= 2


# ── Idempotency ───────────────────────────────────────────────────────────────

def _content_hash_exists(db: Session, file_hash: str, tenant_id: int | None) -> bool:
    q = db.query(Upload).filter_by(file_hash=file_hash, status="processed")
    if tenant_id is not None:
        q = q.filter_by(tenant_id=tenant_id)
    return q.first() is not None


def _business_key_exists(db: Session, business_key: str, company_name: str,
                          tenant_id: int | None) -> bool:
    q = db.query(Upload).filter(
        Upload.business_key == business_key,
        Upload.company_name == company_name,
        Upload.status == "processed",
    )
    if tenant_id is not None:
        q = q.filter(Upload.tenant_id == tenant_id)
    return q.first() is not None


# ── Delta Detection ───────────────────────────────────────────────────────────

def _latest_fingerprint(db: Session, company_name: str, tenant_id: int | None) -> str | None:
    q = (
        db.query(Snapshot.content_fingerprint)
        .join(Upload, Snapshot.upload_id == Upload.id)
        .filter(Upload.company_name == company_name, Upload.status == "processed")
    )
    if tenant_id is not None:
        q = q.filter(Upload.tenant_id == tenant_id)
    row = q.order_by(Snapshot.snapshot_at.desc()).first()
    return row[0] if row else None


def _previous_snapshot(db: Session, company_name: str, tenant_id: int | None) -> Snapshot | None:
    """Return most-recent prior snapshot for this company (for delta detection / migration)."""
    q = (
        db.query(Snapshot)
        .join(Upload, Snapshot.upload_id == Upload.id)
        .filter(Upload.company_name == company_name, Upload.status == "processed")
    )
    if tenant_id is not None:
        q = q.filter(Upload.tenant_id == tenant_id)
    return q.order_by(Snapshot.snapshot_at.desc()).first()


# ── SCD Type 2 (concurrency-safe) ────────────────────────────────────────────

def _get_or_create_company(
    db: Session, data: MasterData, snapshot_at: datetime,
    upload_id: int, tenant_id: int | None,
) -> Company:
    q = db.query(Company).filter_by(company_name=data.rated_entity, is_current=True)
    if tenant_id is not None:
        q = q.filter_by(tenant_id=tenant_id)
    current = q.with_for_update().first()

    new_attrs = {k: getattr(data, k) for k in SCD_FIELDS}

    if current is None:
        company = Company(
            tenant_id=tenant_id,
            company_name=data.rated_entity,
            version=1,
            valid_from=snapshot_at,
            **new_attrs,
        )
        db.add(company)
        db.flush()
        cdc.emit_insert(db, company, tenant_id=tenant_id)
        return company

    changed_fields = [k for k in SCD_FIELDS if getattr(current, k) != new_attrs[k]]
    if not changed_fields:
        return current

    before = cdc._serialize(current)
    current.valid_to = snapshot_at
    current.is_current = False

    new_company = Company(
        tenant_id=tenant_id,
        company_name=data.rated_entity,
        version=current.version + 1,
        valid_from=snapshot_at,
        **new_attrs,
    )
    db.add(new_company)
    db.flush()

    cdc.emit_update(db, new_company, changed_fields, before, tenant_id=tenant_id)

    for field_name in changed_fields:
        db.add(CompanyChangeLog(
            tenant_id=tenant_id,
            company_id=new_company.id,
            company_name=data.rated_entity,
            changed_at=snapshot_at,
            changed_by_upload_id=upload_id,
            field_name=field_name,
            old_value=str(getattr(current, field_name)),
            new_value=str(new_attrs[field_name]),
        ))

    return new_company


# ── Watermark ─────────────────────────────────────────────────────────────────

def _update_watermark(db: Session, data: MasterData, upload_id: int,
                      processed_at: datetime, tenant_id: int | None):
    q = db.query(IngestionWatermark).filter_by(company_name=data.rated_entity)
    if tenant_id is not None:
        q = q.filter_by(tenant_id=tenant_id)
    wm = q.first()
    if wm is None:
        wm = IngestionWatermark(tenant_id=tenant_id, company_name=data.rated_entity)
        db.add(wm)
    wm.last_processed_at = processed_at
    wm.last_upload_id = upload_id
    wm.last_business_key = data.business_key


# ── Schema Audit ──────────────────────────────────────────────────────────────

def _write_schema_audit(db: Session, upload_id: int, data: MasterData,
                         compat_result: dict | None = None):
    required_labels = {
        "Rated entity", "CorporateSector", "Reporting Currency/Units",
        "Country of origin", "Accounting principles", "End of business year",
    }
    missing = [lbl for lbl in required_labels if lbl not in data.labels_seen]

    compat_status = "UNKNOWN"
    schema_version_id = None
    if compat_result:
        compat_status = "BREAKING" if compat_result.get("breaking") else "COMPATIBLE"

    db.add(SchemaAudit(
        upload_id=upload_id,
        labels_seen=data.labels_seen,
        unknown_labels=data.unknown_labels,
        missing_required_labels=missing,
        breaking_change_detected=len(missing) > 0,
        compatibility_status=compat_status,
    ))


# ── DB Rule Engine ────────────────────────────────────────────────────────────

def _load_db_rules(db: Session, tenant_id: int | None) -> list[dict]:
    q = db.query(ValidationRule).filter_by(is_active=True)
    if tenant_id is not None:
        from sqlalchemy import or_
        q = q.filter(or_(ValidationRule.tenant_id == tenant_id,
                          ValidationRule.tenant_id.is_(None)))
    return [
        {"field_name": r.field_name, "rule_type": r.rule_type,
         "params": r.params, "severity": r.severity}
        for r in q.all()
    ]


# ── Core File Loader ──────────────────────────────────────────────────────────

def _load_file(
    db: Session,
    path: Path,
    run: PipelineRun,
    tenant_id: int | None = None,
    reprocess_from_id: int | None = None,
    reprocess_reason: str | None = None,
) -> str:
    log.info("Processing %s (tenant=%s)", path.name, tenant_id)
    ol_run_id = str(uuid.uuid4())

    # ── Extract ───────────────────────────────────────────────────────────
    try:
        data: MasterData = extract(path)
    except Exception as exc:
        log.error("Extraction failed for %s: %s", path.name, exc)
        openlineage.emit_fail(ol_run_id, str(path), str(exc))
        db.add(Upload(
            tenant_id=tenant_id,
            pipeline_run_id=run.id,
            filename=path.name,
            file_hash="",
            raw_file=path.read_bytes(),
            company_name=None,
            status="failed",
            error_detail=str(exc),
            quality_score=0,
            openlineage_run_id=ol_run_id,
        ))
        db.flush()
        return "failed"

    openlineage.emit_start(ol_run_id, str(path), data.file_hash)

    # ── Content-hash idempotency (hard gate) ──────────────────────────────
    if not reprocess_from_id and _content_hash_exists(db, data.file_hash, tenant_id):
        log.info("Skipping %s — content hash already processed", path.name)
        return "skipped"

    # ── Business-hash duplicate detection (soft: warn, don't gate) ────────
    if _business_key_exists(db, data.business_key, data.rated_entity, tenant_id):
        log.warning("Business key exists for %s/%s — re-submission accepted",
                    data.rated_entity, data.business_year_end)

    # ── Schema compatibility check ────────────────────────────────────────
    compat_result = check_compatibility(set(data.labels_seen), db)

    # ── Capture previous snapshot for delta/migration/methodology analysis ─
    prev_snap = _previous_snapshot(db, data.rated_entity, tenant_id)

    # ── Validate (static rules + DB rules + Great Expectations) ──────────
    db_rules = _load_db_rules(db, tenant_id)
    issues = validate(data, db_rules=db_rules)
    ge_issues = validate_with_ge(data)
    issues.extend(ge_issues)

    # ── Business-aware quality signals (stale, methodology, weight drift) ─
    issues.extend(analytics.detect_stale_metrics(data.credit_metrics))
    if prev_snap is not None:
        issues.extend(analytics.detect_methodology_change(
            prev_snap.methodologies or [], data.methodologies
        ))
        issues.extend(analytics.detect_weight_drift(
            prev_snap.industry_risks or [],
            [{"risk": s.risk, "score": s.score, "weight": s.weight}
             for s in data.industry_segments],
        ))
    issues.extend(analytics.detect_metric_cliffs(data.credit_metrics))

    # Mark forecast vs actual on extracted credit metrics
    analytics.mark_estimates(data.credit_metrics)

    quality_score = compute_quality_score(issues)
    coverage = analytics.coverage_pct(data.credit_metrics)

    # ── Derive ratings ────────────────────────────────────────────────────
    rating = derive_final_rating(
        data.business_risk_profile,
        data.financial_risk_profile,
        data.liquidity_adjustment,
    )

    snapshot_at = _utcnow()

    # ── Register upload ───────────────────────────────────────────────────
    upload = Upload(
        tenant_id=tenant_id,
        pipeline_run_id=run.id,
        filename=path.name,
        file_hash=data.file_hash,
        business_key=data.business_key,
        raw_file=data.raw_bytes,
        company_name=data.rated_entity,
        status="processed",
        quality_score=quality_score,
        reprocessed_from_id=reprocess_from_id,
        reprocess_reason=reprocess_reason,
        openlineage_run_id=ol_run_id,
    )
    db.add(upload)
    db.flush()

    cdc.emit_insert(db, upload, tenant_id=tenant_id)

    for issue in issues:
        db.add(DataQualityIssue(
            upload_id=upload.id,
            field_name=issue.get("field_name"),
            issue_type=issue.get("issue_type"),
            issue_detail=issue.get("issue_detail"),
            severity=issue.get("severity"),
            source_sheet=issue.get("source_sheet"),
            source_row=issue.get("source_row"),
            source_col=issue.get("source_col"),
            expectation_type=issue.get("expectation_type"),
            expectation_kwargs=issue.get("expectation_kwargs"),
        ))

    _write_schema_audit(db, upload.id, data, compat_result)

    # ── SCD Type 2 ────────────────────────────────────────────────────────
    company = _get_or_create_company(db, data, snapshot_at, upload.id, tenant_id)

    # ── Delta detection ───────────────────────────────────────────────────
    prev_fingerprint = _latest_fingerprint(db, data.rated_entity, tenant_id)
    if prev_fingerprint and prev_fingerprint == data.content_fingerprint:
        upload.status = "skipped_no_delta"
        log.info("No delta for %s — skipping snapshot", data.rated_entity)
        _update_watermark(db, data, upload.id, snapshot_at, tenant_id)
        return "skipped_no_delta"

    # ── Snapshot version ──────────────────────────────────────────────────
    q = db.query(Upload).filter_by(company_name=data.rated_entity, status="processed")
    if tenant_id is not None:
        q = q.filter_by(tenant_id=tenant_id)
    prior_count = q.count()

    snapshot = Snapshot(
        tenant_id=tenant_id,
        upload_id=upload.id,
        company_id=company.id,
        snapshot_at=snapshot_at,
        version=prior_count,
        content_fingerprint=data.content_fingerprint,
        industry_risks=[
            {"risk": s.risk, "score": s.score, "weight": s.weight}
            for s in data.industry_segments
        ],
        methodologies=data.methodologies,
        segmentation_criteria=data.segmentation_criteria,
        business_risk_profile=data.business_risk_profile,
        blended_industry_risk=data.blended_industry_risk,
        competitive_positioning=data.competitive_positioning,
        market_share=data.market_share,
        diversification=data.diversification,
        operating_profitability=data.operating_profitability,
        sector_specific_factor_1=data.sector_specific_factor_1,
        sector_specific_factor_2=data.sector_specific_factor_2,
        financial_risk_profile=data.financial_risk_profile,
        leverage=data.leverage,
        interest_cover=data.interest_cover,
        cash_flow_cover=data.cash_flow_cover,
        liquidity_adjustment=data.liquidity_adjustment,
        anchor_rating=rating["anchor_rating"],
        final_rating=rating["final_rating"],
        metric_coverage_pct=coverage,
        approval_status="pending",
    )
    db.add(snapshot)
    db.flush()

    cdc.emit_insert(db, snapshot, tenant_id=tenant_id)

    # ── Record rating migration if final_rating changed ───────────────────
    if rating["final_rating"]:
        prev_final = prev_snap.final_rating if prev_snap else None
        if prev_final != rating["final_rating"]:
            db.add(RatingMigration(
                tenant_id=tenant_id,
                company_name=data.rated_entity,
                from_snapshot_id=prev_snap.id if prev_snap else None,
                to_snapshot_id=snapshot.id,
                from_rating=prev_final,
                to_rating=rating["final_rating"],
                notches_moved=(
                    notches_between(prev_final, rating["final_rating"])
                    if prev_final else 0
                ),
                direction=migration_direction(prev_final, rating["final_rating"]),
                migrated_at=snapshot_at,
            ))

    # ── Credit metrics (with actuals/estimates flag + stale marking) ─────
    for metric in data.credit_metrics:
        db.add(CreditMetric(
            snapshot_id=snapshot.id,
            metric_year=metric.year,
            ebitda_interest_cover=metric.ebitda_interest_cover,
            debt_ebitda=metric.debt_ebitda,
            ffo_debt=metric.ffo_debt,
            loan_value=metric.loan_value,
            focf_debt=metric.focf_debt,
            liquidity=metric.liquidity,
            is_estimate=bool(getattr(metric, "is_estimate", False)),
            is_stale=bool(getattr(metric, "is_stale", False)),
        ))

    # ── Cell-level provenance ─────────────────────────────────────────────
    for p in data.provenance:
        db.add(FieldProvenance(
            snapshot_id=snapshot.id,
            field_name=p.field_name,
            source_sheet=p.source_sheet,
            source_row=p.source_row,
            source_col=p.source_col,
            raw_value=p.raw_value,
            extracted_value=p.extracted_value,
        ))

    # ── Watermark ─────────────────────────────────────────────────────────
    _update_watermark(db, data, upload.id, snapshot_at, tenant_id)

    # ── OpenLineage COMPLETE ──────────────────────────────────────────────
    prov_dicts = [
        {"field_name": p.field_name, "source_sheet": p.source_sheet,
         "source_row": p.source_row, "source_col": p.source_col}
        for p in data.provenance
    ]
    openlineage.emit_complete(
        ol_run_id, str(path), data.file_hash, prov_dicts, quality_score
    )

    log.info(
        "Loaded %s → company='%s' v%d, snapshot v%d, score=%.0f, %d metrics, %d issues",
        path.name, data.rated_entity, company.version,
        snapshot.version, quality_score,
        len(data.credit_metrics), len(issues),
    )
    return "processed"


# ── Public Entry Points ───────────────────────────────────────────────────────

def run_pipeline(
    data_dir: str | None = None,
    tenant_id: int | None = None,
) -> dict:
    directory = Path(data_dir or settings.data_dir)
    files = sorted(directory.glob("*.xlsm"))
    log.info("Pipeline starting — %d .xlsm files in %s (tenant=%s)",
             len(files), directory, tenant_id)

    start = time.monotonic()
    counts: dict[str, int] = {"processed": 0, "skipped": 0, "skipped_no_delta": 0, "failed": 0}

    db: Session = SessionLocal()
    try:
        ol_run_id = str(uuid.uuid4())
        run = PipelineRun(
            tenant_id=tenant_id,
            files_attempted=len(files),
            status="running",
            openlineage_run_id=ol_run_id,
        )
        db.add(run)
        db.flush()

        for path in files:
            def load_one(p=path):
                result = _load_file(db, p, run, tenant_id=tenant_id)
                db.commit()
                return result

            try:
                result = _with_retry(load_one)
                counts[result] = counts.get(result, 0) + 1
            except Exception as exc:
                db.rollback()
                counts["failed"] += 1
                log.error("Unrecoverable error on %s: %s", path.name, exc)

        run.files_processed = counts["processed"]
        run.files_skipped = counts["skipped"] + counts.get("skipped_no_delta", 0)
        run.files_failed = counts["failed"]
        run.duration_seconds = round(time.monotonic() - start, 3)
        run.status = "completed" if counts["failed"] == 0 else "partial"
        db.commit()

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    summary = {"status": run.status, "duration_seconds": float(run.duration_seconds), **counts}
    log.info("Pipeline finished: %s", summary)
    return summary


def run_pipeline_from_queue(tenant_id: int | None = None) -> dict:
    """
    Worker entry point: claim tasks from IngestionTask queue and process them.
    Run in a separate process or thread; designed for concurrent workers.
    """
    from app.queue import claim_next, complete, fail_task
    import tempfile

    db: Session = SessionLocal()
    processed = 0
    try:
        run = PipelineRun(tenant_id=tenant_id, files_attempted=0, status="running")
        db.add(run)
        db.flush()

        while True:
            task = claim_next(db)
            if task is None:
                break

            run.files_attempted += 1
            db.commit()

            try:
                path = Path(task.file_path)
                if not path.exists():
                    raise FileNotFoundError(f"{path} not found")

                def load_one():
                    result = _load_file(db, path, run, tenant_id=tenant_id)
                    db.commit()
                    return result

                result = _with_retry(load_one)
                complete(db, task.id)
                db.commit()
                processed += 1
            except Exception as exc:
                db.rollback()
                fail_task(db, task.id, str(exc))
                db.commit()

        run.files_processed = processed
        run.status = "completed"
        db.commit()

    finally:
        db.close()

    return {"processed": processed}


def reprocess_upload(upload_id: int, reason: str) -> dict:
    """Re-run processing for a specific upload using its stored raw file."""
    import tempfile

    db: Session = SessionLocal()
    try:
        original = db.get(Upload, upload_id)
        if not original:
            raise ValueError(f"Upload {upload_id} not found")
        if not original.raw_file:
            raise ValueError(f"Upload {upload_id} has no stored raw file")

        tenant_id = original.tenant_id
        run = PipelineRun(tenant_id=tenant_id, files_attempted=1, status="running")
        db.add(run)
        db.flush()

        with tempfile.NamedTemporaryFile(suffix=original.filename, delete=False) as tmp:
            tmp.write(bytes(original.raw_file))
            tmp_path = Path(tmp.name)

        try:
            def load_one():
                result = _load_file(
                    db, tmp_path, run,
                    tenant_id=tenant_id,
                    reprocess_from_id=upload_id,
                    reprocess_reason=reason,
                )
                db.commit()
                return result

            result = _with_retry(load_one)
        finally:
            tmp_path.unlink(missing_ok=True)

        run.files_processed = 1 if result == "processed" else 0
        run.status = "completed" if result != "failed" else "failed"
        db.commit()

        return {"status": result, "original_upload_id": upload_id}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
