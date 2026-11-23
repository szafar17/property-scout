"""Tests for the New-Today Run Log digest helpers.

Covers digest selection (only New Today, top-N by score) and the one-line "why"
(top fixer signal + trend grade), plus that ``append_run_log`` no-ops without a
token instead of crashing.
"""

from __future__ import annotations

from property_scout.run import (
    _digest_why,
    _fmt_price,
    append_run_log,
    new_today_digest,
)


def _item(addr, score, status, signals=None, grade=None, price=None):
    return {
        "address": addr,
        "town": "Englewood",
        "price": price,
        "score": score,
        "status": status,
        "trend_grade": grade,
        "fixer_signals": signals or [],
    }


def _summary():
    return {
        "listings": [
            _item("Active A", 95, "Active"),                      # excluded
            _item("New Hi", 80, "New Today", ["Estate Sale"], "A", 355000),
            _item("New Mid", 60, "New Today", ["TLC"], "B", 400000),
            _item("New Lo", 40, "New Today", [], "C", 250000),
            _item("New 4th", 30, "New Today", ["As-Is"], "D", 300000),
        ]
    }


def test_new_today_digest_picks_top3_new_only():
    digest = new_today_digest(_summary())
    assert [d["address"] for d in digest] == ["New Hi", "New Mid", "New Lo"]
    # No Active rows leaked in.
    assert all(d["status"] == "New Today" for d in digest)


def test_digest_why_uses_top_signal_and_grade():
    assert _digest_why(_item("x", 1, "New Today", ["Estate Sale", "TLC"], "A")) == \
        "Estate Sale, trend A"
    assert _digest_why(_item("x", 1, "New Today", [], None)) == "no fixer signal yet"


def test_fmt_price():
    assert _fmt_price(355000) == "$355,000"
    assert _fmt_price(None) == "$?"


def test_append_run_log_noops_without_token(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    # Must not raise / make any HTTP call.
    append_run_log("page-id", _summary())


def test_empty_digest_when_no_new_today():
    summary = {"listings": [_item("Active A", 95, "Active")]}
    assert new_today_digest(summary) == []
