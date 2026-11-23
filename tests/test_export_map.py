"""Tests for the read-only Notion -> listings.json export.

Uses an httpx ``MockTransport`` so no network/creds are needed, asserting that a
select, a multi-select, a number, a date, the title, and the url all flatten to
the exact Notion column names, and that rows are sorted by Score desc.
"""

from __future__ import annotations

import json

import httpx

from property_scout.export_map import export_listings, page_to_flat


def _page(*, address: str, score: float, status: str) -> dict:
    """A minimal Notion page object as returned by the data-source query API."""
    return {
        "id": f"page-{address}",
        "properties": {
            "Address": {"title": [{"plain_text": address}]},
            "Link": {"url": "https://www.njmls.com/listings/"},
            "Status": {"select": {"name": status}},           # select -> str
            "County": {"select": {"name": "Bergen"}},
            "Fixer Signals": {                                  # multi -> array
                "multi_select": [{"name": "Estate Sale"}, {"name": "TLC"}]
            },
            "Reno Items": {"multi_select": []},
            "List Price": {"number": 355000},                   # number
            "Score": {"number": score},
            "Added": {"date": {"start": "2026-06-14"}},         # date -> ISO str
            "Last Seen": {"date": {"start": "2026-06-14"}},
            "Town": {"rich_text": [{"plain_text": "Englewood"}]},
            "MLS Num": {"rich_text": [{"plain_text": "NJ2455519"}]},
        },
    }


def test_page_to_flat_decodes_each_property_kind():
    flat = page_to_flat(_page(address="1 A St, NJ", score=68.0, status="New Today"))

    assert flat["Address"] == "1 A St, NJ"          # title -> Address
    assert flat["Link"] == "https://www.njmls.com/listings/"  # url -> Link
    assert flat["Status"] == "New Today"            # select -> string
    assert flat["County"] == "Bergen"
    assert flat["Fixer Signals"] == ["Estate Sale", "TLC"]    # multi -> array
    assert flat["Reno Items"] == []
    assert flat["List Price"] == 355000             # number
    assert flat["Added"] == "2026-06-14"            # date -> ISO string
    assert flat["Town"] == "Englewood"


def test_page_to_flat_handles_empty_props():
    flat = page_to_flat({"properties": {}})
    assert flat["Address"] == ""
    assert flat["Status"] is None
    assert flat["Fixer Signals"] == []
    assert flat["Score"] is None


def test_page_to_flat_marks_real_data_not_sample():
    """Real Notion pages flatten with ``sample: False`` so the map banner
    (which shows iff any row has ``sample === true``) auto-clears after the
    first live export overwrites the seed."""
    flat = page_to_flat(_page(address="1 A St, NJ", score=68.0, status="New Today"))
    assert flat["sample"] is False

    # Holds even for a page with no properties at all.
    assert page_to_flat({"properties": {}})["sample"] is False


def _mock_transport(pages: list[dict], captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["calls"] = captured.get("calls", 0) + 1
        body = json.loads(request.content.decode())
        # First page returns the lower-scored row + has_more; second the top one.
        if "start_cursor" not in body:
            return httpx.Response(
                200, json={"results": [pages[0]], "has_more": True, "next_cursor": "c1"}
            )
        return httpx.Response(200, json={"results": [pages[1]], "has_more": False})

    return httpx.MockTransport(handler)


def test_export_paginates_sorts_and_writes(tmp_path):
    pages = [
        _page(address="Low, NJ", score=40.0, status="Active"),
        _page(address="High, NJ", score=90.0, status="New Today"),
    ]
    captured: dict = {}
    client = httpx.Client(transport=_mock_transport(pages, captured))

    out = tmp_path / "listings.json"
    rows = export_listings("ds-test", out, token="t", http=client)

    # Paginated across two pages (has_more honored).
    assert captured["calls"] == 2
    # Sorted by Score descending.
    assert [r["Score"] for r in rows] == [90.0, 40.0]
    assert rows[0]["Address"] == "High, NJ"

    # File written and parseable, with the exact field names.
    written = json.loads(out.read_text())
    assert written[0]["Status"] == "New Today"
    assert written[0]["Fixer Signals"] == ["Estate Sale", "TLC"]
    assert written[1]["List Price"] == 355000

    # Every exported (live) row is flagged not-sample, so the published map
    # hides the seed/sample banner after this export overwrites the seed.
    assert all(r["sample"] is False for r in rows)
    assert all(r["sample"] is False for r in written)
