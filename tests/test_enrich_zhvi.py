"""Tests for ZHVI appreciation analytics (pure helpers, no network/CSV).

All cases build a tiny in-memory DataFrame shaped like the Zillow ZHVI-by-ZIP
CSV: a ``RegionName`` column plus monthly ``YYYY-MM-DD`` value columns.
"""

from __future__ import annotations

import pandas as pd

from property_scout.enrich.zhvi import (
    _date_columns,
    _zip_of,
    compute_appreciation,
    enrich_appreciation,
    grade_vs_county,
)
from property_scout.models import County, Listing, MlsBoard


def _monthly_frame(zip_code: str, start_year: int, monthly_growth: float, value0: float):
    """Build a ZHVI-shaped frame: one ZIP, 21 years of monthly geometric growth.

    21 years of months means there is data strictly *more* than 20*12 months
    back, so the 20y window is computable.
    """
    cols = []
    values = []
    val = value0
    # 21 years * 12 = 252 monthly points (Jan start_year .. Dec start_year+20).
    for offset in range(21 * 12):
        year = start_year + offset // 12
        month = offset % 12 + 1
        cols.append(f"{year}-{month:02d}-01")
        values.append(round(val, 2))
        val *= 1 + monthly_growth

    row = {"RegionName": zip_code, "RegionType": "zip", "State": "NJ"}
    row.update(dict(zip(cols, values, strict=True)))
    return pd.DataFrame([row])


def test_zip_of_extracts_five_digit_zip():
    assert _zip_of("12 Maple Ave, Englewood, NJ 07631") == "07631"
    assert _zip_of("99 Main St, Town, NJ 07024-1234") == "07024"  # zip+4
    assert _zip_of("No zip in this address") is None
    # Five-digit run inside a longer number should not false-match the boundary.
    assert _zip_of("Unit 7, Newark NJ 07102") == "07102"


def test_date_columns_picks_only_time_series():
    df = pd.DataFrame(
        [{"RegionName": "07631", "State": "NJ", "2020-01-01": 1.0, "2020-02-01": 2.0}]
    )
    assert _date_columns(df) == ["2020-01-01", "2020-02-01"]


def test_compute_appreciation_positive_growth():
    # ~0.5%/month compounding over 20 years.
    df = _monthly_frame("07631", start_year=2004, monthly_growth=0.005, value0=200_000.0)
    result = compute_appreciation(df, "07631")

    assert result is not None
    assert result.zip_code == "07631"
    assert result.latest_value is not None and result.latest_value > 200_000
    # Each window present and strictly positive.
    for window in ("1y", "5y", "10y", "20y"):
        assert window in result.appr_pct
        assert window in result.cagr
        assert result.appr_pct[window] > 0
        assert result.cagr[window] > 0
    # Longer windows accumulate more total appreciation than shorter ones.
    assert result.appr_pct["20y"] > result.appr_pct["10y"] > result.appr_pct["1y"]
    # CAGR for steady ~0.5%/mo is ~6.17%/yr; check it's in a sane band.
    assert 5.0 < result.cagr["20y"] < 7.5


def test_compute_appreciation_zero_padded_lookup():
    # Frame stores zero-padded ZIP; lookup with an int-stripped ZIP still hits.
    df = _monthly_frame("07024", start_year=2004, monthly_growth=0.004, value0=300_000.0)
    result = compute_appreciation(df, "7024")  # missing leading zero
    assert result is not None
    assert result.zip_code == "07024"


def test_compute_appreciation_missing_zip_returns_none():
    df = _monthly_frame("07631", start_year=2004, monthly_growth=0.005, value0=200_000.0)
    assert compute_appreciation(df, "99999") is None


def test_compute_appreciation_short_history_skips_long_windows():
    # Only ~3 years of data: 1y window computable, 5y/10y/20y not.
    cols = {}
    val = 250_000.0
    for offset in range(36):
        year = 2020 + offset // 12
        month = offset % 12 + 1
        cols[f"{year}-{month:02d}-01"] = round(val, 2)
        val *= 1.003
    df = pd.DataFrame([{"RegionName": "07631", **cols}])

    result = compute_appreciation(df, "07631")
    assert result is not None
    assert "1y" in result.appr_pct
    assert "5y" not in result.appr_pct
    assert "20y" not in result.appr_pct


def test_grade_vs_county_bands():
    assert grade_vs_county(150, 100) == "A"  # ratio 1.5 >= 1.25
    assert grade_vs_county(115, 100) == "B"  # ratio 1.15
    assert grade_vs_county(100, 100) == "C"  # at median
    assert grade_vs_county(80, 100) == "D"   # ratio 0.80
    assert grade_vs_county(50, 100) == "F"   # ratio 0.50
    # Non-positive county median is a defensive "C".
    assert grade_vs_county(100, 0) == "C"
    assert grade_vs_county(100, -5) == "C"


def _listing(address: str) -> Listing:
    return Listing(
        source=MlsBoard.NJMLS,
        mls_number="NJ1",
        address=address,
        town="Englewood",
        county=County.BERGEN,
    )


def test_enrich_appreciation_populates_listing_fields():
    df = _monthly_frame("07631", start_year=2004, monthly_growth=0.005, value0=200_000.0)
    listing = _listing("12 Maple Ave, Englewood, NJ 07631")

    out = enrich_appreciation(listing, df, county_median_20y=100.0)

    assert out is listing
    assert listing.appr_20y_pct is not None and listing.appr_20y_pct > 0
    # 20y appreciation here is huge vs a 100% county median -> grade A.
    assert listing.trend_grade == "A"
    assert listing.appr_cagr  # non-empty CAGR dict
    assert listing.price_history_note is not None
    # YoY note lists up to 5 recent years as "YYYY: +x.x%".
    assert listing.price_history_note.count(";") <= 4


def test_enrich_appreciation_no_zip_is_noop():
    df = _monthly_frame("07631", start_year=2004, monthly_growth=0.005, value0=200_000.0)
    listing = _listing("Address with no postal code, NJ")

    out = enrich_appreciation(listing, df, county_median_20y=100.0)

    assert out is listing
    assert listing.appr_20y_pct is None
    assert listing.trend_grade is None
    assert listing.appr_cagr == {}


def test_enrich_appreciation_unknown_zip_is_noop():
    df = _monthly_frame("07631", start_year=2004, monthly_growth=0.005, value0=200_000.0)
    listing = _listing("1 Nowhere Rd, NJ 12345")  # ZIP absent from frame

    enrich_appreciation(listing, df, county_median_20y=100.0)

    assert listing.appr_20y_pct is None
    assert listing.trend_grade is None
