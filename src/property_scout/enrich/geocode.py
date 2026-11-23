"""Address -> latitude/longitude.

Default: the **U.S. Census Geocoder** (free, no API key). Fallback: the Google
Maps Geocoding API when Census can't match (common for new construction or
unit-level addresses). Coordinates are REQUIRED — the Notion map view and the
downstream interactive map both key off lat/lng.
"""

from __future__ import annotations

import os

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

from property_scout.models import Listing

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, max=10))
def _census_geocode(address: str, http: httpx.Client) -> tuple[float, float] | None:
    """Geocode via the free Census onelineaddress endpoint."""
    resp = http.get(
        CENSUS_URL,
        params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
    )
    resp.raise_for_status()
    matches = resp.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    coords = matches[0]["coordinates"]
    return float(coords["y"]), float(coords["x"])  # (lat, lng)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, max=10))
def _google_geocode(address: str, api_key: str, http: httpx.Client) -> tuple[float, float] | None:
    """Geocode via Google Maps Geocoding API (fallback; needs GOOGLE_MAPS_KEY)."""
    resp = http.get(GOOGLE_URL, params={"address": address, "key": api_key})
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        return None
    loc = data["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])


def geocode(address: str, *, http: httpx.Client | None = None) -> tuple[float, float] | None:
    """Resolve an address to ``(lat, lng)``, Census first then Google.

    Returns ``None`` if neither provider matches. Never raises on a non-match
    so enrichment never blocks a run.
    """
    owns = http is None
    http = http or httpx.Client(timeout=20.0)
    try:
        try:
            hit = _census_geocode(address, http)
            if hit:
                return hit
        except (httpx.HTTPError, RetryError):
            # RetryError wraps the last HTTPError once @retry exhausts its
            # attempts; catch it too so a persistent Census failure falls
            # through to the Google fallback instead of aborting the run.
            pass
        google_key = os.environ.get("GOOGLE_MAPS_KEY")
        if google_key:
            try:
                return _google_geocode(address, google_key, http)
            except (httpx.HTTPError, RetryError):
                return None
        return None
    finally:
        if owns:
            http.close()


def enrich_geocode(listing: Listing, *, http: httpx.Client | None = None) -> Listing:
    """Fill ``latitude``/``longitude`` on a listing if not already present."""
    if listing.latitude is not None and listing.longitude is not None:
        return listing
    hit = geocode(listing.address, http=http)
    if hit:
        listing.latitude, listing.longitude = hit
    return listing
