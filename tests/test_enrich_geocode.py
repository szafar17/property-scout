"""Tests for address -> lat/lng geocoding (Census first, Google fallback).

Uses an httpx ``MockTransport`` so no network/creds are needed. Routes are
distinguished by request host so a single transport can serve both providers.
"""

from __future__ import annotations

import httpx

from property_scout.enrich.geocode import enrich_geocode, geocode
from property_scout.models import County, Listing, MlsBoard

_CENSUS_HOST = "geocoding.geo.census.gov"
_GOOGLE_HOST = "maps.googleapis.com"


def _census_hit(lat: float, lng: float) -> dict:
    """A Census onelineaddress response with one match (note x=lng, y=lat)."""
    return {
        "result": {
            "addressMatches": [{"coordinates": {"x": lng, "y": lat}}],
        }
    }


def _census_miss() -> dict:
    return {"result": {"addressMatches": []}}


def _google_hit(lat: float, lng: float) -> dict:
    return {
        "status": "OK",
        "results": [{"geometry": {"location": {"lat": lat, "lng": lng}}}],
    }


def _transport(*, census: httpx.Response, google: httpx.Response, captured: dict):
    """Dispatch by host; record which providers were called."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        captured.setdefault("hosts", []).append(host)
        if host == _CENSUS_HOST:
            return census
        if host == _GOOGLE_HOST:
            return google
        raise AssertionError(f"unexpected host {host}")

    return httpx.MockTransport(handler)


def _listing(address: str = "12 Maple Ave, Englewood, NJ 07631") -> Listing:
    return Listing(
        source=MlsBoard.NJMLS,
        mls_number="NJ123",
        address=address,
        town="Englewood",
        county=County.BERGEN,
    )


def test_census_hit_returns_lat_lng(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_KEY", raising=False)
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            census=httpx.Response(200, json=_census_hit(40.8929, -73.9712)),
            google=httpx.Response(200, json=_google_hit(0.0, 0.0)),
            captured=captured,
        )
    )

    result = geocode("12 Maple Ave, Englewood, NJ 07631", http=client)
    client.close()

    assert result == (40.8929, -73.9712)
    # Census satisfied the request; Google never called.
    assert captured["hosts"] == [_CENSUS_HOST]


def test_census_miss_falls_back_to_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key-123")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            census=httpx.Response(200, json=_census_miss()),
            google=httpx.Response(200, json=_google_hit(40.7128, -74.0060)),
            captured=captured,
        )
    )

    result = geocode("New Construction, Hoboken, NJ", http=client)
    client.close()

    assert result == (40.7128, -74.0060)
    # Census tried first, then Google fallback.
    assert captured["hosts"] == [_CENSUS_HOST, _GOOGLE_HOST]


def test_census_persistent_error_falls_back_to_google(monkeypatch):
    # A persistent Census failure (every retry returns 500) surfaces as
    # tenacity.RetryError; geocode() now catches it and falls back to Google
    # instead of aborting the run.
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key-123")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            census=httpx.Response(500, json={"error": "boom"}),
            google=httpx.Response(200, json=_google_hit(41.0, -74.1)),
            captured=captured,
        )
    )

    result = geocode("Somewhere, NJ", http=client)
    client.close()

    assert result == (41.0, -74.1)
    # Census failed on every retry, then the Google fallback succeeded.
    assert _CENSUS_HOST in captured["hosts"]
    assert _GOOGLE_HOST in captured["hosts"]


def test_no_match_and_no_google_key_returns_none(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_KEY", raising=False)
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            census=httpx.Response(200, json=_census_miss()),
            google=httpx.Response(200, json=_google_hit(0.0, 0.0)),
            captured=captured,
        )
    )

    # No Census match and no Google key -> graceful None, no Google call.
    result = geocode("Unmatchable, NJ", http=client)
    client.close()

    assert result is None
    assert captured["hosts"] == [_CENSUS_HOST]


def test_google_persistent_error_after_census_miss_returns_none(monkeypatch):
    # Census misses, then Google fails on every retry: the RetryError is caught
    # and geocode() degrades to None instead of raising.
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key-123")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            census=httpx.Response(200, json=_census_miss()),
            google=httpx.Response(500, json={"error": "down"}),
            captured=captured,
        )
    )

    result = geocode("Anywhere, NJ", http=client)
    client.close()

    assert result is None
    assert _GOOGLE_HOST in captured["hosts"]


def test_enrich_geocode_fills_coordinates(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_KEY", raising=False)
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            census=httpx.Response(200, json=_census_hit(40.85, -74.05)),
            google=httpx.Response(200, json=_google_hit(0.0, 0.0)),
            captured=captured,
        )
    )

    listing = _listing()
    out = enrich_geocode(listing, http=client)
    client.close()

    assert out is listing
    assert listing.latitude == 40.85
    assert listing.longitude == -74.05


def test_enrich_geocode_skips_when_already_coded():
    listing = _listing()
    listing.latitude, listing.longitude = 1.23, 4.56

    # Passing no client would attempt a real call if it geocoded; the early
    # return means we never touch the network.
    out = enrich_geocode(listing)

    assert out is listing
    assert (listing.latitude, listing.longitude) == (1.23, 4.56)
