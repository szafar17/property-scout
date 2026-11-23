"""Tests for NJ parcel/tax public-records enrichment (offline, credential-free).

Covers the best-effort contract: a mocked ArcGIS success backfills
``taxes_per_year`` / ``year_built``; the ``NJ_PARCELS_URL`` override is honored;
and an HTTP error or malformed JSON degrades to ``None`` without ever raising.

A mocked httpx transport stands in for the live feature service, so no network
or credentials are touched.
"""

from __future__ import annotations

import httpx

from property_scout.enrich import records
from property_scout.models import County, Listing, MlsBoard


def _listing() -> Listing:
    return Listing(
        source=MlsBoard.NJMLS,
        mls_number="NJ123",
        address="12 Maple Ave, Englewood, NJ 07631",
        town="Englewood",
        county=County.BERGEN,
    )


def _attrs(**over) -> dict:
    base = {
        "PROP_LOC": "12 MAPLE AVE",
        "LAST_YR_TX": 9123.45,
        "YR_CONSTR": 1958,
    }
    base.update(over)
    return base


def _client(payload, *, status: int = 200, captured: dict | None = None):
    """An httpx.Client returning ``payload`` (a dict, raw text, or error status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["url"] = str(request.url)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        if isinstance(payload, str):  # malformed (non-JSON) body
            return httpx.Response(200, text=payload)
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_returns_expected_attributes():
    client = _client({"features": [{"attributes": _attrs()}]})
    attrs = records.lookup_parcel("12 Maple Ave, Englewood, NJ", http=client)
    client.close()

    assert attrs["LAST_YR_TX"] == 9123.45
    assert attrs["YR_CONSTR"] == 1958


def test_enrich_records_backfills_taxes_and_year():
    client = _client({"features": [{"attributes": _attrs()}]})
    listing = _listing()  # taxes_per_year / year_built start as None
    records.enrich_records(listing, http=client)
    client.close()

    assert listing.taxes_per_year == 9123.45
    assert listing.year_built == 1958


def test_enrich_records_never_overwrites_mls_values():
    client = _client({"features": [{"attributes": _attrs()}]})
    listing = _listing()
    listing.taxes_per_year = 5000.0  # MLS-provided; must win
    listing.year_built = 2001
    records.enrich_records(listing, http=client)
    client.close()

    assert listing.taxes_per_year == 5000.0
    assert listing.year_built == 2001


def test_nj_parcels_url_override_is_honored(monkeypatch):
    monkeypatch.setenv("NJ_PARCELS_URL", "https://example.test/custom/query")
    captured: dict = {}
    client = _client({"features": [{"attributes": _attrs()}]}, captured=captured)

    records.lookup_parcel("12 Maple Ave, Englewood, NJ", http=client)
    client.close()

    assert captured["url"].startswith("https://example.test/custom/query")


def test_default_url_used_without_override(monkeypatch):
    monkeypatch.delenv("NJ_PARCELS_URL", raising=False)
    captured: dict = {}
    client = _client({"features": [{"attributes": _attrs()}]}, captured=captured)

    records.lookup_parcel("12 Maple Ave, Englewood, NJ", http=client)
    client.close()

    assert captured["url"].startswith(records._DEFAULT_NJ_PARCELS_FS)


def test_http_error_returns_none_no_raise():
    client = _client(None, status=503)
    assert records.lookup_parcel("12 Maple Ave, NJ", http=client) is None
    client.close()


def test_malformed_json_returns_none_no_raise():
    client = _client("<html>not json</html>")
    assert records.lookup_parcel("12 Maple Ave, NJ", http=client) is None
    client.close()


def test_empty_features_returns_none():
    client = _client({"features": []})
    assert records.lookup_parcel("12 Maple Ave, NJ", http=client) is None
    client.close()


def test_features_without_attributes_returns_none():
    client = _client({"features": [{"geometry": {}}]})
    assert records.lookup_parcel("12 Maple Ave, NJ", http=client) is None
    client.close()


def test_enrich_records_no_match_leaves_listing_untouched():
    client = _client({"features": []})
    listing = _listing()
    records.enrich_records(listing, http=client)
    client.close()

    assert listing.taxes_per_year is None
    assert listing.year_built is None
