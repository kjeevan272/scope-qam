"""
Business analytics — data-quality detectors and signal generators.

Used by the pipeline (detect during load) and by /analytics endpoints (query
historical patterns). All functions are pure: they take data, return signals
or quality issues. Storage is the caller's responsibility.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

METRIC_FIELDS = (
    "ebitda_interest_cover", "debt_ebitda", "ffo_debt",
    "loan_value", "focf_debt", "liquidity",
)

CLIFF_THRESHOLD = 0.50          # >=50% YoY deterioration triggers cliff alert
WEIGHT_DRIFT_WARN = 0.05        # 5pp segment weight change → warning
WEIGHT_DRIFT_ERROR = 0.15       # 15pp → error


# ── Stale Data Detection ──────────────────────────────────────────────────────

def detect_stale_metrics(credit_metrics: list) -> list[dict]:
    """
    Flag years where every credit metric is identical to the preceding year.
    Pattern observed in Company A 2018–2020 — a copy-paste error in template.
    """
    issues: list[dict] = []
    sorted_metrics = sorted(credit_metrics, key=lambda m: m.year)
    for i in range(1, len(sorted_metrics)):
        prev, curr = sorted_metrics[i - 1], sorted_metrics[i]
        comparable_fields = [
            f for f in METRIC_FIELDS
            if getattr(prev, f, None) is not None
            and getattr(curr, f, None) is not None
        ]
        if not comparable_fields:
            continue
        all_same = all(
            getattr(prev, f) == getattr(curr, f) for f in comparable_fields
        )
        if all_same:
            curr.is_stale = True
            issues.append({
                "field_name": f"credit_metrics.{curr.year}",
                "issue_type": "stale_data",
                "issue_detail": (
                    f"Year {curr.year} metrics identical to {prev.year} — "
                    f"likely copy-paste error in template"
                ),
                "severity": "warning",
            })
    return issues


# ── Estimate Detection ───────────────────────────────────────────────────────

def mark_estimates(credit_metrics: list, submission_year: int | None = None) -> None:
    """
    Mark metrics whose year is >= submission_year as forward-looking estimates.
    Required for Basel III / IFRS 9 model separation of actuals vs projections.
    """
    if submission_year is None:
        submission_year = datetime.now().year
    for m in credit_metrics:
        m.is_estimate = m.year >= submission_year


# ── Metric Coverage Score ─────────────────────────────────────────────────────

def coverage_pct(credit_metrics: list) -> float:
    """
    % of credit metric cells that are non-null.
    A complete 7-year × 6-metric table = 42 cells.
    """
    if not credit_metrics:
        return 0.0
    total = len(credit_metrics) * len(METRIC_FIELDS)
    filled = sum(
        1
        for m in credit_metrics
        for f in METRIC_FIELDS
        if getattr(m, f, None) is not None
    )
    return round((filled / total * 100), 2) if total else 0.0


# ── Methodology Change Detection ──────────────────────────────────────────────

def detect_methodology_change(prev_methodologies: list[str] | None,
                                new_methodologies: list[str]) -> list[dict]:
    """
    Regulatory event: methodology added/dropped between submissions.
    Observed: Company A_1 had 2 methodologies, A_2 dropped to 1.
    """
    issues: list[dict] = []
    if not prev_methodologies:
        return issues
    prev_set = set(prev_methodologies)
    new_set = set(new_methodologies)
    added = sorted(new_set - prev_set)
    removed = sorted(prev_set - new_set)
    if added or removed:
        issues.append({
            "field_name": "methodologies",
            "issue_type": "methodology_change",
            "issue_detail": (
                f"Methodology delta: added={added}, removed={removed} — "
                f"regulatory disclosure may be required"
            ),
            "severity": "warning",
        })
    return issues


# ── Industry Weight Drift Detection ───────────────────────────────────────────

def detect_weight_drift(prev_segments: list[dict] | None,
                         new_segments: list[dict]) -> list[dict]:
    """
    Material weight shift between industry segments.
    Observed: Company B suppliers 15% → 25% (10pp shift).
    """
    issues: list[dict] = []
    if not prev_segments:
        return issues

    prev_map = {s["risk"]: float(s.get("weight", 0)) for s in prev_segments}
    for seg in new_segments:
        risk = seg["risk"]
        new_w = float(seg.get("weight", 0))
        prev_w = prev_map.get(risk, 0.0)
        delta = abs(new_w - prev_w)
        if delta >= WEIGHT_DRIFT_ERROR:
            severity = "error"
        elif delta >= WEIGHT_DRIFT_WARN:
            severity = "warning"
        else:
            continue
        issues.append({
            "field_name": f"industry_weight.{risk}",
            "issue_type": "weight_drift",
            "issue_detail": (
                f"Weight changed {prev_w:.0%} → {new_w:.0%} "
                f"(Δ {delta:.0%}) for segment '{risk}'"
            ),
            "severity": severity,
        })

    # Segments removed entirely
    new_risks = {s["risk"] for s in new_segments}
    for risk in prev_map.keys() - new_risks:
        issues.append({
            "field_name": f"industry_weight.{risk}",
            "issue_type": "segment_removed",
            "issue_detail": f"Industry segment '{risk}' removed from portfolio",
            "severity": "warning",
        })
    return issues


# ── Cliff Detection (Year-over-Year Trend) ────────────────────────────────────

def detect_metric_cliffs(credit_metrics: list, metric: str = "ebitda_interest_cover") -> list[dict]:
    """
    Identify cliffs: YoY deterioration >= CLIFF_THRESHOLD (50% by default).
    Observed in Company A: EBITDA/int dropped from 27.3x to 4.9x (82% drop).
    """
    issues: list[dict] = []
    sorted_metrics = sorted(credit_metrics, key=lambda m: m.year)
    for i in range(1, len(sorted_metrics)):
        prev, curr = sorted_metrics[i - 1], sorted_metrics[i]
        p_val = getattr(prev, metric, None)
        c_val = getattr(curr, metric, None)
        if p_val is None or c_val is None:
            continue
        try:
            p_val = float(p_val)
            c_val = float(c_val)
        except (TypeError, ValueError):
            continue
        if p_val <= 0:
            continue
        change = (c_val - p_val) / p_val
        if change <= -CLIFF_THRESHOLD:
            issues.append({
                "field_name": f"credit_metrics.{metric}.{curr.year}",
                "issue_type": "metric_cliff",
                "issue_detail": (
                    f"{metric} dropped {p_val:.2f} → {c_val:.2f} "
                    f"({change:.0%} YoY) between {prev.year} and {curr.year}"
                ),
                "severity": "warning",
            })
    return issues


# ── Trend Classifier ──────────────────────────────────────────────────────────

def classify_trend(values: list[float | None]) -> str:
    """
    Classify a time series into deteriorating / improving / stable / volatile.
    Used for /analytics/trend endpoint.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return "insufficient_data"

    deltas = [clean[i] - clean[i - 1] for i in range(1, len(clean))]
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    total = len(deltas)

    if pos / total >= 0.75:
        return "improving"
    if neg / total >= 0.75:
        return "deteriorating"
    if abs(pos - neg) <= 1 and pos + neg >= 2:
        return "volatile"
    return "stable"


# ── Percentile Helper for Sector Benchmark ────────────────────────────────────

def percentile(values: list[float], p: float) -> float | None:
    """Compute pth percentile (0–100) without numpy. Linear interpolation."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    k = (len(clean) - 1) * (p / 100)
    f = int(k)
    c = min(f + 1, len(clean) - 1)
    if f == c:
        return clean[f]
    return clean[f] + (clean[c] - clean[f]) * (k - f)
