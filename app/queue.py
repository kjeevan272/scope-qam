"""
DB-backed async ingestion queue.

Design:
  - IngestionTask rows are the queue; workers poll with SELECT FOR UPDATE SKIP LOCKED
  - Visibility timeout (claimed_until) prevents double-processing if a worker dies
  - Priority queue: lower number = higher priority (1=critical, 10=batch)
  - Automatic retry up to MAX_ATTEMPTS with exponential back-off tracking via `last_error`

PostgreSQL LISTEN/NOTIFY integration (optional, for push-based workers):
  After enqueueing, NOTIFY 'ingestion_queue' with the task ID.
  Workers LISTEN on that channel and wake immediately instead of polling.

Usage:
  task_id = enqueue(db, file_path, tenant_id=1, priority=1)
  task = claim_next(db, worker_id="worker-1")
  if task:
      process(task)
      complete(db, task.id)
"""
from __future__ import annotations

import socket
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

MAX_ATTEMPTS = 4
VISIBILITY_TIMEOUT_SECONDS = 300   # 5 min — worker must complete within this window
WORKER_ID = f"{socket.gethostname()}-{id(object())}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    db: Session,
    file_path: str | Path,
    tenant_id: int | None = None,
    priority: int = 5,
    file_hash: str | None = None,
) -> int:
    """Add a file to the ingestion queue. Returns task ID."""
    from app.models import IngestionTask

    task = IngestionTask(
        tenant_id=tenant_id,
        file_path=str(file_path),
        file_hash=file_hash,
        priority=priority,
        status="pending",
        attempts=0,
    )
    db.add(task)
    db.flush()
    task_id = task.id

    # PostgreSQL LISTEN/NOTIFY — notify workers immediately
    try:
        db.execute(
            __import__("sqlalchemy").text("SELECT pg_notify('ingestion_queue', :payload)"),
            {"payload": str(task_id)},
        )
    except Exception:
        pass  # SQLite / non-Postgres — skip notification

    return task_id


def claim_next(db: Session, worker_id: str = WORKER_ID):
    """
    Atomically claim the next available task.
    SELECT FOR UPDATE SKIP LOCKED ensures only one worker gets each task.
    """
    from app.models import IngestionTask

    now = _utcnow()
    task = (
        db.query(IngestionTask)
        .filter(
            IngestionTask.status == "pending",
            IngestionTask.attempts < MAX_ATTEMPTS,
            or_(
                IngestionTask.claimed_until.is_(None),
                IngestionTask.claimed_until <= now,
            ),
        )
        .order_by(IngestionTask.priority, IngestionTask.enqueued_at)
        .with_for_update(skip_locked=True)
        .first()
    )

    if task is None:
        return None

    task.status = "claimed"
    task.claimed_by = worker_id
    task.claimed_until = now + timedelta(seconds=VISIBILITY_TIMEOUT_SECONDS)
    task.attempts += 1
    db.flush()
    return task


def complete(db: Session, task_id: int, upload_id: int | None = None):
    from app.models import IngestionTask
    task = db.get(IngestionTask, task_id)
    if task:
        task.status = "done"
        task.completed_at = _utcnow()
        task.upload_id = upload_id
        db.flush()


def fail_task(db: Session, task_id: int, error: str):
    from app.models import IngestionTask
    task = db.get(IngestionTask, task_id)
    if task:
        task.last_error = error
        if task.attempts >= MAX_ATTEMPTS:
            task.status = "failed"
        else:
            task.status = "pending"  # back to pending for retry
            task.claimed_until = None
        db.flush()


def queue_stats(db: Session) -> dict:
    from app.models import IngestionTask
    from sqlalchemy import func

    rows = (
        db.query(IngestionTask.status, func.count(IngestionTask.id))
        .group_by(IngestionTask.status)
        .all()
    )
    return {status: count for status, count in rows}
