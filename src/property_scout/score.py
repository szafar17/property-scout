"""Weighted 0-100 deal score from config-driven weights.

Each of the six subscores is normalized to 0-10 *within the candidate pool*
(min-max scaling over the listings being scored together), so the score is
relative to the day's inventory. Carry costs are inverted (lower is better).
The final score is the weighted sum scaled to 0-100.

Subscores:
    price_vs_area_value   — lower $/sqft vs pool is better
    fixer_upside          — more fixer signals + older home is better
    trend_20y             — higher 20y appreciation is better
    safety                — higher safety score is better
    location_convenience  — higher Walk Score + shorter commute is better
    carry_costs           — lower taxes + HOA is better (inverted)
"""

from __future__ import annotations

from collections.abc import Sequence

from property_scout.config import ScoringWeights
from property_scout.models import Listing

SUBSCORE_KEYS = (
    "price_vs_area_value",
    "fixer_upside",
    "trend_20y",
    "safety",
    "location_convenience",
    "carry_costs",
)


def _minmax(values: Sequence[float | None], *, invert: bool = False) -> list[float | None]:
    """Scale a sequence to 0-10 by min-max; ``None`` stays ``None``.

    With a degenerate range (all equal/one value) everything maps to the
    midpoint 5.0. When ``invert`` is set, low raw values score high.
    """
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values)
    lo, hi = min(present), max(present)
    span = hi - lo
    out: list[float | None] = []
    for v in values:
        if v is None:
            out.append(None)
        elif span == 0:
            out.append(5.0)
        else:
            scaled = (v - lo) / span * 10.0
            out.append(round(10.0 - scaled if invert else scaled, 3))
    return out


def _fixer_raw(listing: Listing) -> float:
    """Raw fixer-upside signal: count of fixer signals plus an age bonus."""
    signal = float(len(listing.fixer_signals) + len(listing.reno_items))
    if listing.year_built:
        age = max(0, 2026 - listing.year_built)
        signal += min(age / 25.0, 3.0)  # up to +3 for older homes
    return signal


def _convenience_raw(listing: Listing) -> float | None:
    """Composite convenience: Walk Score (0-100) blended with inverse commute."""
    parts: list[float] = []
    if listing.walk_score is not None:
        parts.append(listing.walk_score / 10.0)  # 0-10
    if listing.commute_minutes is not None:
        # 0 min -> 10, 90+ min -> 0
        parts.append(max(0.0, 10.0 - listing.commute_minutes / 9.0))
    if not parts:
        return None
    return sum(parts) / len(parts)


def _carry_raw(listing: Listing) -> float | None:
    """Annual carry cost = taxes + 12*HOA (lower is better; inverted later)."""
    taxes = listing.taxes_per_year
    hoa = (listing.hoa_per_month or 0) * 12
    if taxes is None and not hoa:
        return None
    return (taxes or 0) + hoa


def compute_subscores(listings: list[Listing]) -> list[dict[str, float | None]]:
    """Compute the six 0-10 subscores for each listing, pool-relative.

    Returns a list aligned with ``listings``; each entry maps subscore key ->
    value (or ``None`` when the underlying data is missing).
    """
    price = _minmax([listing.price_per_sqft for listing in listings], invert=True)
    fixer = _minmax([_fixer_raw(listing) for listing in listings])
    trend = _minmax([listing.appr_20y_pct for listing in listings])
    safety = _minmax([listing.safety_score for listing in listings])
    conv = _minmax([_convenience_raw(listing) for listing in listings])
    carry = _minmax([_carry_raw(listing) for listing in listings], invert=True)

    subscores: list[dict[str, float | None]] = []
    for i in range(len(listings)):
        subscores.append(
            {
                "price_vs_area_value": price[i],
                "fixer_upside": fixer[i],
                "trend_20y": trend[i],
                "safety": safety[i],
                "location_convenience": conv[i],
                "carry_costs": carry[i],
            }
        )
    return subscores


def score_one(subscores: dict[str, float | None], weights: ScoringWeights) -> int:
    """Combine subscores with weights into a 0-100 integer score.

    Missing subscores are treated as the neutral midpoint (5.0/10) so a single
    data gap can't zero out an otherwise strong listing.
    """
    weight_map = weights.model_dump()
    total = 0.0
    for key in SUBSCORE_KEYS:
        sub = subscores.get(key)
        sub_val = 5.0 if sub is None else sub
        total += (sub_val / 10.0) * weight_map[key]
    return int(round(total))


def score_pool(listings: list[Listing], weights: ScoringWeights) -> list[Listing]:
    """Score every listing in a pool, writing ``score`` and ``subscores``.

    Returns the same list (mutated in place), sorted by score descending.
    """
    all_subs = compute_subscores(listings)
    for listing, subs in zip(listings, all_subs, strict=True):
        listing.subscores = {k: (round(v, 2) if v is not None else None) for k, v in subs.items()}
        listing.score = score_one(subs, weights)
    listings.sort(key=lambda listing: listing.score or 0, reverse=True)
    return listings
