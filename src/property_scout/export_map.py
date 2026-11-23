"""Read-only export of the Notion pipeline DB to a flat ``listings.json``.

This powers the GitHub Pages map: each morning CI runs ``property-scout-export``
*after* the pipeline upsert, queries the Notion data source (the same paginated
``POST /v1/data_sources/{id}/query`` pattern used by ``notion_sync.py``), maps
each page's properties back to the flat field shape the map expects (the exact
Notion column names), sorts by ``Score`` descending, and writes a JSON array to
``docs/listings.json`` which ``actions/deploy-pages`` then publishes.

It **never writes to Notion** — query-only. Auth + ``Notion-Version`` are reused
from ``config.py`` so there is a single source of truth for the API version.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from property_scout.config import NOTION_VERSION, pipeline_data_source_id

API = "https://api.notion.com/v1"

# The exact Notion column names, grouped by how they decode.
_SELECT_FIELDS = ("County", "MLS", "Type", "Status", "Trend Grade")
_MULTI_FIELDS = ("Fixer Signals", "Reno Items")
_NUMBER_FIELDS = (
    "List Price", "Beds", "Baths", "SqFt", "$ per SqFt", "Year Built",
    "20Y Appr Pct", "Safety 0-10", "Walk Score", "Commute Min", "HOA per Mo",
    "Taxes per Yr", "DOM", "Score", "Latitude", "Longitude",
)
_TEXT_FIELDS = ("Town", "MLS Num", "Flags", "Last Reno", "Price History", "Pros")
_DATE_FIELDS = ("Added", "Last Seen")


# --------------------------------------------------------------------------- #
# Notion property decoders                                                     #
# --------------------------------------------------------------------------- #
def _title_text(prop: dict) -> str:
    return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))


def _rich_text(prop: dict) -> str | None:
    out = "".join(rt.get("plain_text", "") for rt in prop.get("rich_text", []))
    return out or None


def _select_name(prop: dict) -> str | None:
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _multi_names(prop: dict) -> list[str]:
    return [opt.get("name", "") for opt in prop.get("multi_select", [])]


def _number(prop: dict) -> float | int | None:
    return prop.get("number")


def _date_iso(prop: dict) -> str | None:
    d = prop.get("date")
    return d.get("start") if d else None


def _url(prop: dict) -> str | None:
    return prop.get("url")


def page_to_flat(page: dict) -> dict:
    """Map one Notion page object to the flat ``listings.json`` dict.

    Uses the exact Notion column names as JSON keys so the map markup (borrowed
    from ``property_scout_map.html``) reads them verbatim. Missing properties
    decode to sensible empties (``None`` / ``[]``), never raising.
    """
    props = page.get("properties", {})
    flat: dict[str, object] = {}

    # Title -> Address. url -> Link.
    flat["Address"] = _title_text(props.get("Address", {}))
    flat["Link"] = _url(props.get("Link", {}))

    for name in _SELECT_FIELDS:
        flat[name] = _select_name(props.get(name, {}))
    for name in _MULTI_FIELDS:
        flat[name] = _multi_names(props.get(name, {}))
    for name in _NUMBER_FIELDS:
        flat[name] = _number(props.get(name, {}))
    for name in _TEXT_FIELDS:
        flat[name] = _rich_text(props.get(name, {}))
    for name in _DATE_FIELDS:
        flat[name] = _date_iso(props.get(name, {}))

    # Real Notion data is never sample data. The committed seed
    # ``docs/listings.json`` hardcodes ``sample: true`` so the map shows a
    # banner before the first live run; the morning export overwrites that
    # seed with these flagged-false rows, which auto-clears the banner.
    flat["sample"] = False

    return flat


# --------------------------------------------------------------------------- #
# Notion query (read-only, paginated)                                          #
# --------------------------------------------------------------------------- #
def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20))
def _query_page(http: httpx.Client, dsid: str, body: dict) -> dict:
    resp = http.post(f"{API}/data_sources/{dsid}/query", json=body)
    resp.raise_for_status()
    return resp.json()


def query_all_pages(
    data_source_id: str, *, token: str, http: httpx.Client | None = None
) -> list[dict]:
    """Return every page in the data source, following ``has_more``/``next_cursor``.

    Mirrors ``NotionSync._query`` pagination: at most 100 results per call.
    """
    owns = http is None
    http = http or httpx.Client(timeout=30.0, headers=_headers(token))
    try:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = _query_page(http, data_source_id, body)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:  # defensive: has_more but no cursor -> stop
                break
        return results
    finally:
        if owns:
            http.close()


def _score_key(row: dict) -> float:
    score = row.get("Score")
    return float(score) if isinstance(score, (int, float)) else -1.0


def export_listings(
    data_source_id: str,
    out_path: str | Path = "docs/listings.json",
    *,
    token: str | None = None,
    http: httpx.Client | None = None,
) -> list[dict]:
    """Query Notion, flatten every page, sort by Score desc, write JSON array.

    Args:
        data_source_id: The Notion data source (DB) id to export.
        out_path: Destination JSON file (default ``docs/listings.json``).
        token: Notion token; defaults to ``$NOTION_TOKEN``.
        http: Optional pre-built client (tests inject a MockTransport client).

    Returns:
        The list of flattened listing dicts that was written.
    """
    token = token if token is not None else os.environ.get("NOTION_TOKEN", "")
    pages = query_all_pages(data_source_id, token=token, http=http)
    rows = [page_to_flat(p) for p in pages]
    rows.sort(key=_score_key, reverse=True)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    return rows


def main(argv: list[str] | None = None) -> int:
    """Console entry ``property-scout-export``. Read-only; WARNs without a token."""
    parser = argparse.ArgumentParser(
        prog="property-scout-export",
        description="Export the Notion pipeline DB to a flat listings.json for the map.",
    )
    parser.add_argument(
        "--data-source-id",
        default=None,
        help="Notion data source id (defaults to the configured pipeline DB).",
    )
    parser.add_argument(
        "--out",
        default="docs/listings.json",
        help="Output JSON path (default: docs/listings.json).",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        # Don't hard-fail in CI/local without creds; the committed seed file
        # already lets the page render. Just warn and exit 0.
        print(
            "WARN: NOTION_TOKEN not set — skipping export "
            "(the committed docs/listings.json seed remains in place).",
        )
        return 0

    dsid = args.data_source_id or pipeline_data_source_id()
    if not dsid:
        print("WARN: NOTION_PIPELINE_DATA_SOURCE_ID not set; skipping export.")
        return 0
    rows = export_listings(dsid, args.out, token=token)
    print(f"Exported {len(rows)} listings to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
