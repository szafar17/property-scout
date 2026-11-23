"""Deterministic tests for the weighted scorer."""

from __future__ import annotations

from property_scout.config import ScoringWeights
from property_scout.models import County, Listing, MlsBoard
from property_scout.score import _minmax, compute_subscores, score_one, score_pool


def _listing(price: float, sqft: int, **kw) -> Listing:
    return Listing(
        source=MlsBoard.NJMLS,
        mls_number=str(price),
        address=f"{int(price)} Main St, Wayne, NJ 07470",
        town="Wayne",
        county=County.PASSAIC,
        list_price=price,
        sqft=sqft,
        **kw,
    )


def test_minmax_basic():
    assert _minmax([0, 10, 20]) == [0.0, 5.0, 10.0]


def test_minmax_inverted():
    # Lowest raw value scores highest when inverted.
    assert _minmax([0, 10, 20], invert=True) == [10.0, 5.0, 0.0]


def test_minmax_degenerate_is_midpoint():
    assert _minmax([7, 7, 7]) == [5.0, 5.0, 5.0]


def test_minmax_preserves_none():
    assert _minmax([None, 0, 10]) == [None, 0.0, 10.0]


def test_weights_must_sum_to_100():
    import pytest

    with pytest.raises(ValueError):
        ScoringWeights(price_vs_area_value=50)  # breaks the sum


def test_score_one_neutral_when_all_missing():
    # All subscores None -> treated as 5/10 -> exactly half of 100.
    weights = ScoringWeights()
    subs = dict.fromkeys(
        ["price_vs_area_value", "fixer_upside", "trend_20y",
         "safety", "location_convenience", "carry_costs"], None
    )
    assert score_one(subs, weights) == 50


def test_score_one_perfect():
    weights = ScoringWeights()
    subs = dict.fromkeys(
        ["price_vs_area_value", "fixer_upside", "trend_20y",
         "safety", "location_convenience", "carry_costs"], 10.0
    )
    assert score_one(subs, weights) == 100


def test_cheaper_per_sqft_scores_higher():
    cheap = _listing(300000, 2000)   # $150/sqft
    pricey = _listing(800000, 2000)  # $400/sqft
    subs = compute_subscores([cheap, pricey])
    assert subs[0]["price_vs_area_value"] > subs[1]["price_vs_area_value"]


def test_lower_carry_costs_score_higher():
    low = _listing(400000, 1500, taxes_per_year=5000, hoa_per_month=0)
    high = _listing(400000, 1500, taxes_per_year=20000, hoa_per_month=500)
    subs = compute_subscores([low, high])
    assert subs[0]["carry_costs"] > subs[1]["carry_costs"]


def test_score_pool_sorts_descending_and_writes_subscores():
    a = _listing(300000, 2000, taxes_per_year=4000, appr_20y_pct=120, safety_score=9)
    b = _listing(900000, 1200, taxes_per_year=25000, appr_20y_pct=20, safety_score=3)
    ranked = score_pool([b, a], ScoringWeights())
    assert ranked[0] is a  # the obviously-better deal ranks first
    assert ranked[0].score >= ranked[1].score
    assert set(ranked[0].subscores) == {
        "price_vs_area_value", "fixer_upside", "trend_20y",
        "safety", "location_convenience", "carry_costs",
    }
    assert 0 <= ranked[0].score <= 100
