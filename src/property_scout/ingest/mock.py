"""Offline source — reads a local sample JSON of listings.

Used for tests and dry runs without any MLS credentials. The JSON shape mirrors
the normalized :class:`Listing` fields (a thin, source-agnostic schema), so the
fixture doubles as living documentation of what downstream code expects.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from property_scout.config import FilterCriteria
from property_scout.ingest.base import BaseListingSource, register_source
from property_scout.models import (
    County,
    Listing,
    ListingStatus,
    MlsBoard,
    PropertyType,
)

DEFAULT_SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample_listings.json"


@register_source("mock")
class MockSource(BaseListingSource):
    """Yields listings from a local JSON file, with the same filtering shape."""

    name = "mock"

    def __init__(self, *, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_SAMPLE

    def fetch_active(self, criteria: FilterCriteria) -> Iterator[Listing]:
        """Yield listings from the sample file, applying basic county/price filters."""
        records = json.loads(self._path.read_text())
        for record in records:
            listing = self._normalize(record)
            if self._passes(listing, criteria):
                yield listing

    @staticmethod
    def _passes(listing: Listing, criteria: FilterCriteria) -> bool:
        if criteria.counties and listing.county.value not in criteria.counties:
            return False
        if criteria.min_price and (listing.list_price or 0) < criteria.min_price:
            return False
        if criteria.max_price and (listing.list_price or 0) > criteria.max_price:
            return False
        return not (criteria.min_beds and (listing.beds or 0) < criteria.min_beds)

    @staticmethod
    def _normalize(record: dict) -> Listing:
        return Listing(
            source=MlsBoard(record["source"]),
            mls_number=str(record["mls_number"]),
            address=record["address"],
            town=record["town"],
            county=County(record["county"]),
            list_price=record.get("list_price"),
            beds=record.get("beds"),
            baths=record.get("baths"),
            sqft=record.get("sqft"),
            year_built=record.get("year_built"),
            property_type=(
                PropertyType(record["property_type"]) if record.get("property_type") else None
            ),
            status=ListingStatus(record.get("status", "New Today")),
            days_on_market=record.get("days_on_market"),
            hoa_per_month=record.get("hoa_per_month"),
            taxes_per_year=record.get("taxes_per_year"),
            listing_url=record.get("listing_url"),
            latitude=record.get("latitude"),
            longitude=record.get("longitude"),
            raw=record,
        )
