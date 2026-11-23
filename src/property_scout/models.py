"""Core domain models shared across the pipeline.

Everything downstream of the ingest adapters operates on the
source-agnostic :class:`Listing` dataclass, so swapping or adding an MLS
board never ripples past ``ingest/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class County(str, Enum):
    """NJ counties covered by this pipeline."""

    BERGEN = "Bergen"
    PASSAIC = "Passaic"
    HUDSON = "Hudson"


class MlsBoard(str, Enum):
    """MLS boards we ingest from. Values match the Notion ``MLS`` select."""

    NJMLS = "NJMLS"  # New Jersey MLS (Bergen / Passaic) via RE/Advantage
    HCMLS = "HCMLS"  # Hudson County MLS / Liberty Board via CoreLogic Trestle
    GSMLS = "GSMLS"  # Garden State MLS (reserved; not yet wired)


class PropertyType(str, Enum):
    """Property type select values, matching Notion exactly."""

    SINGLE_FAMILY = "Single Family"
    MULTI_FAMILY = "Multi-Family"
    TOWNHOUSE = "Townhouse"
    CONDO = "Condo"


class ListingStatus(str, Enum):
    """Pipeline status select values, matching Notion exactly."""

    NEW_TODAY = "New Today"
    ACTIVE = "Active"
    PRICE_DROP = "Price Drop"
    OFF_MARKET = "Off Market"


@dataclass(slots=True)
class Listing:
    """A normalized listing, the lingua franca of the pipeline.

    Ingest adapters populate the *raw* MLS-sourced fields; enrichment and
    analysis stages fill the optional derived fields. ``None`` always means
    "unknown / not yet computed", never zero.
    """

    # --- Identity (raw from MLS) ---
    source: MlsBoard
    mls_number: str
    address: str
    town: str
    county: County

    # --- Core attributes ---
    list_price: float | None = None
    beds: int | None = None
    baths: float | None = None
    sqft: int | None = None
    year_built: int | None = None
    property_type: PropertyType | None = None
    status: ListingStatus = ListingStatus.NEW_TODAY
    days_on_market: int | None = None
    hoa_per_month: float | None = None
    taxes_per_year: float | None = None
    listing_url: str | None = None

    # --- Geocode (enrich/geocode.py) ---
    latitude: float | None = None
    longitude: float | None = None
    normalized_address: str | None = None

    # --- Appreciation (enrich/zhvi.py) ---
    appr_20y_pct: float | None = None
    trend_grade: str | None = None  # A-F vs county median
    appr_cagr: dict[str, float] = field(default_factory=dict)  # {"1y":..,"5y":..}
    price_history_note: str | None = None

    # --- Safety (enrich/crime.py) ---
    safety_score: float | None = None  # 0-10

    # --- Convenience (enrich/convenience.py) ---
    walk_score: int | None = None
    commute_minutes: float | None = None

    # --- Analysis (analyze.py) ---
    fixer_signals: list[str] = field(default_factory=list)
    reno_items: list[str] = field(default_factory=list)
    last_reno: str | None = None
    flags: str | None = None
    pros: str | None = None

    # --- Scoring (score.py) ---
    score: int | None = None
    subscores: dict[str, float | None] = field(default_factory=dict)

    # --- Provenance ---
    raw: dict[str, Any] = field(default_factory=dict)
    also_seen_mls: list[str] = field(default_factory=list)  # other boards' MLS#s
    first_seen: date | None = None
    last_seen: date | None = None

    @property
    def price_per_sqft(self) -> float | None:
        """List price divided by living area, or ``None`` if either is missing."""
        if self.list_price and self.sqft:
            return round(self.list_price / self.sqft, 2)
        return None

    @property
    def dedupe_key(self) -> str:
        """Stable cross-MLS identity: normalized address (falls back to raw)."""
        return (self.normalized_address or self.address).upper().strip()
