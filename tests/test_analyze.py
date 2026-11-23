"""Tests for fixer-keyword detection driving the Fixer Signals column."""

from __future__ import annotations

from property_scout.analyze import _merge_signals, analyze_listing, apply_analysis
from property_scout.config import AppConfig
from property_scout.models import County, Listing, MlsBoard


def _listing(description: str) -> Listing:
    return Listing(
        source=MlsBoard.NJMLS, mls_number="24-1001",
        address="412 Example Ave, Fairview, NJ 07022", town="Fairview",
        county=County.BERGEN, raw={"description": description},
    )


def test_keyword_prescreen_drives_signals_without_api(monkeypatch):
    # No ANTHROPIC_API_KEY -> pure keyword path; config keywords must populate.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = AppConfig()
    listing = _listing("Charming estate sale, sold strictly as-is. Bring all offers!")

    analysis = analyze_listing(listing, config)

    assert "estate sale" in analysis.fixer_signals
    assert "as-is" in analysis.fixer_signals
    assert "bring all offers" in analysis.fixer_signals


def test_apply_analysis_writes_fixer_signals(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = AppConfig()
    listing = _listing("Handyman special, needs work throughout.")

    apply_analysis(listing, analyze_listing(listing, config))

    assert "handyman" in listing.fixer_signals
    assert "needs work" in listing.fixer_signals


def test_no_description_yields_no_signals(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    listing = _listing("")
    analysis = analyze_listing(listing, AppConfig())
    assert analysis.fixer_signals == []


def test_merge_signals_dedupes_case_insensitively():
    merged = _merge_signals(["As-Is", "roof"], ["as-is", "tlc"])
    # Preserves first occurrence; folds in keyword-only hits; no case dups.
    assert merged == ["As-Is", "roof", "tlc"]
