"""
Great Expectations integration.

Uses GE's PandasDataset API to validate extracted MasterData.
Expectations run after extraction and supplement the static rule validator
with statistical and distributional checks.

Returns GE results converted to our internal DataQualityIssue dict format.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from great_expectations.dataset import PandasDataset
    _GE_AVAILABLE = True
except ImportError:
    _GE_AVAILABLE = False
    log.warning("great_expectations not installed — GE validation disabled")

VALID_RATINGS = [
    "AAA","AA+","AA","AA-","A+","A","A-",
    "BBB+","BBB","BBB-","BB+","BB","BB-",
    "B+","B","B-","CCC+","CCC","CCC-","CC","C","D","SD","",
]
VALID_CURRENCIES = ["EUR","USD","GBP","CHF","JPY","AUD","CAD","SEK","NOK","DKK"]
RATING_COLUMNS = [
    "business_risk_profile", "blended_industry_risk", "competitive_positioning",
    "market_share", "diversification", "operating_profitability",
    "financial_risk_profile", "leverage", "interest_cover", "cash_flow_cover",
]


def _issue(field: str, expectation_type: str, detail: str, kwargs: dict) -> dict:
    return {
        "field_name": field,
        "issue_type": "ge_expectation_failed",
        "issue_detail": detail,
        "severity": "warning",
        "expectation_type": expectation_type,
        "expectation_kwargs": kwargs,
    }


def validate_with_ge(data) -> list[dict]:
    """
    Run Great Expectations validation on MasterData scalar fields.
    Returns list of issue dicts compatible with DataQualityIssue model.
    """
    if not _GE_AVAILABLE:
        return []

    import pandas as pd

    row = {
        "rated_entity": data.rated_entity or None,
        "sector": data.sector or None,
        "currency": data.currency or None,
        "country": data.country or None,
        "accounting_principles": data.accounting_principles or None,
        "business_year_end": data.business_year_end or None,
        **{col: (getattr(data, col) or "") for col in RATING_COLUMNS},
    }
    ds = PandasDataset(pd.DataFrame([row]))
    issues: list[dict] = []

    def check(result, field: str, expectation_type: str, kwargs: dict):
        if not result.success:
            issues.append(_issue(
                field, expectation_type,
                f"{expectation_type} failed: {result.result}",
                kwargs,
            ))

    # Required non-null fields
    for col in ["rated_entity", "sector", "currency", "country",
                "accounting_principles", "business_year_end"]:
        kwargs = {"column": col}
        check(ds.expect_column_values_to_not_be_null(col), col,
              "expect_column_values_to_not_be_null", kwargs)

    # Currency must be in known set
    kwargs = {"column": "currency", "value_set": VALID_CURRENCIES}
    check(ds.expect_column_values_to_be_in_set("currency", VALID_CURRENCIES),
          "currency", "expect_column_values_to_be_in_set", kwargs)

    # Rating columns must be in known notation set
    for col in RATING_COLUMNS:
        kwargs = {"column": col, "value_set": VALID_RATINGS}
        check(ds.expect_column_values_to_be_in_set(col, VALID_RATINGS, mostly=0.8),
              col, "expect_column_values_to_be_in_set", kwargs)

    # Company name length 1–255
    check(
        ds.expect_column_value_lengths_to_be_between("rated_entity", min_value=1, max_value=255),
        "rated_entity", "expect_column_value_lengths_to_be_between",
        {"column": "rated_entity", "min_value": 1, "max_value": 255},
    )

    return issues
