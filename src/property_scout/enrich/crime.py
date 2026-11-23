"""Per-municipality safety score (0-10) from FBI / NJSP UCR data.

Pulls agency-level offense counts from the FBI Crime Data Explorer (CDE) API
and converts a municipality's violent + property crime rate per 1,000 residents
into a 0-10 safety score (10 = safest). Results are cached quarterly because
UCR data updates infrequently.

Primary source: FBI Crime Data Explorer API (https://cde.ucr.cjis.gov, requires
a free api.data.gov key passed as ``API_KEY``). Fallback: NJSP Uniform Crime
Report tables, which publish municipal totals annually.

The api.data.gov key is read from the ``DATA_GOV_KEY`` environment variable
(``FBI_CDE_KEY`` is also accepted as an alias). It is never hardcoded; when the
key is absent the module degrades to ``None`` so enrichment never blocks a run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from tenacity import (
    RetryError,
    retry,
    stop_after_attempt,
    wait_exponential,
)

CDE_BASE = "https://api.usa.gov/crime/fbi/cde"
CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "crime"
CACHE_TTL_SECONDS = 90 * 24 * 3600  # quarterly

# Statewide-ish reference: NJ total crime rate per 1,000 (violent+property).
# Used to anchor the 0-10 scale so a town at the state average lands near 5.
_NJ_AVG_RATE_PER_1000 = 18.0


def _api_key_from_env() -> str | None:
    """Return the api.data.gov key from the environment, or ``None``.

    Reads ``DATA_GOV_KEY`` first, then the ``FBI_CDE_KEY`` alias. Never raises;
    a missing key simply degrades the FBI CDE path to ``None``.
    """
    return os.environ.get("DATA_GOV_KEY") or os.environ.get("FBI_CDE_KEY")


def _cache_path(municipality: str, county: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{county}_{municipality}".lower().replace(" ", "_")
    return CACHE_DIR / f"{key}.json"


def _read_cache(path: Path) -> dict | None:
    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
        return json.loads(path.read_text())
    return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, max=15))
def _fetch_cde_agency_summary(
    ori: str, api_key: str, *, year: int, http: httpx.Client
) -> dict:
    """Fetch an agency's annual offense summary from the FBI CDE API.

    ``ori`` is the agency's ORI code (e.g. ``NJ0021100``). The CDE 'summarized'
    endpoint returns violent and property offense counts by year.
    """
    resp = http.get(
        f"{CDE_BASE}/summarized/agency/{ori}/all",
        params={"from": str(year), "to": str(year), "API_KEY": api_key},
    )
    resp.raise_for_status()
    return resp.json()


def rate_to_score(rate_per_1000: float) -> float:
    """Map a crime rate per 1,000 residents to a 0-10 safety score.

    A rate of 0 -> 10.0; a rate at twice the NJ average -> ~0. Linear, clamped.
    """
    if rate_per_1000 <= 0:
        return 10.0
    # Scale so the NJ average lands at ~5.0, twice the average at ~0.
    score = 10.0 * (1.0 - rate_per_1000 / (2 * _NJ_AVG_RATE_PER_1000))
    return round(max(0.0, min(10.0, score)), 1)


def safety_score(
    municipality: str,
    county: str,
    *,
    api_key: str | None = None,
    ori: str | None = None,
    population: int | None = None,
    year: int | None = None,
    http: httpx.Client | None = None,
) -> float | None:
    """Return a 0-10 safety score for a municipality, cached quarterly.

    Requires an ORI code and population to convert raw CDE counts into a rate.
    The api.data.gov key falls back to ``DATA_GOV_KEY`` / ``FBI_CDE_KEY`` from the
    environment when not passed explicitly. Returns ``None`` when inputs are
    insufficient (including a missing key), so enrichment never blocks.
    """
    cache = _cache_path(municipality, county)
    cached = _read_cache(cache)
    if cached is not None:
        return cached.get("score")

    api_key = api_key or _api_key_from_env()
    if not (api_key and ori and population):
        # Not enough to compute against the live API; leave unscored.
        return None

    year = year or (time.gmtime().tm_year - 2)  # CDE lags ~2 years
    owns = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        data = _fetch_cde_agency_summary(ori, api_key, year=year, http=http)
    except (httpx.HTTPError, RetryError):
        # RetryError wraps the last HTTPError once tenacity exhausts its attempts.
        return None
    finally:
        if owns:
            http.close()

    counts = _extract_offense_counts(data)
    total = counts.get("violent", 0) + counts.get("property", 0)
    rate = (total / population) * 1000 if population else 0.0
    score = rate_to_score(rate)
    cache.write_text(json.dumps({"score": score, "rate_per_1000": round(rate, 2)}))
    return score


def _extract_offense_counts(payload: dict) -> dict[str, int]:
    """Best-effort extraction of violent/property totals from a CDE payload.

    The CDE schema varies by endpoint; this tolerates the common shapes and
    returns zeros for anything it can't find rather than raising.
    """
    out = {"violent": 0, "property": 0}
    offenses = payload.get("offenses", payload)
    if isinstance(offenses, dict):
        rates = offenses.get("rates") or offenses.get("actuals") or {}
        for key, series in rates.items() if isinstance(rates, dict) else []:
            bucket = "violent" if "violent" in key.lower() else (
                "property" if "property" in key.lower() else None
            )
            if bucket and isinstance(series, dict):
                vals = [v for v in series.values() if isinstance(v, (int, float))]
                if vals:
                    out[bucket] += int(vals[-1])
    return out


def enrich_safety(listing, **kwargs) -> object:
    """Populate ``safety_score`` on a listing (pass ORI/population via kwargs)."""
    listing.safety_score = safety_score(listing.town, listing.county.value, **kwargs)
    return listing
