"""
Schema Registry — versioned field manifests with compatibility validation.

Compatibility modes (mirrors Confluent Schema Registry):
  BACKWARD  — new schema can read data produced by old schema (fields can be added)
  FORWARD   — old schema can read data produced by new schema (fields can be removed)
  FULL      — both: only optional field additions/removals allowed
  NONE      — no compatibility check

Schema format:
  {
    "label": {
      "type": "str|float|int|list",
      "required": true|false,
      "description": "..."
    }
  }
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.extractor import KNOWN_LABELS


# Canonical v1 manifest derived from KNOWN_LABELS + type hints
V1_MANIFEST: dict = {
    "Rated entity":                        {"type": "str",   "required": True},
    "CorporateSector":                     {"type": "str",   "required": True},
    "Reporting Currency/Units":            {"type": "str",   "required": True},
    "Country of origin":                   {"type": "str",   "required": True},
    "Accounting principles":               {"type": "str",   "required": True},
    "End of business year":                {"type": "str",   "required": True},
    "Rating methodologies applied":        {"type": "list",  "required": False},
    "Industry risk":                       {"type": "list",  "required": True},
    "Industry risk score":                 {"type": "list",  "required": True},
    "Industry weight":                     {"type": "list",  "required": True},
    "Segmentation criteria":               {"type": "str",   "required": False},
    "Business risk profile":               {"type": "str",   "required": True},
    "(Blended) Industry risk profile":     {"type": "str",   "required": False},
    "Competitive Positioning":             {"type": "str",   "required": False},
    "Market share":                        {"type": "str",   "required": False},
    "Diversification":                     {"type": "str",   "required": False},
    "Operating profitability":             {"type": "str",   "required": False},
    "Sector/company-specific factors (1)": {"type": "str",   "required": False},
    "Sector/company-specific factors (2)": {"type": "str",   "required": False},
    "Financial risk profile":              {"type": "str",   "required": True},
    "Leverage":                            {"type": "str",   "required": False},
    "Interest cover":                      {"type": "str",   "required": False},
    "Cash flow cover":                     {"type": "str",   "required": False},
    "Liquidity":                           {"type": "str",   "required": False},
    "[Scope Credit Metrics]":              {"type": "marker","required": False},
    "Scope-adjusted EBITDA interest cover":{"type": "float", "required": False},
    "Scope-adjusted debt/EBITDA":          {"type": "float", "required": False},
    "Scope-adjusted FFO/debt":             {"type": "float", "required": False},
    "Scope-adjusted loan/value":           {"type": "float", "required": False},
    "Scope-adjusted FOCF/debt":            {"type": "float", "required": False},
}


def _manifest_fingerprint(manifest: dict) -> str:
    canonical = json.dumps(manifest, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed_v1(db: Session):
    """Insert the v1 schema manifest if not already present."""
    from app.models import SchemaVersion
    exists = db.query(SchemaVersion).filter_by(subject="MASTER", version=1).first()
    if not exists:
        db.add(SchemaVersion(
            subject="MASTER",
            version=1,
            schema_str=V1_MANIFEST,
            compatibility="BACKWARD",
            fingerprint=_manifest_fingerprint(V1_MANIFEST),
            notes="Initial MASTER sheet schema — baseline for drift detection",
        ))
        db.commit()


def get_latest_version(db: Session, subject: str = "MASTER"):
    """Return the highest active SchemaVersion for a subject."""
    from app.models import SchemaVersion
    return (
        db.query(SchemaVersion)
        .filter_by(subject=subject)
        .filter(SchemaVersion.deprecated_at.is_(None))
        .order_by(SchemaVersion.version.desc())
        .first()
    )


def check_compatibility(
    new_labels: set[str],
    db: Session,
    subject: str = "MASTER",
    mode: str = "BACKWARD",
) -> dict:
    """
    Check if a new set of labels is compatible with the registered schema.

    Returns:
      {
        "compatible": bool,
        "mode": str,
        "added_labels": [...],      # in new, not in registered
        "removed_labels": [...],    # in registered, not in new
        "removed_required": [...],  # removed labels that were required=True
        "breaking": bool,
      }
    """
    schema_ver = get_latest_version(db, subject)
    if not schema_ver:
        return {"compatible": True, "mode": mode, "breaking": False,
                "added_labels": [], "removed_labels": [], "removed_required": []}

    registered: dict = schema_ver.schema_str
    registered_labels = set(registered.keys())

    added = list(new_labels - registered_labels)
    removed = list(registered_labels - new_labels)
    removed_required = [
        lbl for lbl in removed
        if registered.get(lbl, {}).get("required", False)
    ]

    breaking = False
    if mode in ("BACKWARD", "FULL") and removed_required:
        breaking = True
    if mode in ("FORWARD", "FULL") and added:
        # Added fields in FORWARD mode means old consumers may not know them
        breaking = False  # added fields are safe for BACKWARD; only removed breaks

    compatible = not breaking

    return {
        "compatible": compatible,
        "mode": mode,
        "schema_version": schema_ver.version,
        "added_labels": added,
        "removed_labels": removed,
        "removed_required": removed_required,
        "breaking": breaking,
    }


def register_new_version(
    db: Session,
    new_manifest: dict,
    subject: str = "MASTER",
    notes: str = "",
) -> dict:
    """
    Register a new schema version after compatibility check.
    Returns the compatibility result + newly created version number.
    """
    from app.models import SchemaVersion

    latest = get_latest_version(db, subject)
    new_version_num = (latest.version + 1) if latest else 1
    compat_mode = latest.compatibility if latest else "BACKWARD"

    compat = check_compatibility(set(new_manifest.keys()), db, subject, compat_mode)

    sv = SchemaVersion(
        subject=subject,
        version=new_version_num,
        schema_str=new_manifest,
        compatibility=compat_mode,
        fingerprint=_manifest_fingerprint(new_manifest),
        breaking_change=compat["breaking"],
        notes=notes,
    )
    db.add(sv)
    db.commit()
    db.refresh(sv)

    return {"schema_version": new_version_num, **compat}
