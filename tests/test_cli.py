"""Tests for the preflight `--check` command (no network when creds absent)."""

from __future__ import annotations

import httpx

from property_scout.cli import format_table, run_checks


def test_all_creds_absent_yields_all_warn_no_fail(monkeypatch):
    for var in (
        "NOTION_TOKEN", "TRESTLE_CLIENT_ID", "TRESTLE_CLIENT_SECRET",
        "TRESTLE_TOKEN_URL", "NJMLS_READVANTAGE_LOGIN_URL",
        "NJMLS_READVANTAGE_USERNAME", "NJMLS_READVANTAGE_PASSWORD",
        "ANTHROPIC_API_KEY", "WALKSCORE_KEY", "GOOGLE_MAPS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    results = run_checks()

    # Missing creds -> WARN, never FAIL; the run is not blocked.
    assert all(r.status == "WARN" for r in results)
    assert all(r.ok for r in results)
    assert "STATUS" in format_table(results)


def test_present_keys_pass(monkeypatch):
    monkeypatch.setenv("WALKSCORE_KEY", "ws-123")
    monkeypatch.setenv("GOOGLE_MAPS_KEY", "g-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for var in (
        "NOTION_TOKEN", "TRESTLE_CLIENT_ID", "NJMLS_READVANTAGE_LOGIN_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    by_name = {r.name: r for r in run_checks()}

    assert by_name["WalkScore key"].status == "PASS"
    assert by_name["Google Maps key"].status == "PASS"
    assert by_name["Anthropic key (analysis)"].status == "PASS"


def test_notion_bad_token_is_fail(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "bad-token")
    monkeypatch.setenv("NOTION_PIPELINE_DATA_SOURCE_ID", "00000000-0000-0000-0000-000000000000")
    for var in (
        "TRESTLE_CLIENT_ID", "NJMLS_READVANTAGE_LOGIN_URL",
        "ANTHROPIC_API_KEY", "WALKSCORE_KEY", "GOOGLE_MAPS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # Fake transport returns 401 for the data-source schema read.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"object": "error", "message": "unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    by_name = {r.name: r for r in run_checks(http=client)}
    client.close()

    notion = by_name["Notion token + data source"]
    assert notion.status == "FAIL"
    assert not notion.ok
