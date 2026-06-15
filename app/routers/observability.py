"""
Observability endpoints:
  GET /metrics       — Prometheus-compatible text exposition
  GET /lineage/{id}  — Full upload-to-cell lineage for an upload
  GET /catalog       — Metadata catalog entries
"""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MetadataCatalog, Snapshot, Upload
from app.schemas import MetadataCatalogOut

router = APIRouter(tags=["Observability"])


@router.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(db: Session = Depends(get_db)):
    """Prometheus-compatible metrics exposition."""
    total = db.query(func.count(Upload.id)).scalar() or 0
    processed = db.query(func.count(Upload.id)).filter_by(status="processed").scalar() or 0
    failed = db.query(func.count(Upload.id)).filter_by(status="failed").scalar() or 0
    delta_skipped = db.query(func.count(Upload.id)).filter_by(status="skipped_no_delta").scalar() or 0
    avg_quality = db.query(func.avg(Upload.quality_score)).filter_by(status="processed").scalar()
    total_snapshots = db.query(func.count(Snapshot.id)).scalar() or 0

    avg_q = float(avg_quality) if avg_quality is not None else 0.0

    return (
        "# HELP uploads_total Total ingestion attempts\n"
        f"uploads_total {total}\n"
        "# HELP uploads_processed_total Successfully processed uploads\n"
        f"uploads_processed_total {processed}\n"
        "# HELP uploads_failed_total Failed uploads\n"
        f"uploads_failed_total {failed}\n"
        "# HELP uploads_delta_skipped_total Uploads skipped due to no data change\n"
        f"uploads_delta_skipped_total {delta_skipped}\n"
        "# HELP upload_quality_score_avg Average data quality score (0-100)\n"
        f"upload_quality_score_avg {avg_q:.2f}\n"
        "# HELP snapshots_total Total analytical snapshots stored\n"
        f"snapshots_total {total_snapshots}\n"
    )


@router.get("/catalog", response_model=list[MetadataCatalogOut])
def list_catalog(db: Session = Depends(get_db)):
    """Data catalog — describes every logical field in the pipeline."""
    return db.query(MetadataCatalog).order_by(MetadataCatalog.table_name, MetadataCatalog.field_name).all()
