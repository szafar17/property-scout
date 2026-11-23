"""Walkability and commute-to-anchor enrichment.

- **Walk Score**: the Walk Score API returns a 0-100 walkability score for a
  lat/lng + address (requires ``WALKSCORE_KEY``).
- **Commute**: the Google Routes API (Compute Routes) returns transit/driving
  duration from the listing to the configured commute anchor (requires
  ``GOOGLE_MAPS_KEY``).

Both require coordinates, so run ``enrich/geocode.py`` first.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime, timedelta

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

from property_scout.config import CommuteAnchor
from property_scout.models import Listing

WALKSCORE_URL = "https://api.walkscore.com/score"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Walk Score travel-mode string -> Google RouteTravelMode.
_MODE_MAP = {
    "transit": "TRANSIT",
    "driving": "DRIVE",
    "walking": "WALK",
    "bicycling": "BICYCLE",
}


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, max=10))
def walk_score(
    address: str, lat: float, lng: float, *, api_key: str, http: httpx.Client
) -> int | None:
    """Return the 0-100 Walk Score for a coordinate, or ``None`` on miss."""
    resp = http.get(
        WALKSCORE_URL,
        params={
            "format": "json",
            "address": address,
            "lat": lat,
            "lon": lng,
            "wsapikey": api_key,
            "transit": 1,
            "bike": 1,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    # status 1 == success per Walk Score API docs.
    return int(data["walkscore"]) if data.get("status") == 1 and data.get("walkscore") else None


def _next_weekday_arrival(arrival_local_time: str) -> str:
    """RFC3339 UTC timestamp for the next weekday at the given local HH:MM (ET)."""
    hour, minute = (int(x) for x in arrival_local_time.split(":"))
    # America/New_York is UTC-4/-5; use a fixed offset approximation for the
    # request (Routes API tolerates near-future arrival times).
    now = datetime.now(UTC) + timedelta(days=1)
    while now.weekday() >= 5:  # skip Sat/Sun
        now += timedelta(days=1)
    arrival = now.replace(hour=(hour + 4) % 24, minute=minute, second=0, microsecond=0)
    return arrival.isoformat().replace("+00:00", "Z")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, max=10))
def commute_minutes(
    origin: tuple[float, float],
    anchor: CommuteAnchor,
    *,
    api_key: str,
    http: httpx.Client,
) -> float | None:
    """Compute commute minutes from ``origin`` to the anchor via Google Routes."""
    travel_mode = _MODE_MAP.get(anchor.mode, "TRANSIT")
    body: dict = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"address": anchor.address},
        "travelMode": travel_mode,
    }
    if travel_mode == "TRANSIT":
        body["arrivalTime"] = _next_weekday_arrival(anchor.arrival_local_time)
    elif travel_mode == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE"

    resp = http.post(
        ROUTES_URL,
        json=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.duration",
        },
    )
    resp.raise_for_status()
    routes = resp.json().get("routes", [])
    if not routes:
        return None
    duration = routes[0]["duration"]  # e.g. "2280s"
    seconds = int(str(duration).rstrip("s"))
    return round(seconds / 60.0, 1)


def enrich_convenience(
    listing: Listing, anchor: CommuteAnchor, *, http: httpx.Client | None = None
) -> Listing:
    """Populate ``walk_score`` and ``commute_minutes`` if coordinates exist."""
    if listing.latitude is None or listing.longitude is None:
        return listing
    owns = http is None
    http = http or httpx.Client(timeout=30.0)
    # @retry surfaces a persistent failure as RetryError (not httpx.HTTPError),
    # so suppress both to degrade just that one field to None rather than abort
    # the whole listing's enrichment.
    try:
        ws_key = os.environ.get("WALKSCORE_KEY")
        if ws_key:
            with contextlib.suppress(httpx.HTTPError, RetryError):
                listing.walk_score = walk_score(
                    listing.address, listing.latitude, listing.longitude, api_key=ws_key, http=http
                )
        g_key = os.environ.get("GOOGLE_MAPS_KEY")
        if g_key:
            with contextlib.suppress(httpx.HTTPError, RetryError):
                listing.commute_minutes = commute_minutes(
                    (listing.latitude, listing.longitude), anchor, api_key=g_key, http=http
                )
        return listing
    finally:
        if owns:
            http.close()
