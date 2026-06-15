"""
Analyst assignment & rating approval workflow.

Use case: a rating agency assigns analysts to specific companies and requires
formal sign-off before a snapshot is considered final. The platform tracks
approval state per snapshot — pending / approved / rejected.
"""
from datetime import datetime, timezone
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AnalystAssignment, ApiKey, Snapshot, SnapshotApproval
from app.security import get_current_key, require_role

router = APIRouter(prefix="/workflow", tags=["Workflow"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class AssignmentCreate(BaseModel):
    company_name: str
    analyst_key_id: int
    role: str = "primary"
    tenant_id: int | None = None


class AssignmentOut(BaseModel):
    id: int
    company_name: str
    analyst_key_id: int
    role: str
    assigned_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


class ApprovalDecision(BaseModel):
    decision: str          # approved / rejected
    approval_role: str = "analyst"
    comment: str | None = None


class ApprovalOut(BaseModel):
    id: int
    snapshot_id: int
    approved_by_key_id: int | None
    approval_role: str | None
    decision: str
    decision_at: datetime
    comment: str | None

    class Config:
        from_attributes = True


# ── Analyst Assignments ───────────────────────────────────────────────────────

@router.post("/assignments", response_model=AssignmentOut, status_code=201)
def create_assignment(body: AssignmentCreate, db: Session = Depends(get_db),
                       _=Depends(require_role("admin"))):
    analyst = db.get(ApiKey, body.analyst_key_id)
    if not analyst:
        raise HTTPException(404, f"Analyst key {body.analyst_key_id} not found")
    if body.role not in ("primary", "secondary", "reviewer"):
        raise HTTPException(422, "role must be primary, secondary, or reviewer")

    assignment = AnalystAssignment(
        tenant_id=body.tenant_id,
        company_name=body.company_name,
        analyst_key_id=body.analyst_key_id,
        role=body.role,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment


@router.get("/assignments", response_model=list[AssignmentOut])
def list_assignments(
    company_name: str | None = None,
    analyst_key_id: int | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(AnalystAssignment).filter_by(is_active=True)
    if company_name:
        q = q.filter_by(company_name=company_name)
    if analyst_key_id:
        q = q.filter_by(analyst_key_id=analyst_key_id)
    return q.order_by(AnalystAssignment.company_name).all()


@router.delete("/assignments/{assignment_id}")
def unassign(assignment_id: int, db: Session = Depends(get_db),
              _=Depends(require_role("admin"))):
    a = db.get(AnalystAssignment, assignment_id)
    if not a:
        raise HTTPException(404, "Not found")
    a.is_active = False
    a.unassigned_at = datetime.now(timezone.utc)
    db.commit()
    return {"unassigned": True}


# ── Snapshot Approvals ────────────────────────────────────────────────────────

@router.post("/snapshots/{snapshot_id}/approve", response_model=ApprovalOut, status_code=201)
def approve_snapshot(
    snapshot_id: int,
    body: ApprovalDecision,
    db: Session = Depends(get_db),
    key=Depends(get_current_key),
):
    snap = db.get(Snapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, f"Snapshot {snapshot_id} not found")
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(422, "decision must be 'approved' or 'rejected'")

    approval = SnapshotApproval(
        snapshot_id=snapshot_id,
        approved_by_key_id=key.id if key else None,
        approval_role=body.approval_role,
        decision=body.decision,
        comment=body.comment,
    )
    db.add(approval)

    # Update snapshot summary state
    snap.approval_status = body.decision
    db.commit()
    db.refresh(approval)
    return approval


@router.get("/snapshots/{snapshot_id}/approvals", response_model=list[ApprovalOut])
def list_approvals(snapshot_id: int, db: Session = Depends(get_db)):
    snap = db.get(Snapshot, snapshot_id)
    if not snap:
        raise HTTPException(404, f"Snapshot {snapshot_id} not found")
    return (
        db.query(SnapshotApproval)
        .filter_by(snapshot_id=snapshot_id)
        .order_by(SnapshotApproval.decision_at.desc())
        .all()
    )


@router.get("/pending-approvals")
def pending_approvals(db: Session = Depends(get_db)):
    """List snapshots awaiting approval — analyst worklist."""
    snaps = (
        db.query(Snapshot)
        .filter(Snapshot.approval_status == "pending")
        .order_by(Snapshot.snapshot_at.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "snapshot_id": s.id,
            "company_id": s.company_id,
            "snapshot_at": s.snapshot_at.isoformat(),
            "final_rating": s.final_rating,
            "anchor_rating": s.anchor_rating,
            "quality_score": float(s.upload.quality_score) if s.upload and s.upload.quality_score else None,
        }
        for s in snaps
    ]
