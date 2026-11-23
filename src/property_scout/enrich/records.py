"""NJ parcel / tax public-records enrichment — best-effort, never blocking.

NJ property tax and parcel data is available through the NJ Division of Taxation
MOD-IV / NJ Parcels datasets and the per-county tax board portals. There is no
single statewide API, so this module is intentionally best-effort: it tries a
known open endpoint and silently degrades to leaving fields untouched. It must
never raise into the pipeline — public-records gaps are normal and should not
fail a run.

The parcels feature-service base URL is configurable via the ``NJ_PARCELS_URL``
environment variable, defaulting to the open NJGIN composite service below.
"""

from __future__ import annotations

import contextlib
import os

import httpx

from property_scout.models import Listing

# NJ Geographic Information Network (NJGIN) parcels feature service (open).
# Overridable at runtime via the NJ_PARCELS_URL environment variable.
_DEFAULT_NJ_PARCELS_FS = (
    "https://services.arcgis.com/Hms2LavU4cwxHM5b/arcgis/rest/services/"
    "Parcels_Composite_of_NJ/FeatureServer/0/query"
)


def _parcels_url() -> str:
    """Return the parcels feature-service URL, honoring ``NJ_PARCELS_URL``."""
    return os.environ.get("NJ_PARCELS_URL") or _DEFAULT_NJ_PARCELS_FS


def lookup_parcel(address: str, *, http: httpx.Client | None = None) -> dict | None:
    """Best-effort parcel/tax lookup by address. Returns a dict or ``None``.

    Queries the NJGIN parcels feature service by address-like attribute. Any
    error (network, schema drift, no match) returns ``None`` — never raises.
    """
    owns = http is None
    http = http or httpx.Client(timeout=20.0)
    try:
        resp = http.get(
            _parcels_url(),
            params={
                "where": f"PROP_LOC LIKE '%{address.split(',')[0].upper()}%'",
                "outFields": "PROP_LOC,LAND_VAL,IMPRVT_VAL,NET_VALUE,LAST_YR_TX,YR_CONSTR",
                "f": "json",
                "resultRecordCount": 1,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            return None
        features = payload.get("features")
        if not isinstance(features, list) or not features:
            return None
        attrs = features[0].get("attributes") if isinstance(features[0], dict) else None
        return attrs if isinstance(attrs, dict) else None
    except Exception:  # noqa: BLE001 - records are best-effort, never raise
        return None
    finally:
        if owns:
            http.close()


def enrich_records(listing: Listing, *, http: httpx.Client | None = None) -> Listing:
    """Backfill taxes / year_built from public records when MLS omitted them.

    Only fills gaps; never overwrites MLS-provided values.
    """
    attrs = lookup_parcel(listing.address, http=http)
    if not attrs:
        return listing
    if listing.taxes_per_year is None and attrs.get("LAST_YR_TX"):
        with contextlib.suppress(TypeError, ValueError):
            listing.taxes_per_year = float(attrs["LAST_YR_TX"])
    if listing.year_built is None and attrs.get("YR_CONSTR"):
        with contextlib.suppress(TypeError, ValueError):
            listing.year_built = int(attrs["YR_CONSTR"])
    return listing
