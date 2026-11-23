"""Tests for the NJMLS / RE-Advantage adapter's auto-detecting transport.

Offline and credential-free: ``respx`` mocks every httpx call, so we exercise
both the RESO Web API (OData) path and the RETS path, plus the ``NJMLS_TRANSPORT``
override and the ``auto`` probe-then-fallback behaviour — without touching the
network or any real feed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from property_scout.config import FilterCriteria
from property_scout.ingest.njmls_readvantage import (
    NjmlsReAdvantageSource,
    NjmlsResoClient,
    RetsClient,
)
from property_scout.models import County, ListingStatus, MlsBoard, PropertyType

# --------------------------------------------------------------------------- #
# Fixtures (kept inline per the isolation contract)                            #
# --------------------------------------------------------------------------- #
RESO_BASE = "https://reso.njmls.test/odata"
RESO_TOKEN_URL = "https://reso.njmls.test/oauth/token"
RETS_LOGIN_URL = "https://rets.njmls.test/rets/Login"
RETS_SEARCH_URL = "https://rets.njmls.test/rets/Search"
RETS_LOGOUT_URL = "https://rets.njmls.test/rets/Logout"

_CRITERIA = FilterCriteria(counties=["Bergen", "Passaic"], min_price=200_000, max_price=600_000)


def _reso_property() -> dict:
    """A single RESO ``Property`` record with the fields the adapter reads."""
    return {
        "ListingId": "NJ2455519",
        "StreetNumber": "12",
        "StreetName": "Maple",
        "StreetSuffix": "Ave",
        "City": "Englewood",
        "CountyOrParish": "Bergen",
        "PostalCode": "07631",
        "StandardStatus": "Active",
        "ListPrice": 355000,
        "BedroomsTotal": 3,
        "BathroomsTotalDecimal": 2.0,
        "LivingArea": 1600,
        "YearBuilt": 1955,
        "PropertySubType": "Single Family Residence",
        "PublicRemarks": "Estate sale, needs TLC.",
        "Latitude": 40.89,
        "Longitude": -73.97,
    }


def _rets_body() -> str:
    """A RETS COMPACT-DECODED Search response with one Bergen listing."""
    columns = "\t".join(
        [
            "ListingId", "StreetNumber", "StreetName", "City", "County",
            "PostalCode", "ListPrice", "BedroomsTotal", "BathroomsTotalDecimal",
            "LivingArea", "YearBuilt", "PropertyType", "PublicRemarks",
            "Latitude", "Longitude",
        ]
    )
    data = "\t".join(
        [
            "NJ2455519", "12", "Maple Ave", "Englewood", "Bergen",
            "07631", "355000", "3", "2.0",
            "1600", "1955", "Single Family", "Estate sale, needs TLC.",
            "40.89", "-73.97",
        ]
    )
    return (
        "<RETS ReplyCode=\"0\" ReplyText=\"Operation Successful\">"
        f"<COLUMNS>\t{columns}\t</COLUMNS>"
        f"<DATA>\t{data}\t</DATA>"
        "</RETS>"
    )


def _rets_login_body() -> str:
    return (
        "<RETS ReplyCode=\"0\" ReplyText=\"Success\"><RETS-RESPONSE>\n"
        f"Search={RETS_SEARCH_URL}\n"
        f"Logout={RETS_LOGOUT_URL}\n"
        "</RETS-RESPONSE></RETS>"
    )


def _make_rets_client() -> RetsClient:
    return RetsClient(RETS_LOGIN_URL, "user", "pass", http=httpx.Client(timeout=5.0))


def _make_reso_client() -> NjmlsResoClient:
    return NjmlsResoClient(
        base_url=RESO_BASE,
        client_id="cid",
        client_secret="secret",
        token_url=RESO_TOKEN_URL,
        http=httpx.Client(timeout=5.0),
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure no ambient NJMLS env leaks into the construction defaults."""
    for var in (
        "NJMLS_TRANSPORT",
        "NJMLS_RESO_BASE_URL", "NJMLS_RESO_CLIENT_ID",
        "NJMLS_RESO_CLIENT_SECRET", "NJMLS_RESO_TOKEN_URL",
        "NJMLS_READVANTAGE_LOGIN_URL", "NJMLS_READVANTAGE_USERNAME",
        "NJMLS_READVANTAGE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# Registry / interface stability                                              #
# --------------------------------------------------------------------------- #
def test_source_key_and_interface_unchanged():
    from property_scout.ingest.base import build_source

    src = build_source("njmls", reso_client=_make_reso_client(), client=_make_rets_client())
    assert isinstance(src, NjmlsReAdvantageSource)
    assert src.name == "njmls"


# --------------------------------------------------------------------------- #
# NJMLS_TRANSPORT=reso -> RESO Web API                                         #
# --------------------------------------------------------------------------- #
@respx.mock
def test_transport_reso_uses_odata(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "reso")
    respx.post(RESO_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    prop_route = respx.get(f"{RESO_BASE}/Property").mock(
        return_value=httpx.Response(200, json={"value": [_reso_property()]})
    )

    src = NjmlsReAdvantageSource(reso_client=_make_reso_client())
    listings = list(src.fetch_active(_CRITERIA))

    assert prop_route.called
    # The OData $filter pushed the configured counties + price band down.
    sent_filter = prop_route.calls.last.request.url.params["$filter"]
    assert "CountyOrParish eq 'Bergen'" in sent_filter
    assert "ListPrice ge 200000" in sent_filter

    assert len(listings) == 1
    li = listings[0]
    assert li.source is MlsBoard.NJMLS
    assert li.mls_number == "NJ2455519"
    assert li.address == "12 Maple Ave, Englewood, NJ 07631"
    assert li.county is County.BERGEN
    assert li.list_price == 355000
    assert li.beds == 3
    assert li.status is ListingStatus.ACTIVE
    assert li.property_type is PropertyType.SINGLE_FAMILY
    # Remarks surfaced under the source-agnostic key for analyze.py.
    assert li.raw["description"] == "Estate sale, needs TLC."


# --------------------------------------------------------------------------- #
# NJMLS_TRANSPORT=rets -> RETS                                                 #
# --------------------------------------------------------------------------- #
@respx.mock
def test_transport_rets_uses_dmql(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "rets")
    respx.get(RETS_LOGIN_URL).mock(
        return_value=httpx.Response(200, text=_rets_login_body())
    )
    search_route = respx.get(RETS_SEARCH_URL).mock(
        return_value=httpx.Response(200, text=_rets_body())
    )
    respx.get(RETS_LOGOUT_URL).mock(return_value=httpx.Response(200, text="OK"))

    src = NjmlsReAdvantageSource(client=_make_rets_client(), reso_client=_make_reso_client())
    listings = list(src.fetch_active(_CRITERIA))

    assert search_route.called
    sent_query = search_route.calls.last.request.url.params["Query"]
    assert "(Status=|Active)" in sent_query
    assert "Bergen" in sent_query

    assert len(listings) == 1
    li = listings[0]
    assert li.source is MlsBoard.NJMLS
    assert li.mls_number == "NJ2455519"
    assert li.address == "12 Maple Ave, Englewood, NJ 07631"
    assert li.county is County.BERGEN
    assert li.list_price == 355000
    assert li.beds == 3
    assert li.status is ListingStatus.ACTIVE
    assert li.property_type is PropertyType.SINGLE_FAMILY
    assert li.raw["description"] == "Estate sale, needs TLC."


# --------------------------------------------------------------------------- #
# NJMLS_TRANSPORT=auto -> probe RESO first                                     #
# --------------------------------------------------------------------------- #
@respx.mock
def test_auto_prefers_reso_when_probe_succeeds(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "auto")
    respx.post(RESO_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    prop_route = respx.get(f"{RESO_BASE}/Property").mock(
        return_value=httpx.Response(200, json={"value": [_reso_property()]})
    )
    # RETS must never be touched when RESO answers the probe.
    login_route = respx.get(RETS_LOGIN_URL).mock(
        return_value=httpx.Response(500, text="should not be called")
    )

    src = NjmlsReAdvantageSource(client=_make_rets_client(), reso_client=_make_reso_client())
    listings = list(src.fetch_active(_CRITERIA))

    assert prop_route.called
    assert not login_route.called
    assert len(listings) == 1
    assert listings[0].mls_number == "NJ2455519"


# --------------------------------------------------------------------------- #
# NJMLS_TRANSPORT=auto -> fall back to RETS when RESO probe fails              #
# --------------------------------------------------------------------------- #
@respx.mock
def test_auto_falls_back_to_rets_when_probe_fails(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "auto")
    # RESO probe: token mints, but the $top=1 Property request 503s -> probe False.
    respx.post(RESO_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.get(f"{RESO_BASE}/Property").mock(
        return_value=httpx.Response(503, text="unavailable")
    )
    respx.get(RETS_LOGIN_URL).mock(return_value=httpx.Response(200, text=_rets_login_body()))
    search_route = respx.get(RETS_SEARCH_URL).mock(
        return_value=httpx.Response(200, text=_rets_body())
    )
    respx.get(RETS_LOGOUT_URL).mock(return_value=httpx.Response(200, text="OK"))

    src = NjmlsReAdvantageSource(client=_make_rets_client(), reso_client=_make_reso_client())
    listings = list(src.fetch_active(_CRITERIA))

    assert search_route.called
    assert len(listings) == 1
    assert listings[0].mls_number == "NJ2455519"
    assert listings[0].source is MlsBoard.NJMLS


# --------------------------------------------------------------------------- #
# auto with no RESO client configured -> RETS, no probe attempted             #
# --------------------------------------------------------------------------- #
@respx.mock
def test_auto_without_reso_client_uses_rets(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "auto")
    respx.get(RETS_LOGIN_URL).mock(return_value=httpx.Response(200, text=_rets_login_body()))
    search_route = respx.get(RETS_SEARCH_URL).mock(
        return_value=httpx.Response(200, text=_rets_body())
    )
    respx.get(RETS_LOGOUT_URL).mock(return_value=httpx.Response(200, text="OK"))

    # reso_client omitted and no NJMLS_RESO_BASE_URL -> RESO unavailable.
    src = NjmlsReAdvantageSource(client=_make_rets_client())
    listings = list(src.fetch_active(_CRITERIA))

    assert search_route.called
    assert len(listings) == 1


# --------------------------------------------------------------------------- #
# Unknown override degrades to auto rather than crashing                       #
# --------------------------------------------------------------------------- #
def test_unknown_transport_env_degrades_to_auto(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "carrier-pigeon")
    src = NjmlsReAdvantageSource(client=_make_rets_client())
    assert src._transport == "auto"


# --------------------------------------------------------------------------- #
# healthcheck never raises on transport failure                                #
# --------------------------------------------------------------------------- #
@respx.mock
def test_healthcheck_false_on_failure(monkeypatch):
    monkeypatch.setenv("NJMLS_TRANSPORT", "rets")
    respx.get(RETS_LOGIN_URL).mock(return_value=httpx.Response(500, text="boom"))
    src = NjmlsReAdvantageSource(client=_make_rets_client())
    assert src.healthcheck() is False
