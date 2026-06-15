from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class IndustrySegmentOut(BaseModel):
    risk: str
    score: str
    weight: float


class CreditMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metric_year: int
    ebitda_interest_cover: float | None
    debt_ebitda: float | None
    ffo_debt: float | None
    loan_value: float | None
    focf_debt: float | None
    liquidity: float | None
    is_estimate: bool | None = None
    is_stale: bool | None = None


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_name: str
    sector: str | None
    country: str | None
    currency: str | None
    accounting_principles: str | None
    business_year_end: str | None
    version: int
    valid_from: datetime
    valid_to: datetime | None
    is_current: bool


class CompanyChangeLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_name: str
    changed_at: datetime
    changed_by_upload_id: int | None
    field_name: str
    old_value: str | None
    new_value: str | None


class FieldProvenanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_name: str
    source_sheet: str
    source_row: int | None
    source_col: str | None
    raw_value: str | None
    extracted_value: str | None


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    upload_id: int
    company_id: int
    snapshot_at: datetime
    version: int
    content_fingerprint: str | None
    industry_risks: list[dict[str, Any]]
    methodologies: list[str]
    business_risk_profile: str | None
    blended_industry_risk: str | None
    competitive_positioning: str | None
    market_share: str | None
    diversification: str | None
    operating_profitability: str | None
    financial_risk_profile: str | None
    leverage: str | None
    interest_cover: str | None
    cash_flow_cover: str | None
    liquidity_adjustment: str | None
    anchor_rating: str | None = None
    final_rating: str | None = None
    metric_coverage_pct: float | None = None
    approval_status: str | None = None
    credit_metrics: list[CreditMetricOut] = []


class SnapshotDetailOut(SnapshotOut):
    provenance: list[FieldProvenanceOut] = []


class UploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    uploaded_at: datetime
    company_name: str | None
    status: str
    error_detail: str | None
    quality_score: float | None
    business_key: str | None
    reprocessed_from_id: int | None


class UploadDetailOut(UploadOut):
    quality_issues: list[dict[str, Any]] = []
    snapshots: list[SnapshotOut] = []


class UploadStatsOut(BaseModel):
    total_uploads: int
    processed: int
    failed: int
    skipped_no_delta: int
    companies_tracked: int
    avg_quality_score: float | None
    latest_upload_at: datetime | None


class CompareOut(BaseModel):
    as_of_date: datetime
    companies: list[SnapshotOut]


class QualityIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    upload_id: int
    field_name: str | None
    issue_type: str | None
    issue_detail: str | None
    severity: str | None
    source_sheet: str | None
    source_row: int | None
    source_col: str | None


class SchemaAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    upload_id: int
    observed_at: datetime
    labels_seen: list[str] | None
    unknown_labels: list[str] | None
    missing_required_labels: list[str] | None
    breaking_change_detected: bool


class ValidationRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    field_name: str
    rule_type: str
    params: Any
    severity: str
    description: str | None
    is_active: bool
    created_at: datetime


class ValidationRuleCreate(BaseModel):
    field_name: str
    rule_type: str
    params: Any = None
    severity: str = "error"
    description: str | None = None


class PipelineRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_at: datetime
    files_attempted: int
    files_processed: int
    files_skipped: int
    files_failed: int
    duration_seconds: float | None
    status: str


class ReprocessRequest(BaseModel):
    reason: str


class MetadataCatalogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    table_name: str
    field_name: str
    data_type: str | None
    description: str | None
    source_label: str | None
    source_sheet: str | None
    pii: bool
    owner: str | None
    sla_freshness_hours: int | None
    lineage_upstream: list[dict] | None
