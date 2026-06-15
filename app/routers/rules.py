from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ValidationRule
from app.schemas import ValidationRuleCreate, ValidationRuleOut

router = APIRouter(prefix="/rules", tags=["Rule Engine"])


@router.get("", response_model=list[ValidationRuleOut])
def list_rules(db: Session = Depends(get_db)):
    """All validation rules — active and inactive."""
    return db.query(ValidationRule).order_by(ValidationRule.field_name).all()


@router.post("", response_model=ValidationRuleOut, status_code=201)
def create_rule(body: ValidationRuleCreate, db: Session = Depends(get_db)):
    """Add a runtime validation rule — no code deploy required."""
    rule = ValidationRule(**body.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/{rule_id}/deactivate", response_model=ValidationRuleOut)
def deactivate_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(ValidationRule, rule_id)
    if not rule:
        raise HTTPException(404, f"Rule {rule_id} not found")
    rule.is_active = False
    db.commit()
    db.refresh(rule)
    return rule
