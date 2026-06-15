"""
Validation framework for extracted MasterData.

Two layers:
  1. Static rules hardcoded here — always run, zero latency
  2. DB-driven rule engine — loads active ValidationRule rows at runtime,
     allowing analysts to add constraints without code deploys

Quality scoring: 100 - (errors × 10) - (warnings × 2), floored at 0.
"""
from __future__ import annotations

import re

from app.extractor import MasterData, VALID_RATING_NOTATIONS

KNOWN_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "JPY", "AUD", "CAD", "SEK", "NOK", "DKK"}
NOTCH_PATTERN_WORDS = {"notch", "notches"}

RATING_FIELDS = {
    "business_risk_profile", "blended_industry_risk", "competitive_positioning",
    "market_share", "diversification", "operating_profitability",
    "sector_specific_factor_1", "financial_risk_profile",
    "leverage", "interest_cover", "cash_flow_cover",
}


def _is_valid_rating(value: str) -> bool:
    return value in VALID_RATING_NOTATIONS or not value


def _is_notch_adjustment(value: str) -> bool:
    return any(w in value.lower() for w in NOTCH_PATTERN_WORDS)


def _apply_db_rules(data: MasterData, db_rules: list[dict]) -> list[dict]:
    """Apply externalized validation rules loaded from the DB."""
    issues: list[dict] = []

    for rule in db_rules:
        field = rule["field_name"]
        rule_type = rule["rule_type"]
        params = rule.get("params") or {}
        severity = rule.get("severity", "error")
        value = str(getattr(data, field, "") or "")

        match rule_type:
            case "required":
                if not value:
                    issues.append({
                        "field_name": field,
                        "issue_type": "missing",
                        "issue_detail": f"Required field '{field}' is empty",
                        "severity": severity,
                    })
            case "allowed_values":
                if value and value not in params:
                    issues.append({
                        "field_name": field,
                        "issue_type": "invalid_value",
                        "issue_detail": f"'{value}' not in allowed values {params}",
                        "severity": severity,
                    })
            case "range":
                try:
                    fval = float(value)
                    lo, hi = params.get("min"), params.get("max")
                    if (lo is not None and fval < lo) or (hi is not None and fval > hi):
                        issues.append({
                            "field_name": field,
                            "issue_type": "out_of_range",
                            "issue_detail": f"'{value}' outside range [{lo}, {hi}]",
                            "severity": severity,
                        })
                except (ValueError, TypeError):
                    pass
            case "regex":
                pattern = params if isinstance(params, str) else params.get("pattern", "")
                if value and not re.fullmatch(pattern, value):
                    issues.append({
                        "field_name": field,
                        "issue_type": "pattern_mismatch",
                        "issue_detail": f"'{value}' does not match pattern '{pattern}'",
                        "severity": severity,
                    })

    return issues


def validate(data: MasterData, db_rules: list[dict] | None = None) -> list[dict]:
    """
    Returns list of issue dicts.
    db_rules: pre-loaded list of active ValidationRule dicts from DB (optional).
    """
    issues: list[dict] = list(data.quality_issues)  # carry over extraction warnings

    def add(field: str, issue_type: str, detail: str, severity: str = "error"):
        issues.append({
            "field_name": field,
            "issue_type": issue_type,
            "issue_detail": detail,
            "severity": severity,
        })

    # ── Static Rule 1: required fields ───────────────────────────────────
    required = {
        "rated_entity": data.rated_entity,
        "sector": data.sector,
        "currency": data.currency,
        "country": data.country,
        "accounting_principles": data.accounting_principles,
        "business_year_end": data.business_year_end,
    }
    for field, val in required.items():
        if not val:
            add(field, "missing", f"Required field '{field}' is empty or absent")

    # ── Static Rule 2: currency ───────────────────────────────────────────
    if data.currency and data.currency not in KNOWN_CURRENCIES:
        add("currency", "invalid_value", f"Unknown currency '{data.currency}'", "warning")

    # ── Static Rule 3: rating notations ──────────────────────────────────
    for field in RATING_FIELDS:
        val = getattr(data, field, "")
        if val and not _is_valid_rating(val):
            add(field, "invalid_value", f"'{val}' is not a recognised rating notation", "warning")

    if data.liquidity_adjustment and not (
        _is_notch_adjustment(data.liquidity_adjustment)
        or _is_valid_rating(data.liquidity_adjustment)
    ):
        add("liquidity_adjustment", "invalid_value",
            f"Unexpected value '{data.liquidity_adjustment}'", "warning")

    # ── Static Rule 4: industry weights sum to 1.0 ────────────────────────
    if data.industry_segments:
        total = sum(s.weight for s in data.industry_segments)
        if abs(total - 1.0) > 0.01:
            add("industry_weight", "out_of_range",
                f"Industry weights sum to {total:.4f}, expected 1.0")

    # ── Static Rule 5: at least one industry segment ──────────────────────
    if not data.industry_segments:
        add("industry_risks", "missing", "No industry risk segments found")

    # ── Static Rule 6: at least one methodology ───────────────────────────
    if not data.methodologies:
        add("methodologies", "missing", "No rating methodologies listed", "warning")

    # ── Static Rule 7: credit metrics presence ────────────────────────────
    if not data.credit_metrics:
        add("credit_metrics", "missing", "No [Scope Credit Metrics] data found", "warning")

    # ── Dynamic DB rules ──────────────────────────────────────────────────
    if db_rules:
        issues.extend(_apply_db_rules(data, db_rules))

    return issues


def compute_quality_score(issues: list[dict]) -> float:
    """0–100 quality score. Errors cost 10 pts, warnings cost 2 pts."""
    errors = sum(1 for i in issues if i.get("severity") == "error")
    warnings = sum(1 for i in issues if i.get("severity") == "warning")
    return max(0.0, 100.0 - (errors * 10) - (warnings * 2))
