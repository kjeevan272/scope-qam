from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Company, DataQualityIssue, Snapshot, Upload
from app.pipeline import reprocess_upload
from app.schemas import (
    ReprocessRequest, SchemaAuditOut, UploadDetailOut, UploadOut, UploadStatsOut,
)

router = APIRouter(prefix="/uploads", tags=["Uploads"])


@router.get("", response_model=list[UploadOut])
def list_uploads(db: Session = Depends(get_db)):
    return db.query(Upload).order_by(Upload.uploaded_at.desc()).all()


@router.get("/stats", response_model=UploadStatsOut)
def upload_stats(db: Session = Depends(get_db)):
    total = db.query(func.count(Upload.id)).scalar()
    processed = db.query(func.count(Upload.id)).filter_by(status="processed").scalar()
    failed = db.query(func.count(Upload.id)).filter_by(status="failed").scalar()
    skipped_no_delta = db.query(func.count(Upload.id)).filter_by(status="skipped_no_delta").scalar()
    companies = db.query(func.count(Company.id)).filter_by(is_current=True).scalar()
    latest = db.query(func.max(Upload.uploaded_at)).scalar()
    avg_quality = db.query(func.avg(Upload.quality_score)).filter_by(status="processed").scalar()
    return UploadStatsOut(
        total_uploads=total,
        processed=processed,
        failed=failed,
        skipped_no_delta=skipped_no_delta,
        companies_tracked=companies,
        avg_quality_score=float(avg_quality) if avg_quality is not None else None,
        latest_upload_at=latest,
    )


@router.get("/{upload_id}/file")
def download_file(upload_id: int, db: Session = Depends(get_db)):
    """Return original .xlsm binary."""
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(404, f"Upload {upload_id} not found")
    if not upload.raw_file:
        raise HTTPException(404, "No file content stored for this upload")
    return Response(
        content=bytes(upload.raw_file),
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        headers={"Content-Disposition": f'attachment; filename="{upload.filename}"'},
    )


@router.get("/{upload_id}/details", response_model=UploadDetailOut)
def upload_details(upload_id: int, db: Session = Depends(get_db)):
    upload = (
        db.query(Upload)
        .options(
            selectinload(Upload.snapshots).selectinload(Snapshot.credit_metrics),
            selectinload(Upload.quality_issues),
        )
        .filter(Upload.id == upload_id)
        .first()
    )
    if not upload:
        raise HTTPException(404, f"Upload {upload_id} not found")

    issues = [
        {
            "field_name": i.field_name,
            "issue_type": i.issue_type,
            "issue_detail": i.issue_detail,
            "severity": i.severity,
            "source_sheet": i.source_sheet,
            "source_row": i.source_row,
            "source_col": i.source_col,
        }
        for i in upload.quality_issues
    ]

    return UploadDetailOut(
        id=upload.id,
        filename=upload.filename,
        uploaded_at=upload.uploaded_at,
        company_name=upload.company_name,
        status=upload.status,
        error_detail=upload.error_detail,
        quality_score=float(upload.quality_score) if upload.quality_score is not None else None,
        business_key=upload.business_key,
        reprocessed_from_id=upload.reprocessed_from_id,
        quality_issues=issues,
        snapshots=upload.snapshots,
    )


@router.get("/{upload_id}/schema-audit", response_model=SchemaAuditOut)
def upload_schema_audit(upload_id: int, db: Session = Depends(get_db)):
    """Schema drift report for a specific upload."""
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(404, f"Upload {upload_id} not found")
    if not upload.schema_audit:
        raise HTTPException(404, "No schema audit record for this upload")
    return upload.schema_audit


@router.post("/{upload_id}/reprocess")
def reprocess(upload_id: int, body: ReprocessRequest, db: Session = Depends(get_db)):
    """Re-run extraction and load for an existing upload using its stored raw file."""
    upload = db.get(Upload, upload_id)
    if not upload:
        raise HTTPException(404, f"Upload {upload_id} not found")
    try:
        result = reprocess_upload(upload_id, body.reason)
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))
