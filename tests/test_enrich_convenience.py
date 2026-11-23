"""Tests for walkability + commute enrichment (Walk Score + Google Routes).

Uses an httpx ``MockTransport`` dispatching by host so a single client serves
both providers offline. A real ``CommuteAnchor`` is constructed (read-only).
"""

from __future__ import annotations

import httpx

from property_scout.config import CommuteAnchor
from property_scout.enrich.convenience import (
    _next_weekday_arrival,
    commute_minutes,
    enrich_convenience,
    walk_score,
)
from property_scout.models import County, Listing, MlsBoard

_WALKSCORE_HOST = "api.walkscore.com"
_ROUTES_HOST = "routes.googleapis.com"


def _transport(*, ws: httpx.Response, routes: httpx.Response, captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        captured.setdefault("hosts", []).append(host)
        if host == _WALKSCORE_HOST:
            return ws
        if host == _ROUTES_HOST:
            return routes
        raise AssertionError(f"unexpected host {host}")

    return httpx.MockTransport(handler)


def _listing(*, lat: float | None = 40.85, lng: float | None = -74.05) -> Listing:
    return Listing(
        source=MlsBoard.NJMLS,
        mls_number="NJ1",
        address="12 Maple Ave, Englewood, NJ 07631",
        town="Englewood",
        county=County.BERGEN,
        latitude=lat,
        longitude=lng,
    )


def _anchor(mode: str = "transit") -> CommuteAnchor:
    return CommuteAnchor(
        label="NYC Port Authority",
        address="625 8th Ave, New York, NY 10018",
        mode=mode,
        arrival_local_time="09:00",
    )


# --- pure / unit helpers ---------------------------------------------------


def test_next_weekday_arrival_is_rfc3339_weekday():
    ts = _next_weekday_arrival("09:00")
    assert ts.endswith("Z")
    # Parse back; the offset shift makes this a UTC instant on a weekday.
    from datetime import datetime

    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.weekday() < 5  # Mon-Fri
    assert parsed.minute == 0


def test_walk_score_success():
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={"status": 1, "walkscore": 87}),
            routes=httpx.Response(200, json={}),
            captured=captured,
        )
    )
    score = walk_score("12 Maple Ave", 40.85, -74.05, api_key="ws", http=client)
    client.close()
    assert score == 87


def test_walk_score_miss_returns_none():
    captured: dict = {}
    # status != 1 (e.g. 2 = pending) -> None, never raises.
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={"status": 2}),
            routes=httpx.Response(200, json={}),
            captured=captured,
        )
    )
    assert walk_score("addr", 1.0, 2.0, api_key="ws", http=client) is None
    client.close()


def test_commute_minutes_transit_success():
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={}),
            routes=httpx.Response(200, json={"routes": [{"duration": "2280s"}]}),
            captured=captured,
        )
    )
    mins = commute_minutes(
        (40.85, -74.05), _anchor("transit"), api_key="g", http=client
    )
    client.close()
    assert mins == 38.0  # 2280s / 60


def test_commute_minutes_no_routes_returns_none():
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={}),
            routes=httpx.Response(200, json={"routes": []}),
            captured=captured,
        )
    )
    mins = commute_minutes((1.0, 2.0), _anchor("driving"), api_key="g", http=client)
    client.close()
    assert mins is None


# --- enrich_convenience orchestration --------------------------------------


def test_enrich_convenience_populates_both(monkeypatch):
    monkeypatch.setenv("WALKSCORE_KEY", "ws-key")
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={"status": 1, "walkscore": 72}),
            routes=httpx.Response(200, json={"routes": [{"duration": "1800s"}]}),
            captured=captured,
        )
    )

    listing = _listing()
    out = enrich_convenience(listing, _anchor(), http=client)
    client.close()

    assert out is listing
    assert listing.walk_score == 72
    assert listing.commute_minutes == 30.0
    assert _WALKSCORE_HOST in captured["hosts"]
    assert _ROUTES_HOST in captured["hosts"]


def test_enrich_convenience_walkscore_persistent_error_degrades(monkeypatch):
    # A persistent Walk Score 500 surfaces as tenacity.RetryError; it is now
    # caught so walk_score degrades to None while the Routes call still runs and
    # populates commute_minutes (one provider failing must not abort the rest).
    monkeypatch.setenv("WALKSCORE_KEY", "ws-key")
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(500, json={"error": "down"}),
            routes=httpx.Response(200, json={"routes": [{"duration": "600s"}]}),
            captured=captured,
        )
    )

    listing = _listing()
    out = enrich_convenience(listing, _anchor(), http=client)
    client.close()

    assert out is listing
    assert listing.walk_score is None  # degraded, did not raise
    assert listing.commute_minutes == 10.0  # 600s / 60 — Routes still ran
    assert _ROUTES_HOST in captured["hosts"]


def test_enrich_convenience_routes_persistent_error_degrades(monkeypatch):
    # Walk Score succeeds and sets the field; a persistent Routes 500 (raised as
    # RetryError) is caught so commute_minutes degrades to None instead of
    # aborting enrichment for the listing.
    monkeypatch.setenv("WALKSCORE_KEY", "ws-key")
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={"status": 1, "walkscore": 55}),
            routes=httpx.Response(500, json={"error": "down"}),
            captured=captured,
        )
    )

    listing = _listing()
    out = enrich_convenience(listing, _anchor(), http=client)
    client.close()

    assert out is listing
    assert listing.walk_score == 55  # set before the Routes call failed
    assert listing.commute_minutes is None  # degraded, did not raise


def test_enrich_convenience_no_keys_makes_no_calls(monkeypatch):
    monkeypatch.delenv("WALKSCORE_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_KEY", raising=False)
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={"status": 1, "walkscore": 99}),
            routes=httpx.Response(200, json={"routes": [{"duration": "60s"}]}),
            captured=captured,
        )
    )

    listing = _listing()
    enrich_convenience(listing, _anchor(), http=client)
    client.close()

    assert listing.walk_score is None
    assert listing.commute_minutes is None
    assert captured.get("hosts", []) == []  # no provider called without keys


def test_enrich_convenience_skips_without_coordinates(monkeypatch):
    monkeypatch.setenv("WALKSCORE_KEY", "ws-key")
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-key")
    captured: dict = {}
    client = httpx.Client(
        transport=_transport(
            ws=httpx.Response(200, json={"status": 1, "walkscore": 99}),
            routes=httpx.Response(200, json={"routes": [{"duration": "60s"}]}),
            captured=captured,
        )
    )

    listing = _listing(lat=None, lng=None)
    out = enrich_convenience(listing, _anchor(), http=client)
    client.close()

    assert out is listing
    assert listing.walk_score is None
    assert listing.commute_minutes is None
    assert captured.get("hosts", []) == []  # early return, no calls
