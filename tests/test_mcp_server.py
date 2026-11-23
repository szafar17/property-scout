"""Tests for the read-only MCP server over scored listings (mcp_server.py)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from property_scout.mcp_server import (
    ListingStore,
    ScoutTools,
    build_server,
    listing_from_row,
    load_listings,
)
from property_scout.score import SUBSCORE_KEYS

SEED = Path(__file__).resolve().parents[1] / "docs" / "listings.json"


@pytest.fixture
def listings_path(tmp_path: Path) -> Path:
    path = tmp_path / "listings.json"
    shutil.copy(SEED, path)
    return path


@pytest.fixture
def tools(listings_path: Path) -> ScoutTools:
    return ScoutTools(ListingStore(listings_path))


def test_export_rows_map_to_listings_and_bad_rows_are_skipped(tmp_path: Path) -> None:
    row = json.loads(SEED.read_text())[0]
    listing = listing_from_row(row)
    assert listing is not None
    assert listing.mls_number == row["MLS Num"]
    assert listing.county.value == row["County"]
    assert listing.fixer_signals == row["Fixer Signals"]
    assert listing.price_per_sqft == round(row["List Price"] / row["SqFt"], 2)

    path = tmp_path / "mixed.json"
    path.write_text(json.dumps([row, {**row, "County": "Essex"}, {**row, "MLS Num": ""}]))
    assert len(load_listings(path)) == 1


def test_search_ranks_best_first_and_filters_by_town(tools: ScoutTools) -> None:
    everything = tools.search_listings(limit=50)
    assert len(everything) == 16
    scores = [row["score"] for row in everything]
    assert scores == sorted(scores, reverse=True)
    assert [row["rank"] for row in everything] == list(range(1, 17))

    jersey_city = tools.search_listings(town="jersey city", limit=50)
    assert jersey_city
    assert all(row["town"] == "Jersey City" for row in jersey_city)


def test_search_price_beds_score_and_fixer_filters(tools: ScoutTools) -> None:
    results = tools.search_listings(max_price=500_000, min_beds=3, min_score=40, limit=50)
    for row in results:
        assert row["price"] <= 500_000
        assert row["beds"] >= 3
        assert row["score"] >= 40
    assert all(row["fixer_signals"] for row in tools.search_listings(fixer_only=True, limit=50))
    assert len(tools.search_listings(limit=3)) == 3


def test_explain_score_points_add_up_to_the_score(tools: ScoutTools) -> None:
    best = tools.search_listings(limit=1)[0]
    explained = tools.explain_score(best["mls_number"])
    assert explained["score"] == best["score"]
    assert explained["rank"] == 1
    assert [part["subscore"] for part in explained["breakdown"]] == list(SUBSCORE_KEYS)
    assert abs(sum(part["points"] for part in explained["breakdown"]) - explained["score"]) <= 1
    assert explained["remarks_analysis"]["fixer_signals"] == best["fixer_signals"]


def test_explain_unknown_mls_number_is_a_clear_error(tools: ScoutTools) -> None:
    with pytest.raises(ValueError, match="search_listings"):
        tools.explain_score("NOPE-1")


def test_try_weights_rejects_invalid_weights(tools: ScoutTools) -> None:
    with pytest.raises(ValueError, match="sum to 100"):
        tools.try_weights({"fixer_upside": 90})
    with pytest.raises(ValueError, match="between 0 and 100"):
        tools.try_weights({"fixer_upside": -10, "price_vs_area_value": 55})
    with pytest.raises(ValueError, match="Unknown subscore"):
        tools.try_weights({"vibes": 10})


def test_try_weights_reranks_in_memory_and_writes_nothing(
    tools: ScoutTools, listings_path: Path
) -> None:
    before = listings_path.read_bytes()
    only_fixer = dict.fromkeys(SUBSCORE_KEYS, 0) | {"fixer_upside": 100}
    result = tools.try_weights(only_fixer, limit=3)

    assert len(result["top"]) == 3
    assert result["top"][0]["score"] == 100  # the pool's biggest fixer maps to 10/10
    assert "  fixer_upside: 100\n" in result["yaml_for_notion_config"]
    assert listings_path.read_bytes() == before
    assert tools.current_weights()["weights"]["fixer_upside"] == 20  # live weights untouched


def test_store_reloads_when_the_export_changes(tmp_path: Path) -> None:
    rows = json.loads(SEED.read_text())
    path = tmp_path / "listings.json"
    path.write_text(json.dumps(rows[:3]))
    store = ListingStore(path)
    assert len(store.listings()) == 3

    path.write_text(json.dumps(rows[:5]))
    later = time.time() + 5
    os.utime(path, (later, later))
    assert len(store.listings()) == 5


def test_mcp_round_trip_through_a_client(listings_path: Path) -> None:
    from mcp.client import Client

    server = build_server(ListingStore(listings_path))

    async def session() -> tuple[Any, Any, Any, Any]:
        async with Client(server) as client:
            listed = await client.list_tools()
            ok = await client.call_tool("explain_score", {"mls_number": "DEMO-1001"})
            bad = await client.call_tool("explain_score", {"mls_number": "NOPE-1"})
            tuned = await client.call_tool(
                "try_weights", {"fixer_upside": 30, "price_vs_area_value": 15}
            )
            return listed, ok, bad, tuned

    listed, ok, bad, tuned = asyncio.run(session())
    tools = listed.tools
    assert {tool.name for tool in tools} == {
        "search_listings",
        "explain_score",
        "try_weights",
        "current_weights",
    }
    assert all(tool.annotations is not None and tool.annotations.read_only_hint for tool in tools)

    assert not ok.is_error
    assert ok.structured_content["mls_number"] == "DEMO-1001"

    assert bad.is_error
    assert "search_listings" in bad.content[0].text  # the agent sees how to recover

    assert not tuned.is_error
    assert tuned.structured_content["weights"]["fixer_upside"] == 30
    assert sum(tuned.structured_content["weights"].values()) == 100
