"""NJMLS adapter — New Jersey MLS (Bergen / Passaic) via RE/Advantage.

NJMLS distributes its IDX/data feed through RE/Advantage. The exact transport is
settled at provisioning time, so this adapter **auto-detects** it at runtime:

1. ``NJMLS_TRANSPORT`` env override ∈ ``{"reso", "rets", "auto"}`` (default
   ``"auto"``) forces or unlocks detection.
2. In ``"auto"`` it cheaply probes the **RESO Web API** (OData v4, ``$top=1``)
   first — that is the clean, modern transport — and falls back to the
   historically common **RETS** path on any probe failure (network error,
   timeout, or 4xx/5xx).

Both transports translate native records into the same normalized
:class:`~property_scout.models.Listing`, so nothing downstream changes and the
``@register_source("njmls")`` entry stays identical regardless of which wire
protocol the feed turns out to speak.

The RESO path mirrors the OData/OAuth2 machinery in ``reso_trestle.py`` (bearer
token from a client-credentials grant, ``$filter`` push-down, ``@odata.nextLink``
paging) but is kept self-contained here so the two boards stay independent.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from property_scout.config import FilterCriteria
from property_scout.ingest.base import BaseListingSource, register_source
from property_scout.models import (
    County,
    Listing,
    ListingStatus,
    MlsBoard,
    PropertyType,
)

# NJMLS spans both Bergen and Passaic.
_NJMLS_COUNTIES = {"Bergen": County.BERGEN, "Passaic": County.PASSAIC}

_TYPE_MAP = {
    "SF": PropertyType.SINGLE_FAMILY,
    "Single Family": PropertyType.SINGLE_FAMILY,
    "Single Family Residence": PropertyType.SINGLE_FAMILY,
    "MF": PropertyType.MULTI_FAMILY,
    "Multi Family": PropertyType.MULTI_FAMILY,
    "Multi-Family": PropertyType.MULTI_FAMILY,
    "TH": PropertyType.TOWNHOUSE,
    "Townhouse": PropertyType.TOWNHOUSE,
    "CO": PropertyType.CONDO,
    "Condo": PropertyType.CONDO,
    "Condominium": PropertyType.CONDO,
}

# RESO StandardStatus values we treat as "on market" (mirrors reso_trestle.py).
_ACTIVE_RESO_STATUSES = ("Active", "Active Under Contract", "Coming Soon")

# Valid NJMLS_TRANSPORT values.
_TRANSPORTS = ("reso", "rets", "auto")


def _transport_pref() -> str:
    """Read the ``NJMLS_TRANSPORT`` override, defaulting to ``"auto"``.

    Unknown values degrade to ``"auto"`` rather than raising — a misconfigured
    env var must never crash the daily run.
    """
    pref = os.environ.get("NJMLS_TRANSPORT", "auto").strip().lower()
    return pref if pref in _TRANSPORTS else "auto"


class RetsClient:
    """Minimal RETS 1.x client following the ``rets-python`` pattern.

    A real deployment would lean on the ``rets`` PyPI package; this hand-rolled
    client keeps the dependency surface small and documents exactly which RETS
    capabilities we touch. Only the pieces we need (Login, Search) are wired.
    """

    def __init__(
        self,
        login_url: str,
        username: str,
        password: str,
        *,
        user_agent: str = "property-scout/0.1",
        ua_password: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._login_url = login_url
        self._username = username
        self._password = password
        self._user_agent = user_agent
        self._ua_password = ua_password
        # RETS uses HTTP Digest auth in the common RE/Advantage configuration.
        auth = httpx.DigestAuth(username, password) if username else None
        self._http = http or httpx.Client(
            timeout=60.0,
            auth=auth,
            headers={"User-Agent": user_agent, "RETS-Version": "RETS/1.7.2"},
        )
        self._capabilities: dict[str, str] = {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20))
    def login(self) -> dict[str, str]:
        """Perform RETS Login and parse the capability URL map.

        The RETS Login response body is ``KEY=VALUE`` lines inside a
        ``<RETS-RESPONSE>`` element (e.g. ``Search=/rets/Search``). We resolve
        them against the login URL's origin.
        """
        resp = self._http.get(self._login_url)
        resp.raise_for_status()
        caps: dict[str, str] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("<"):
                key, _, value = line.partition("=")
                caps[key.strip()] = value.strip()
        # Resolve relative capability URLs against the login origin.
        base = httpx.URL(self._login_url)
        self._capabilities = {
            k: str(base.join(v)) if v.startswith("/") else v for k, v in caps.items()
        }
        return self._capabilities

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=30))
    def search(
        self,
        *,
        resource: str = "Property",
        klass: str = "Residential",
        query: str,
        limit: int = 200,
    ) -> list[dict[str, str]]:
        """Run a DMQL2 ``Search`` and parse COMPACT-DECODED rows into dicts."""
        if not self._capabilities:
            self.login()
        search_url = self._capabilities.get("Search")
        if not search_url:
            raise RuntimeError("RETS server did not advertise a Search capability URL.")
        resp = self._http.get(
            search_url,
            params={
                "SearchType": resource,
                "Class": klass,
                "Query": query,
                "QueryType": "DMQL2",
                "Format": "COMPACT-DECODED",
                "Count": "1",
                "Limit": str(limit),
                "StandardNames": "1",
            },
        )
        resp.raise_for_status()
        return _parse_compact(resp.text)

    def logout(self) -> None:
        """Best-effort RETS Logout to release the session."""
        url = self._capabilities.get("Logout")
        if url:
            with contextlib.suppress(httpx.HTTPError):
                self._http.get(url)


class NjmlsResoAuth:
    """OAuth2 client-credentials token manager for an NJMLS RESO Web API feed.

    Mirrors ``reso_trestle.TrestleAuth``: caches the bearer token and refreshes
    ~60s before expiry. Kept local so the NJMLS and HCMLS boards stay decoupled.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str,
        *,
        scope: str = "api",
        http: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scope = scope
        self._http = http or httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._expires_at: float = 0.0

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20))
    def _request_token(self) -> dict:
        resp = self._http.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": self._scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()

    def token(self) -> str:
        """Return a valid bearer token, refreshing if needed."""
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        data = self._request_token()
        self._token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token


