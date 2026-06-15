"""
Change Data Capture (CDC) strategy.

Pattern: Transactional Outbox
  1. Every INSERT/UPDATE/DELETE on core tables writes a CDCEvent row in the SAME
     transaction — atomic with the data change.
  2. A relay worker (run separately or via pg_cron) reads unpublished events and
     forwards them to consumers (Kafka topic, webhook, pg NOTIFY, etc.).
  3. Once published, `published_at` is set — events are never deleted (audit trail).

This avoids dual-write problems: if the app crashes after writing data but before
emitting to Kafka, the outbox row persists and will be picked up on restart.

Usage:
  cdc.emit(db, "INSERT", "snapshots", snapshot.id, after_state={...})
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Table → domain aggregate mapping
_AGGREGATE_MAP = {
    "uploads": "Upload",
    "companies": "Company",
    "snapshots": "Snapshot",
    "credit_metrics": "CreditMetric",
}


def emit(
    db: Session,
    event_type: str,        # INSERT | UPDATE | DELETE
    table_name: str,
    record_id: int,
    before_state: dict | None = None,
    after_state: dict | None = None,
    changed_fields: list[str] | None = None,
    tenant_id: int | None = None,
):
    """
    Write a CDC event to the outbox table within the current transaction.
    Call before db.commit() so the event is atomic with the data change.
    """
    from app.models import CDCEvent

    aggregate_type = _AGGREGATE_MAP.get(table_name)
    db.add(CDCEvent(
        event_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        event_type=event_type,
        table_name=table_name,
        record_id=record_id,
        before_state=before_state,
        after_state=after_state,
        changed_fields=changed_fields,
        occurred_at=_utcnow(),
        aggregate_type=aggregate_type,
        aggregate_id=record_id,
    ))


def _serialize(obj: Any) -> dict | None:
    """Convert an ORM row to a dict for CDC state capture. Skips binary blobs."""
    if obj is None:
        return None
    from sqlalchemy import LargeBinary
    result = {}
    for col in obj.__table__.columns:
        if isinstance(col.type, LargeBinary):
            result[col.name] = "<binary>"
            continue
        val = getattr(obj, col.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        elif hasattr(val, "__class__") and val.__class__.__name__ == "Decimal":
            val = float(val)
        result[col.name] = val
    return result


def emit_insert(db: Session, obj, tenant_id: int | None = None):
    emit(db, "INSERT", obj.__tablename__, obj.id,
         after_state=_serialize(obj), tenant_id=tenant_id)


def emit_update(db: Session, obj, changed_fields: list[str], before: dict,
                tenant_id: int | None = None):
    emit(db, "UPDATE", obj.__tablename__, obj.id,
         before_state=before, after_state=_serialize(obj),
         changed_fields=changed_fields, tenant_id=tenant_id)


def emit_delete(db: Session, obj, tenant_id: int | None = None):
    emit(db, "DELETE", obj.__tablename__, obj.id,
         before_state=_serialize(obj), tenant_id=tenant_id)


def publish_pending(db: Session, batch_size: int = 100) -> int:
    """
    Mark pending CDC events as published (simulates relay worker).
    In production: replace the body with actual Kafka/webhook publish logic.
    Returns count of events published.
    """
    from app.models import CDCEvent
    from sqlalchemy import and_

    pending = (
        db.query(CDCEvent)
        .filter(CDCEvent.published_at.is_(None))
        .order_by(CDCEvent.occurred_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)   # safe for concurrent workers
        .all()
    )

    now = _utcnow()
    for event in pending:
        # TODO: publish to Kafka/webhook here
        event.published_at = now

    if pending:
        db.commit()

    return len(pending)
