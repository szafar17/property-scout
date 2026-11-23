"""End-to-end run using the offline MockSource (no network, no creds)."""

from __future__ import annotations

import property_scout.ingest.mock  # noqa: F401  (registers "mock")
from property_scout.config import AppConfig
from property_scout.ingest.base import build_source
from property_scout.run import run_pipeline


def test_mock_end_to_end_runs_green():
    config = AppConfig()
    source = build_source("mock")
    # enrich=False keeps it fully offline; notion=None skips upsert (dry run).
    summary = run_pipeline(config, [source], notion=None, enrich=False)

    assert summary["ingested"] == 5
    # The two 412 Example Ave records collapse to one.
    assert summary["after_dedupe"] == 4
    assert summary["scored"] == 4
    assert summary["errors"] == []
    assert summary["within_budget"] is True
    assert 0 <= summary["top_score"] <= 100

    # Cross-MLS dedupe annotated the merged record.
    merged = [item for item in summary["listings"] if "Example" in item["address"]]
    assert merged and merged[0]["also_seen"]


def test_mock_respects_price_filter():
    config = AppConfig()
    config.filters.max_price = 400000  # excludes the 419k+ listings
    summary = run_pipeline(config, [build_source("mock")], notion=None, enrich=False)
    assert summary["after_dedupe"] == 1  # only the 339k Ridgewood condo


def test_listings_are_scored_and_ranked():
    summary = run_pipeline(AppConfig(), [build_source("mock")], notion=None, enrich=False)
    scores = [item["score"] for item in summary["listings"]]
    assert scores == sorted(scores, reverse=True)
