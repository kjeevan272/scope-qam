"""
Rating Derivation Engine.

Computes the final issuer credit rating from:
  1. Business Risk Profile (BRP)
  2. Financial Risk Profile (FRP)
  3. Liquidity notch adjustment
  4. (optionally) sector-specific overrides

The (BRP, FRP) → anchor_rating mapping is the rating agency's IP, encoded in
COMBINATION_MATRIX. In production this is loaded from the RatingMethodology
table (versioned), allowing methodology updates without code deploys.

Notching:
  +1 notch = move up one grade (better credit quality)
  -2 notches = move down two grades (worse credit quality)
  Liquidity adjustment values like "+1 notch" / "-2 notches" are parsed
  and applied to the anchor rating.

Rating Migration:
  Every snapshot's final_rating is compared against the previous snapshot
  for the same company. Any change is recorded as a RatingMigration row.
"""
from __future__ import annotations

import re
from typing import Optional

# Canonical Scope-style rating scale, most-creditworthy to least
RATING_SCALE = [
    "AAA", "AA+", "AA", "AA-",
    "A+",  "A",  "A-",
    "BBB+", "BBB", "BBB-",
    "BB+",  "BB",  "BB-",
    "B+",   "B",   "B-",
    "CCC+", "CCC", "CCC-",
    "CC", "C", "D", "SD",
]

# (BRP, FRP) → anchor rating before notch adjustment
# Derived from public Scope corporate methodology — encoded as a sparse map
# Falls back to a deterministic blend if combination not explicitly listed
COMBINATION_MATRIX = {
    ("AAA", "AAA"): "AAA",  ("AAA", "AA+"): "AAA",  ("AAA", "AA"): "AA+",
    ("AA",  "AA"):  "AA",   ("AA",  "A"):   "AA-",
    ("A",   "A"):   "A",    ("A",   "BBB"): "A-",
    ("BBB", "BBB"): "BBB",  ("BBB", "BB+"): "BBB-", ("BBB", "BB"):  "BB+",
    ("BBB-","BB"):  "BB+",
    ("BB",  "BB"):  "BB",   ("BB",  "B+"):  "BB-",
    ("B+",  "B"):   "B",    ("B+",  "C"):   "CCC+",
    ("B",   "CC"):  "CCC",  ("B",   "C"):   "CCC-",
    ("CCC", "CCC"): "CCC",  ("CCC", "CC"):  "CC",
    ("CC",  "C"):   "C",
}


def _scale_index(rating: str) -> Optional[int]:
    try:
        return RATING_SCALE.index(rating)
    except ValueError:
        return None


def parse_notch_adjustment(value: str) -> int:
    """
    Parse a liquidity-adjustment string into an integer notch delta.
    '+1 notch' → +1   |   '-2 notches' → -2   |   'BB+' → 0
    """
    if not value:
        return 0
    m = re.match(r"\s*([+-]?\d+)\s*notch", value.lower())
    if m:
        return int(m.group(1))
    return 0


def derive_anchor(brp: str, frp: str) -> Optional[str]:
    """
    Return the anchor rating for a (BRP, FRP) pair.
    If pair not in matrix, blend by averaging scale positions.
    """
    if not brp or not frp:
        return None

    exact = COMBINATION_MATRIX.get((brp, frp))
    if exact:
        return exact

    brp_idx = _scale_index(brp)
    frp_idx = _scale_index(frp)
    if brp_idx is None or frp_idx is None:
        return None

    # Conservative blend: average, rounded toward worse rating (higher index)
    blended_idx = (brp_idx + frp_idx + 1) // 2
    blended_idx = max(0, min(len(RATING_SCALE) - 1, blended_idx))
    return RATING_SCALE[blended_idx]


def apply_notches(anchor: str, notches: int) -> Optional[str]:
    """
    Move `anchor` by `notches` positions on the scale.
    Positive = upgrade (toward AAA), negative = downgrade (toward D).
    """
    idx = _scale_index(anchor)
    if idx is None:
        return None
    # Positive notches move toward AAA (lower index), negative move toward D
    new_idx = idx - notches
    new_idx = max(0, min(len(RATING_SCALE) - 1, new_idx))
    return RATING_SCALE[new_idx]


def derive_final_rating(brp: str, frp: str, liquidity_adjustment: str) -> dict:
    """
    Top-level: produce {anchor_rating, final_rating, notches_applied}.
    """
    anchor = derive_anchor(brp, frp)
    notches = parse_notch_adjustment(liquidity_adjustment)
    final = apply_notches(anchor, notches) if anchor else None
    return {
        "anchor_rating": anchor,
        "final_rating": final,
        "notches_applied": notches,
    }


def notches_between(from_rating: str, to_rating: str) -> int:
    """
    Number of notches between two ratings.
    Positive = upgrade (from worse to better), negative = downgrade.
    """
    f_idx = _scale_index(from_rating)
    t_idx = _scale_index(to_rating)
    if f_idx is None or t_idx is None:
        return 0
    return f_idx - t_idx


def migration_direction(from_rating: Optional[str], to_rating: str) -> str:
    """
    Classify a rating change.
    new = first rating ever, affirmation = unchanged, upgrade/downgrade.
    """
    if from_rating is None:
        return "new"
    if from_rating == to_rating:
        return "affirmation"
    delta = notches_between(from_rating, to_rating)
    return "upgrade" if delta > 0 else "downgrade"
