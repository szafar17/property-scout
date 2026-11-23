"""Appreciation analytics from Zillow Research ZHVI / ZORI.

Downloads the public Zillow Research CSVs (no key required), caches them on
disk, and computes 1/5/10/20-year appreciation, CAGR, and a YoY table at the
ZIP (or metro) level. Each listing is graded A-F by comparing its ZIP's 20-year
appreciation against the county median.

Data source: https://www.zillow.com/research/data/ (ZHVI = Home Value Index,
ZORI = Observed Rent Index). The "ZHVI All Homes (SFR/Condo) by ZIP" time
series is the default file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd

# Public Zillow Research ZHVI by ZIP (smoothed, seasonally adjusted).
ZHVI_ZIP_URL = (
    "https://files.zillowstatic.com/research/public_csvs/zhvi/"
    "Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
)
CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "zhvi"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # weekly refresh


@dataclass(slots=True)
class AppreciationResult:
    """Computed appreciation metrics for one ZIP code."""

    zip_code: str
    cagr: dict[str, float]  # {"1y":.., "5y":.., "10y":.., "20y":..}
    appr_pct: dict[str, float]  # total % change over each window
    yoy_table: dict[int, float]  # {year: yoy_pct}
    latest_value: float | None


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "zhvi_zip.csv"


def download_zhvi(*, force: bool = False, http: httpx.Client | None = None) -> Path:
    """Download (and cache) the ZHVI-by-ZIP CSV; returns the cached path.

    Re-downloads only when the cache is missing, stale, or ``force=True``.
    """
    path = _cache_path()
    fresh = path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS
    if fresh and not force:
        return path
    owns = http is None
    http = http or httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        resp = http.get(ZHVI_ZIP_URL)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    finally:
        if owns:
            http.close()
    return path


def load_zhvi(path: Path | None = None) -> pd.DataFrame:
    """Load the ZHVI CSV into a DataFrame indexed by 5-digit ZIP (RegionName)."""
    df = pd.read_csv(path or _cache_path(), dtype={"RegionName": str})
    df["RegionName"] = df["RegionName"].str.zfill(5)
    return df


def _date_columns(df: pd.DataFrame) -> list[str]:
    """Return the time-series (YYYY-MM-DD) columns in chronological order."""
    return [c for c in df.columns if c[:4].isdigit() and "-" in c]


def compute_appreciation(df: pd.DataFrame, zip_code: str) -> AppreciationResult | None:
    """Compute appreciation metrics for a single ZIP from the ZHVI frame.

    Returns ``None`` if the ZIP is absent or has no usable history.
    """
    zip_code = zip_code.zfill(5)
    row = df.loc[df["RegionName"] == zip_code]
    if row.empty:
        return None
    dates = _date_columns(df)
    series = row[dates].iloc[0].astype(float).dropna()
    if series.empty:
        return None

    latest = float(series.iloc[-1])
    cagr: dict[str, float] = {}
    appr_pct: dict[str, float] = {}
    for years in (1, 5, 10, 20):
        months = years * 12
        if len(series) > months:
            past = float(series.iloc[-1 - months])
            if past > 0:
                total = (latest / past) - 1.0
                appr_pct[f"{years}y"] = round(total * 100, 2)
                cagr[f"{years}y"] = round(((latest / past) ** (1 / years) - 1) * 100, 2)

    # Year-over-year table from December values.
    yoy: dict[int, float] = {}
    by_year: dict[int, float] = {}
    for col in dates:
        year = int(col[:4])
        by_year[year] = float(series.get(col, by_year.get(year, float("nan"))))
    years_sorted = sorted(by_year)
    for i in range(1, len(years_sorted)):
        y, prev = years_sorted[i], years_sorted[i - 1]
        if by_year[prev] and by_year[prev] == by_year[prev]:  # not NaN
            yoy[y] = round((by_year[y] / by_year[prev] - 1) * 100, 2)

    return AppreciationResult(
        zip_code=zip_code, cagr=cagr, appr_pct=appr_pct, yoy_table=yoy, latest_value=latest
    )


def grade_vs_county(zip_appr_20y: float, county_median_20y: float) -> str:
    """Grade a ZIP's 20-year appreciation A-F relative to its county median.

    The ratio of ZIP appreciation to county-median appreciation maps onto a
    standard A-F band; 1.0 (at the median) lands at the B/C boundary.
    """
    if county_median_20y <= 0:
        return "C"
    ratio = zip_appr_20y / county_median_20y
    if ratio >= 1.25:
        return "A"
    if ratio >= 1.10:
        return "B"
    if ratio >= 0.90:
        return "C"
    if ratio >= 0.75:
        return "D"
    return "F"


def _zip_of(address: str) -> str | None:
    """Extract a 5-digit ZIP from an address string, if present."""
    import re

    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address)
    return m.group(1) if m else None


def enrich_appreciation(
    listing,
    df: pd.DataFrame,
    county_median_20y: float,
):
    """Populate appreciation fields on a listing from a preloaded ZHVI frame.

    ``county_median_20y`` is the median 20-year appreciation across the
    candidate pool's county, used to ground the A-F grade.
    """
    zip_code = _zip_of(listing.address)
    if not zip_code:
        return listing
    result = compute_appreciation(df, zip_code)
    if result is None:
        return listing
    listing.appr_cagr = result.cagr
    listing.appr_20y_pct = result.appr_pct.get("20y")
    if listing.appr_20y_pct is not None:
        listing.trend_grade = grade_vs_county(listing.appr_20y_pct, county_median_20y)
    if result.yoy_table:
        recent = sorted(result.yoy_table.items())[-5:]
        listing.price_history_note = "; ".join(f"{y}: {v:+.1f}%" for y, v in recent)
    return listing
