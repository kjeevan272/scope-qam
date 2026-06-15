"""
Regulatory and analytical exports.

ESMA CEREP XML: required by EU Credit Rating Agency Regulation (Article 11).
  Format: simplified XML matching the ESMA submission template structure.
  Real submissions require CEREP technical instructions XSD — this module
  produces the structural skeleton; legal sign-off is required before filing.

CSV: flat snapshot export for analyst Excel workflows.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring


def snapshot_to_esma_xml(snapshot, company, credit_metrics: list) -> bytes:
    """
    Build an ESMA CEREP-style XML element for a single rating submission.
    """
    root = Element("CEREPSubmission", {
        "xmlns": "urn:esma:cerep:1-0",
        "schemaVersion": "1.0",
    })

    header = SubElement(root, "Header")
    SubElement(header, "SubmissionDate").text = datetime.utcnow().isoformat()
    SubElement(header, "SnapshotId").text = str(snapshot.id)
    SubElement(header, "RatingAgency").text = "ScopeQAM"

    entity = SubElement(root, "RatedEntity")
    SubElement(entity, "Name").text = company.company_name
    SubElement(entity, "Country").text = company.country or ""
    SubElement(entity, "Sector").text = company.sector or ""
    SubElement(entity, "Currency").text = company.currency or ""

    rating = SubElement(root, "RatingAction")
    SubElement(rating, "AnchorRating").text = snapshot.anchor_rating or ""
    SubElement(rating, "FinalRating").text = snapshot.final_rating or ""
    SubElement(rating, "BusinessRiskProfile").text = snapshot.business_risk_profile or ""
    SubElement(rating, "FinancialRiskProfile").text = snapshot.financial_risk_profile or ""
    SubElement(rating, "LiquidityAdjustment").text = snapshot.liquidity_adjustment or ""
    SubElement(rating, "RatingDate").text = snapshot.snapshot_at.isoformat()
    SubElement(rating, "MethodologyVersion").text = snapshot.rating_methodology_version or ""

    methodologies = SubElement(root, "Methodologies")
    for m in (snapshot.methodologies or []):
        SubElement(methodologies, "Methodology").text = m

    industry = SubElement(root, "IndustrySegments")
    for seg in (snapshot.industry_risks or []):
        s = SubElement(industry, "Segment")
        SubElement(s, "Risk").text = str(seg.get("risk", ""))
        SubElement(s, "Score").text = str(seg.get("score", ""))
        SubElement(s, "Weight").text = str(seg.get("weight", ""))

    metrics_el = SubElement(root, "CreditMetrics")
    for m in credit_metrics:
        ym = SubElement(metrics_el, "Year", {"value": str(m.metric_year),
                                              "isEstimate": str(bool(m.is_estimate)).lower()})
        for f in ("ebitda_interest_cover", "debt_ebitda", "ffo_debt",
                   "loan_value", "focf_debt", "liquidity"):
            v = getattr(m, f)
            if v is not None:
                SubElement(ym, f).text = str(float(v))

    quality = SubElement(root, "DataQuality")
    SubElement(quality, "QualityScore").text = (
        str(float(snapshot.upload.quality_score)) if snapshot.upload.quality_score else "0"
    )
    SubElement(quality, "MetricCoverage").text = (
        str(float(snapshot.metric_coverage_pct)) if snapshot.metric_coverage_pct else "0"
    )
    SubElement(quality, "ApprovalStatus").text = snapshot.approval_status or "pending"

    return tostring(root, encoding="utf-8", xml_declaration=True)


def snapshots_to_csv(snapshots: list) -> str:
    """
    Flat CSV export of snapshot core fields for analyst Excel workflows.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "snapshot_id", "company_id", "company_name", "snapshot_at",
        "anchor_rating", "final_rating",
        "business_risk_profile", "financial_risk_profile", "liquidity_adjustment",
        "leverage", "interest_cover", "cash_flow_cover",
        "quality_score", "metric_coverage_pct", "approval_status",
    ])
    for s in snapshots:
        writer.writerow([
            s.id,
            s.company_id,
            s.company.company_name if s.company else "",
            s.snapshot_at.isoformat() if s.snapshot_at else "",
            s.anchor_rating or "",
            s.final_rating or "",
            s.business_risk_profile or "",
            s.financial_risk_profile or "",
            s.liquidity_adjustment or "",
            s.leverage or "",
            s.interest_cover or "",
            s.cash_flow_cover or "",
            float(s.upload.quality_score) if s.upload and s.upload.quality_score else "",
            float(s.metric_coverage_pct) if s.metric_coverage_pct else "",
            s.approval_status or "",
        ])
    return buf.getvalue()
