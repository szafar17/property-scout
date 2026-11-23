"""HCMLS adapter — CoreLogic Trestle, RESO Web API (OData), OAuth2.

This is the *clean standard* adapter. Hudson County MLS (Liberty Board of
REALTORS®) is distributed through CoreLogic Trestle, which exposes a RESO Web
API (OData v4) over the standard ``Property`` resource. Auth is OAuth2
client-credentials: exchange Client ID/Secret for a bearer token at the Trestle
token endpoint, then issue OData ``$filter`` queries.

Provisioning: request the HCMLS / Liberty Board feed at
https://trestle.corelogic.com. Drop the credentials into the ``TRESTLE_*``
environment variables (see ``.env.example``).
"""

from __future__ import annotations

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

# RESO StandardStatus values we treat as "on market".
_ACTIVE_RESO_STATUSES = ("Active", "Active Under Contract", "Coming Soon")

# RESO CountyOrParish text -> our County enum. HCMLS is Hudson-centric but the
# feed can surface adjacent counties; map the ones the pipeline covers.
_COUNTY_MAP = {
    "Hudson": County.HUDSON,
    "Bergen": County.BERGEN,
    "Passaic": County.PASSAIC,
}

# RESO PropertySubType / PropertyType -> our enum.
_TYPE_MAP = {
    "Single Family Residence": PropertyType.SINGLE_FAMILY,
    "Residential": PropertyType.SINGLE_FAMILY,
    "Multi Family": PropertyType.MULTI_FAMILY,
    "Multi-Family": PropertyType.MULTI_FAMILY,
    "Townhouse": PropertyType.TOWNHOUSE,
    "Condominium": PropertyType.CONDO,
    "Condo": PropertyType.CONDO,
}


