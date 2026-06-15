"""Unit tests for the MASTER sheet extractor."""
import pytest
from pathlib import Path

from app.extractor import extract, MasterData

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture(scope="module")
def a1() -> MasterData:
    return extract(DATA_DIR / "corporates_A_1.xlsm")


@pytest.fixture(scope="module")
def a2() -> MasterData:
    return extract(DATA_DIR / "corporates_A_2.xlsm")


@pytest.fixture(scope="module")
def b1() -> MasterData:
    return extract(DATA_DIR / "corporates_B_1.xlsm")


@pytest.fixture(scope="module")
def b2() -> MasterData:
    return extract(DATA_DIR / "corporates_B_2.xlsm")


class TestCompanyAVersion1:
    def test_rated_entity(self, a1):
        assert a1.rated_entity == "Company A"

    def test_sector(self, a1):
        assert a1.sector == "Personal & Household Goods"

    def test_currency(self, a1):
        assert a1.currency == "EUR"

    def test_country(self, a1):
        assert a1.country == "Federal Republic of Germany"

    def test_two_methodologies(self, a1):
        assert len(a1.methodologies) == 2
        assert "General Corporate Rating Methodology" in a1.methodologies

    def test_single_industry_segment(self, a1):
        assert len(a1.industry_segments) == 1
        seg = a1.industry_segments[0]
        assert seg.score == "A"
        assert seg.weight == pytest.approx(1.0)

    def test_credit_metrics_years(self, a1):
        years = {m.year for m in a1.credit_metrics}
        # Actual history years only (integer); estimate labels like '2025E' are excluded
        assert {2018, 2019, 2020, 2021, 2022, 2023, 2024} == years

    def test_file_hash_is_sha256(self, a1):
        assert len(a1.file_hash) == 64

    def test_raw_bytes_stored(self, a1):
        assert len(a1.raw_bytes) > 0


class TestCompanyAVersionDiff:
    """A_2 differs from A_1: industry risk score A→BBB, fewer methodologies."""

    def test_industry_score_changed(self, a1, a2):
        assert a1.industry_segments[0].score == "A"
        assert a2.industry_segments[0].score == "BBB"

    def test_methodology_count_reduced(self, a1, a2):
        assert len(a1.methodologies) == 2
        assert len(a2.methodologies) == 1

    def test_different_file_hashes(self, a1, a2):
        assert a1.file_hash != a2.file_hash


class TestCompanyBMultiIndustry:
    def test_two_industry_segments(self, b1):
        assert len(b1.industry_segments) == 2

    def test_weights_sum_to_one(self, b1):
        total = sum(s.weight for s in b1.industry_segments)
        assert total == pytest.approx(1.0)

    def test_currency_chf(self, b1):
        assert b1.currency == "CHF"

    def test_weight_change_in_v2(self, b1, b2):
        w1 = b1.industry_segments[0].weight
        w2 = b2.industry_segments[0].weight
        assert w1 != w2  # 0.15 → 0.25
        assert w1 == pytest.approx(0.15)
        assert w2 == pytest.approx(0.25)

    def test_b2_weights_still_sum_to_one(self, b2):
        total = sum(s.weight for s in b2.industry_segments)
        assert total == pytest.approx(1.0)


class TestNoDataHandling:
    """B_2 has 'No data' string in a numeric metric cell."""

    def test_no_data_becomes_none(self, b2):
        # 2022 ebitda_interest_cover is 'No data' in source
        metric_2022 = next((m for m in b2.credit_metrics if m.year == 2022), None)
        assert metric_2022 is not None
        assert metric_2022.ebitda_interest_cover is None

    def test_quality_issue_raised(self, b2):
        issues = [i for i in b2.quality_issues if "No data" in i.get("issue_detail", "")]
        assert len(issues) >= 1
        assert issues[0]["severity"] == "warning"
