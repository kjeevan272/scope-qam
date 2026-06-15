"""Unit tests for the validation framework."""
import pytest
from pathlib import Path

from app.extractor import extract, IndustrySegment
from app.validator import validate

DATA_DIR = Path(__file__).parent.parent / "data"


def test_valid_file_has_no_errors():
    data = extract(DATA_DIR / "corporates_B_1.xlsm")
    issues = validate(data)
    errors = [i for i in issues if i["severity"] == "error"]
    assert errors == [], f"Unexpected errors: {errors}"


def test_weights_not_summing_to_one_is_error():
    data = extract(DATA_DIR / "corporates_B_1.xlsm")
    # Corrupt weights
    data.industry_segments[0].weight = 0.5
    data.industry_segments[1].weight = 0.7  # sum = 1.2
    issues = validate(data)
    weight_errors = [i for i in issues if i["field_name"] == "industry_weight"]
    assert any(i["severity"] == "error" for i in weight_errors)


def test_missing_rated_entity_is_error():
    data = extract(DATA_DIR / "corporates_A_1.xlsm")
    data.rated_entity = ""
    issues = validate(data)
    assert any(
        i["field_name"] == "rated_entity" and i["severity"] == "error"
        for i in issues
    )


def test_unknown_currency_is_warning():
    data = extract(DATA_DIR / "corporates_A_1.xlsm")
    data.currency = "XYZ"
    issues = validate(data)
    assert any(
        i["field_name"] == "currency" and i["severity"] == "warning"
        for i in issues
    )


def test_missing_industry_segment_is_error():
    data = extract(DATA_DIR / "corporates_A_1.xlsm")
    data.industry_segments = []
    issues = validate(data)
    assert any(i["field_name"] == "industry_risks" and i["severity"] == "error" for i in issues)


def test_liquidity_notch_adjustment_not_flagged():
    """'+1 notch' and '-2 notches' are valid liquidity adjustment values."""
    data = extract(DATA_DIR / "corporates_B_1.xlsm")
    assert data.liquidity_adjustment == "+1 notch"
    issues = validate(data)
    liq_errors = [
        i for i in issues
        if i["field_name"] == "liquidity_adjustment" and i["severity"] == "error"
    ]
    assert liq_errors == []
