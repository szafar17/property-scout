"""Tests for the FBI CDE safety-score enrichment (offline, credential-free).

Exercises the api.data.gov key wiring: key present (from kwarg or env) parses a
mocked CDE payload into a 0-10 score; key absent leaves the score ``None`` and
makes no HTTP call; an HTTP error degrades to ``None`` without raising.

A mocked httpx transport stands in for the live API, and ``CACHE_DIR`` is
redirected to a tmp path so the on-disk quarterly cache never leaks between
tests or touches the repo.
"""

from __future__ import annotations

import httpx
import pytest

from property_scout.enrich import crime
from property_scout.models import County, Listing, MlsBoard


@pytest.fixture(autouse=True)
def _isolate_cache_and_env(tmp_path, monkeypatch):
    """Redirect the on-disk cache and clear any inherited api.data.gov key."""
    monkeypatch.setattr(crime, "CACHE_DIR", tmp_path / "crime")
    monkeypatch.delenv("DATA_GOV_KEY", raising=False)
    monkeypatch.delenv("FBI_CDE_KEY", raising=False)


# A minimal CDE 'summarized/agency' payload in the shape _extract_offense_counts
# tolerates: an ``offenses`` object with per-offense actual series by year.
_CDE_PAYLOAD = {
    "offenses": {
        "actuals": {
            "Violent Crime": {"2022": 12, "2023": 15},
            "Property Crime": {"2022": 80, "2023": 90},
        }
    }
}


def _client(payload: dict | None = None, *, status: int = 200, captured: dict | None = None):
    """An httpx.Client whose transport returns ``payload`` (or an error status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["calls"] = captured.get("calls", 0) + 1
            captured["url"] = str(request.url)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_key_present_parses_mocked_cde_to_score():
    # violent 15 + property 90 = 105 over 10_000 residents -> 10.5/1000.
    # rate_to_score: 10 * (1 - 10.5/36) ~= 7.1.
    client = _client(_CDE_PAYLOAD)
    score = crime.safety_score(
        "Englewood",
        County.BERGEN.value,
        api_key="dg-test",
        ori="NJ0021100",
        population=10_000,
        year=2023,
        http=client,
    )
    client.close()

    assert score == crime.rate_to_score((105 / 10_000) * 1000)
    assert score == pytest.approx(7.1, abs=0.05)


def test_key_from_env_is_used_when_kwarg_absent(monkeypatch):
    monkeypatch.setenv("DATA_GOV_KEY", "env-dg-key")
    captured: dict = {}
    client = _client(_CDE_PAYLOAD, captured=captured)

    score = crime.safety_score(
        "Englewood",
        County.BERGEN.value,
        ori="NJ0021100",
        population=10_000,
        year=2023,
        http=client,
    )
    client.close()

    assert score is not None
    assert captured["calls"] == 1
    assert "API_KEY=env-dg-key" in captured["url"]


def test_fbi_cde_key_alias_is_accepted(monkeypatch):
    monkeypatch.setenv("FBI_CDE_KEY", "alias-key")
    captured: dict = {}
    client = _client(_CDE_PAYLOAD, captured=captured)

    score = crime.safety_score(
        "Hackensack",
        County.BERGEN.value,
        ori="NJ0021200",
        population=10_000,
        year=2023,
        http=client,
    )
    client.close()

    assert score is not None
    assert "API_KEY=alias-key" in captured["url"]


def test_key_absent_returns_none_without_calling():
    captured: dict = {}
    client = _client(_CDE_PAYLOAD, captured=captured)

    score = crime.safety_score(
        "Englewood",
        County.BERGEN.value,
        ori="NJ0021100",
        population=10_000,
        year=2023,
        http=client,
    )
    client.close()

    assert score is None
    assert captured.get("calls", 0) == 0  # no key -> no live call


def test_http_error_returns_none_no_raise():
    client = _client(status=500)
    score = crime.safety_score(
        "Englewood",
        County.BERGEN.value,
        api_key="dg-test",
        ori="NJ0021100",
        population=10_000,
        year=2023,
        http=client,
    )
    client.close()

    assert score is None  # raise_for_status -> HTTPError -> degraded to None


def test_enrich_safety_reads_env_key(monkeypatch):
    monkeypatch.setenv("DATA_GOV_KEY", "env-dg-key")
    client = _client(_CDE_PAYLOAD)
    listing = Listing(
        source=MlsBoard.NJMLS,
        mls_number="NJ123",
        address="1 Test St, Englewood, NJ",
        town="Englewood",
        county=County.BERGEN,
    )

    crime.enrich_safety(listing, ori="NJ0021100", population=10_000, year=2023, http=client)
    client.close()

    assert listing.safety_score is not None