class NjmlsResoClient:
    """RESO Web API (OData v4) client for an NJMLS / RE-Advantage feed.

    Mirrors the OData conventions in ``reso_trestle.py`` — bearer auth from a
    client-credentials grant, ``$filter`` push-down, and ``@odata.nextLink``
    paging — over the standard ``Property`` resource.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        token_url: str,
        http: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=60.0)
        self._auth = NjmlsResoAuth(client_id, client_secret, token_url, http=self._http)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=30))
    def _get(self, url: str, params: dict | None) -> dict:
        resp = self._http.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {self._auth.token()}",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    def probe(self) -> bool:
        """Cheap ``$top=1`` liveness probe used by ``auto`` transport detection.

        Returns ``True`` only when the feed answers a tiny OData query without an
        HTTP error. Any failure (network, timeout, 4xx/5xx, bad token) returns
        ``False`` so the adapter can fall back to RETS.
        """
        try:
            self._get(f"{self._base_url}/Property", {"$top": "1"})
            return True
        except Exception:
            return False

    def fetch(self, *, odata_filter: str) -> Iterator[dict]:
        """Page over ``Property`` records matching ``odata_filter``."""
        url = f"{self._base_url}/Property"
        params: dict | None = {
            "$filter": odata_filter,
            "$top": "200",
            "$orderby": "ModificationTimestamp desc",
        }
        next_url: str | None = url
        while next_url:
            data = self._get(next_url, params)
            yield from data.get("value", [])
            next_url = data.get("@odata.nextLink")
            params = None  # nextLink is fully-qualified


@register_source("njmls")
class NjmlsReAdvantageSource(BaseListingSource):
    """RE/Advantage adapter for NJMLS (Bergen / Passaic).

    Auto-detects its wire transport at runtime: RESO Web API (OData) when the
    feed speaks it, otherwise the RETS path. See module docstring for the
    selection rules and the ``NJMLS_TRANSPORT`` override.
    """

    name = "njmls"

    def __init__(
        self,
        *,
        login_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        client: RetsClient | None = None,
        reso_client: NjmlsResoClient | None = None,
        transport: str | None = None,
    ) -> None:
        # RETS provisioning via your NJMLS IDX/data agreement with RE/Advantage.
        login_url = login_url or os.environ.get("NJMLS_READVANTAGE_LOGIN_URL", "")
        username = username or os.environ.get("NJMLS_READVANTAGE_USERNAME", "")
        password = password or os.environ.get("NJMLS_READVANTAGE_PASSWORD", "")
        self._client = client or RetsClient(
            login_url,
            username,
            password,
            user_agent=os.environ.get("NJMLS_READVANTAGE_USER_AGENT", "property-scout/0.1"),
            ua_password=os.environ.get("NJMLS_READVANTAGE_USER_AGENT_PASSWORD") or None,
        )
        # RESO Web API (OData) provisioning, if RE-Advantage hands you one.
        if reso_client is not None:
            self._reso_client: NjmlsResoClient | None = reso_client
        else:
            base_url = os.environ.get("NJMLS_RESO_BASE_URL", "")
            self._reso_client = (
                NjmlsResoClient(
                    base_url=base_url,
                    client_id=os.environ.get("NJMLS_RESO_CLIENT_ID", ""),
                    client_secret=os.environ.get("NJMLS_RESO_CLIENT_SECRET", ""),
                    token_url=os.environ.get("NJMLS_RESO_TOKEN_URL", ""),
                )
                if base_url
                else None
            )
        # Transport preference: explicit arg wins, else the env override.
        pref = (transport or _transport_pref()).strip().lower()
        self._transport = pref if pref in _TRANSPORTS else "auto"

    # -- public API -------------------------------------------------------- #
    def healthcheck(self) -> bool:
        """Liveness probe over whichever transport would actually be used."""
        try:
            if self._select_transport() == "reso":
                return bool(self._reso_client and self._reso_client.probe())
            return bool(self._client.login())
        except Exception:
            return False

    def fetch_active(self, criteria: FilterCriteria) -> Iterator[Listing]:
        """Yield active NJMLS listings via the detected transport."""
        if self._select_transport() == "reso":
            yield from self._fetch_reso(criteria)
        else:
            yield from self._fetch_rets(criteria)

    # -- transport selection ----------------------------------------------- #
    def _select_transport(self) -> str:
        """Resolve the effective transport ("reso" or "rets").

        ``rets`` / ``reso`` are honored verbatim (``reso`` with no configured
        RESO client falls back to RETS). ``auto`` cheaply probes RESO first and
        falls back to RETS on any probe failure.
        """
        if self._transport == "rets":
            return "rets"
        if self._transport == "reso":
            return "reso" if self._reso_client is not None else "rets"
        # auto: prefer RESO when it's configured and answers a cheap probe.
        if self._reso_client is not None and self._reso_client.probe():
            return "reso"
        return "rets"

    # -- RETS path --------------------------------------------------------- #
    def _fetch_rets(self, criteria: FilterCriteria) -> Iterator[Listing]:
        query = self._build_dmql(criteria)
        try:
            for record in self._client.search(query=query):
                listing = self._normalize_rets(record)
                if listing is not None:
                    yield listing
        finally:
            self._client.logout()

    def _build_dmql(self, criteria: FilterCriteria) -> str:
        """Compose a DMQL2 query string from filter criteria.

        DMQL2 uses ``(Field=value)`` clauses ANDed by comma, with ranged
        numerics like ``(ListPrice=min-max)``.
        """
        clauses = ["(Status=|Active)"]
        counties = [c for c in criteria.counties if c in _NJMLS_COUNTIES] or list(_NJMLS_COUNTIES)
        clauses.append("(County=|" + ",".join(counties) + ")")
        lo = int(criteria.min_price) if criteria.min_price else ""
        hi = int(criteria.max_price) if criteria.max_price else ""
        if lo or hi:
            clauses.append(f"(ListPrice={lo}-{hi})")
        if criteria.min_beds:
            clauses.append(f"(BedroomsTotal={criteria.min_beds}+)")
        return ",".join(clauses)

    def _normalize_rets(self, record: dict[str, str]) -> Listing | None:
        """Translate a RETS COMPACT-DECODED row into a :class:`Listing`."""
        mls_number = record.get("ListingId") or record.get("MLSNumber")
        if not mls_number:
            return None
        county_str = (record.get("County") or "").strip()
        county = _NJMLS_COUNTIES.get(county_str, County.BERGEN)
        town = record.get("City", "") or ""
        street = record.get("StreetName") or record.get("Address") or ""
        number = record.get("StreetNumber", "")
        addr = " ".join(p for p in (number, street) if p).strip()
        full_addr = f"{addr}, {town}, NJ {record.get('PostalCode', '')}".strip(", ")

        # Surface remarks under the source-agnostic ``description`` key so
        # analyze.py (fixer-keyword prescreen + Claude) and normalize.py's
        # richness tie-breaker see RETS remarks the same way as Trestle's.
        remarks = (
            record.get("PublicRemarks")
            or record.get("Remarks")
            or record.get("RemarksPublic")
        )
        if remarks and "description" not in record:
            record["description"] = remarks

        return Listing(
            source=MlsBoard.NJMLS,
            mls_number=str(mls_number),
            address=full_addr,
            town=town,
            county=county,
            list_price=_as_float(record.get("ListPrice")),
            beds=_as_int(record.get("BedroomsTotal")),
            baths=_as_float(record.get("BathroomsTotalDecimal") or record.get("BathroomsTotal")),
            sqft=_as_int(record.get("LivingArea")),
            year_built=_as_int(record.get("YearBuilt")),
            property_type=_TYPE_MAP.get((record.get("PropertyType") or "").strip()),
            status=ListingStatus.ACTIVE,
            days_on_market=_as_int(record.get("DaysOnMarket")),
            hoa_per_month=_as_float(record.get("AssociationFee")),
            taxes_per_year=_as_float(record.get("TaxAnnualAmount")),
            listing_url=record.get("ListingURL"),
            latitude=_as_float(record.get("Latitude")),
            longitude=_as_float(record.get("Longitude")),
            raw=record,
        )

    # -- RESO Web API path ------------------------------------------------- #
    def _fetch_reso(self, criteria: FilterCriteria) -> Iterator[Listing]:
        assert self._reso_client is not None  # guarded by _select_transport
        odata_filter = self._build_filter(criteria)
        for record in self._reso_client.fetch(odata_filter=odata_filter):
            listing = self._normalize_reso(record)
            if listing is not None:
                yield listing

    def _build_filter(self, criteria: FilterCriteria) -> str:
        """Compose an OData ``$filter`` for the RESO ``Property`` resource."""
        clauses: list[str] = []
        statuses = " or ".join(f"StandardStatus eq '{s}'" for s in _ACTIVE_RESO_STATUSES)
        clauses.append(f"({statuses})")
        counties = [c for c in criteria.counties if c in _NJMLS_COUNTIES] or list(_NJMLS_COUNTIES)
        county_clause = " or ".join(f"CountyOrParish eq '{c}'" for c in counties)
        clauses.append(f"({county_clause})")
        if criteria.min_price is not None:
            clauses.append(f"ListPrice ge {criteria.min_price:.0f}")
        if criteria.max_price is not None:
            clauses.append(f"ListPrice le {criteria.max_price:.0f}")
        if criteria.min_beds is not None:
            clauses.append(f"BedroomsTotal ge {criteria.min_beds}")
        return " and ".join(clauses)

    def _normalize_reso(self, record: dict) -> Listing | None:
        """Translate a RESO Property record into a :class:`Listing`."""
        mls_number = record.get("ListingId") or record.get("ListingKey")
        if not mls_number:
            return None
        number = record.get("StreetNumber", "")
        name = record.get("StreetName", "")
        suffix = record.get("StreetSuffix", "")
        unit = record.get("UnitNumber", "")
        street = " ".join(p for p in (number, name, suffix) if p).strip()
        if unit:
            street = f"{street} #{unit}"
        town = record.get("City", "") or ""
        county_name = (record.get("CountyOrParish") or "").strip()
        county = _NJMLS_COUNTIES.get(county_name, County.BERGEN)
        full_addr = f"{street}, {town}, NJ {record.get('PostalCode', '')}".strip(", ")

        remarks = record.get("PublicRemarks")
        if remarks and "description" not in record:
            record["description"] = remarks

        reso_status = record.get("StandardStatus", "")
        status = (
            ListingStatus.ACTIVE
            if reso_status in _ACTIVE_RESO_STATUSES
            else ListingStatus.OFF_MARKET
        )

        property_type = None
        for key in ("PropertySubType", "PropertyType"):
            property_type = _TYPE_MAP.get((record.get(key) or "").strip())
            if property_type:
                break

        return Listing(
            source=MlsBoard.NJMLS,
            mls_number=str(mls_number),
            address=full_addr,
            town=town,
            county=county,
            list_price=_as_float(record.get("ListPrice")),
            beds=_as_int(record.get("BedroomsTotal")),
            baths=_as_float(
                record.get("BathroomsTotalDecimal")
                or record.get("BathroomsTotalInteger")
            ),
            sqft=_as_int(record.get("LivingArea")),
            year_built=_as_int(record.get("YearBuilt")),
            property_type=property_type,
            status=status,
            days_on_market=_as_int(record.get("DaysOnMarket")),
            hoa_per_month=_as_float(record.get("AssociationFee")),
            taxes_per_year=_as_float(record.get("TaxAnnualAmount")),
            listing_url=record.get("ListingURL"),
            latitude=_as_float(record.get("Latitude")),
            longitude=_as_float(record.get("Longitude")),
            raw=record,
        )


def _parse_compact(body: str) -> list[dict[str, str]]:
    """Parse a RETS COMPACT-DECODED XML body into a list of column dicts.

    The body contains a single ``<COLUMNS>`` row and many ``<DATA>`` rows, each
    a tab-delimited string bracketed by the RETS field delimiter (tab).
    """
    import re

    cols_match = re.search(r"<COLUMNS>\t?(.*?)\t?</COLUMNS>", body, re.DOTALL)
    if not cols_match:
        return []
    columns = [c for c in cols_match.group(1).split("\t") if c]
    rows: list[dict[str, str]] = []
    for data_match in re.finditer(r"<DATA>\t?(.*?)\t?</DATA>", body, re.DOTALL):
        values = data_match.group(1).split("\t")
        rows.append(dict(zip(columns, values, strict=False)))
    return rows


def _as_float(value: object) -> float | None:
    if value is None or value == "" or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_int(value: object) -> int | None:
    f = _as_float(value)
    return int(f) if f is not None else None