class TrestleAuth:
    """OAuth2 client-credentials token manager for Trestle.

    Caches the bearer token and refreshes ~60s before expiry.
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
        # Trestle uses standard OAuth2 client_credentials grant.
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


@register_source("hcmls")
class TrestleSource(BaseListingSource):
    """RESO Web API (OData) adapter for HCMLS via CoreLogic Trestle."""

    name = "hcmls"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        base_url: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        # Credentials come from the HCMLS feed provisioned at https://trestle.corelogic.com.
        client_id = client_id or os.environ.get("TRESTLE_CLIENT_ID", "")
        client_secret = client_secret or os.environ.get("TRESTLE_CLIENT_SECRET", "")
        token_url = token_url or os.environ.get("TRESTLE_TOKEN_URL", "")
        self._base_url = (base_url or os.environ.get("TRESTLE_BASE_URL", "")).rstrip("/")
        self._http = http or httpx.Client(timeout=60.0)
        self._auth = TrestleAuth(client_id, client_secret, token_url, http=self._http)

    # -- public API -------------------------------------------------------- #
    def healthcheck(self) -> bool:
        """Verify we can mint a token."""
        try:
            return bool(self._auth.token())
        except Exception:
            return False

    def fetch_active(self, criteria: FilterCriteria) -> Iterator[Listing]:
        """Yield active HCMLS listings matching ``criteria`` via OData.

        Pushes county, status, and price filters into the ``$filter`` so we
        page over the smallest possible result set.
        """
        odata_filter = self._build_filter(criteria)
        url = f"{self._base_url}/Property"
        params = {
            "$filter": odata_filter,
            "$top": "200",
            "$orderby": "ModificationTimestamp desc",
        }
        for record in self._paginate(url, params):
            listing = self._normalize(record)
            if listing is not None:
                yield listing

    # -- internals --------------------------------------------------------- #
    def _build_filter(self, criteria: FilterCriteria) -> str:
        clauses: list[str] = []
        statuses = " or ".join(f"StandardStatus eq '{s}'" for s in _ACTIVE_RESO_STATUSES)
        clauses.append(f"({statuses})")
        # HCMLS is Hudson; still honor the configured county list defensively.
        counties = [c for c in criteria.counties if c == "Hudson"] or ["Hudson"]
        county_clause = " or ".join(f"CountyOrParish eq '{c}'" for c in counties)
        clauses.append(f"({county_clause})")
        if criteria.min_price is not None:
            clauses.append(f"ListPrice ge {criteria.min_price:.0f}")
        if criteria.max_price is not None:
            clauses.append(f"ListPrice le {criteria.max_price:.0f}")
        if criteria.min_beds is not None:
            clauses.append(f"BedroomsTotal ge {criteria.min_beds}")
        return " and ".join(clauses)

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

    def _paginate(self, url: str, params: dict) -> Iterator[dict]:
        """Walk OData ``@odata.nextLink`` pages, yielding raw records."""
        next_url: str | None = url
        next_params: dict | None = params
        while next_url:
            data = self._get(next_url, next_params)
            yield from data.get("value", [])
            next_url = data.get("@odata.nextLink")
            next_params = None  # nextLink is fully-qualified

    @staticmethod
    def _map_status(reso_status: str) -> ListingStatus:
        if reso_status in _ACTIVE_RESO_STATUSES:
            return ListingStatus.ACTIVE
        return ListingStatus.OFF_MARKET

    @staticmethod
    def _map_type(record: dict) -> PropertyType | None:
        for key in ("PropertySubType", "PropertyType"):
            mapped = _TYPE_MAP.get((record.get(key) or "").strip())
            if mapped:
                return mapped
        return None

    @staticmethod
    def _hoa_per_month(record: dict) -> float | None:
        """Normalize RESO ``AssociationFee`` to a monthly figure.

        ``AssociationFee`` is reported against ``AssociationFeeFrequency``
        (Monthly / Quarterly / Annually / ...). The pipeline's ``HOA per Mo``
        column wants a monthly number, so convert by frequency. Defaults to
        treating the fee as monthly when the frequency is absent (the common
        RESO convention for residential HOA dues).
        """
        fee = _as_float(record.get("AssociationFee"))
        if fee is None:
            return None
        freq = (record.get("AssociationFeeFrequency") or "").strip().lower()
        divisor = {
            "annually": 12.0, "annual": 12.0, "yearly": 12.0,
            "semiannually": 6.0, "semi-annually": 6.0,
            "quarterly": 3.0,
            "monthly": 1.0, "": 1.0,
        }.get(freq, 1.0)
        return round(fee / divisor, 2)

    def _normalize(self, record: dict) -> Listing | None:
        """Translate a RESO Property record into a :class:`Listing`."""
        mls_number = record.get("ListingId") or record.get("ListingKey")
        if not mls_number:
            return None
        # RESO standard address fields.
        number = record.get("StreetNumber", "")
        name = record.get("StreetName", "")
        suffix = record.get("StreetSuffix", "")
        unit = record.get("UnitNumber", "")
        street = " ".join(p for p in (number, name, suffix) if p).strip()
        if unit:
            street = f"{street} #{unit}"
        town = record.get("City", "") or ""
        county_name = (record.get("CountyOrParish") or "").strip()
        county = _COUNTY_MAP.get(county_name, County.HUDSON)
        full_addr = f"{street}, {town}, NJ {record.get('PostalCode', '')}".strip(", ")

        # Surface PublicRemarks under the source-agnostic ``description`` key so
        # analyze.py (fixer-keyword prescreen + Claude) and normalize.py's
        # richness tie-breaker both see Trestle remarks uniformly.
        remarks = record.get("PublicRemarks")
        if remarks and "description" not in record:
            record["description"] = remarks

        return Listing(
            source=MlsBoard.HCMLS,
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
            property_type=self._map_type(record),
            status=self._map_status(record.get("StandardStatus", "")),
            days_on_market=_as_int(record.get("DaysOnMarket")),
            hoa_per_month=self._hoa_per_month(record),
            taxes_per_year=_as_float(record.get("TaxAnnualAmount")),
            listing_url=record.get("ListingURL"),
            latitude=_as_float(record.get("Latitude")),
            longitude=_as_float(record.get("Longitude")),
            raw=record,
        )


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
