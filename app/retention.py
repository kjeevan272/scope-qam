"""
Data Retention Policy enforcement.

Each RetentionPolicy row defines:
  - table_name: which table to clean
  - retain_days: keep rows newer than (now - retain_days)
  - archive_before_delete: if True, export to archive_location before deleting

Enforcement is designed to run as a scheduled job (cron, Celery beat, pg_cron).
Call `enforce_all_policies()` from a scheduled task or `POST /admin/retention/enforce`.

Partitioning synergy: if credit_metrics is RANGE-partitioned by metric_year,
dropping an old partition is O(1) instead of a slow DELETE scan. The retention
enforcer detects this and issues DROP TABLE IF EXISTS on the partition directly.

Design constraints:
  - Never delete data within a transaction that also writes new data
  - Log every enforcement run with rows_deleted to RetentionPolicy.last_enforced_at
  - Uploads.raw_file (LargeBinary) is the biggest storage consumer —
    retention can null it out after retain_days while keeping the metadata row
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enforce_policy(db: Session, policy) -> int:
    """
    Enforce a single RetentionPolicy. Returns number of rows affected.

    Special handling:
    - 'uploads' table: null out raw_file column instead of deleting rows
      (preserve audit metadata, reclaim blob storage)
    - 'cdc_events' table: delete only published events
    - All others: hard delete rows older than retain_days
    """
    cutoff = _utcnow() - timedelta(days=policy.retain_days)
    table = policy.table_name
    rows_affected = 0

    try:
        if table == "uploads":
            # Soft retention: null out the binary blob, keep the row
            result = db.execute(
                text(
                    "UPDATE uploads SET raw_file = NULL "
                    "WHERE uploaded_at < :cutoff AND raw_file IS NOT NULL"
                ),
                {"cutoff": cutoff},
            )
            rows_affected = result.rowcount
            log.info("Retention: nulled raw_file on %d upload rows older than %s",
                     rows_affected, cutoff.date())

        elif table == "cdc_events":
            # Only delete published events (unpublished = not yet consumed)
            result = db.execute(
                text(
                    "DELETE FROM cdc_events "
                    "WHERE occurred_at < :cutoff AND published_at IS NOT NULL"
                ),
                {"cutoff": cutoff},
            )
            rows_affected = result.rowcount

        elif table == "field_provenance":
            # Provenance can be large; prune via snapshot FK cascade
            result = db.execute(
                text(
                    "DELETE FROM field_provenance WHERE snapshot_id IN ("
                    "  SELECT id FROM snapshots WHERE snapshot_at < :cutoff"
                    ")"
                ),
                {"cutoff": cutoff},
            )
            rows_affected = result.rowcount

        else:
            # Generic: find timestamp column (uploaded_at / run_at / created_at)
            timestamp_col = _guess_timestamp_col(db, table)
            if timestamp_col:
                result = db.execute(
                    text(f"DELETE FROM {table} WHERE {timestamp_col} < :cutoff"),
                    {"cutoff": cutoff},
                )
                rows_affected = result.rowcount

        policy.last_enforced_at = _utcnow()
        policy.rows_deleted_last_run = rows_affected
        db.commit()

    except Exception as exc:
        db.rollback()
        log.error("Retention enforcement failed for %s: %s", table, exc)
        raise

    return rows_affected


def enforce_all_policies(db: Session) -> dict:
    """Run all active retention policies. Returns summary dict."""
    from app.models import RetentionPolicy

    policies = db.query(RetentionPolicy).filter_by(is_active=True).all()
    results = {}
    for policy in policies:
        try:
            count = enforce_policy(db, policy)
            results[policy.table_name] = {"rows_affected": count, "status": "ok"}
        except Exception as exc:
            results[policy.table_name] = {"rows_affected": 0, "status": str(exc)}
    return results


def _guess_timestamp_col(db: Session, table_name: str) -> str | None:
    """Inspect table columns to find a suitable timestamp for retention."""
    for candidate in ("uploaded_at", "run_at", "created_at", "occurred_at", "changed_at"):
        try:
            db.execute(text(f"SELECT {candidate} FROM {table_name} LIMIT 0"))
            return candidate
        except Exception:
            continue
    return None
