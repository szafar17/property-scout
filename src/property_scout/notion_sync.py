"""Upsert scored listings into the Notion pipeline database.

Keyed on (normalized address + MLS#). Manages status transitions and never
deletes a row:

- A new row is created with status ``New Today``.
- Next day, an unchanged listing flips ``New Today`` -> ``Active``.
- A list-price change flips status to ``Price Drop`` and appends to the
  ``Price History`` text.
- After two consecutive runs without seeing the listing, it flips to
  ``Off Market`` (a per-row miss counter is tracked in ``Flags`` metadata).

Uses the Notion 2025-09 *data source* API: query/create against the
``data_source_id``, update via ``/pages/{id}``.
"""

from __future__ import annotations

import os
from datetime import date

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from property_scout.config import NOTION_VERSION
from property_scout.models import Listing, ListingStatus
from property_scout.normalize import normalize_address

API = "https://api.notion.com/v1"


class NotionSync:
    """Thin client wrapping the upsert + status-transition logic."""

    def __init__(self, data_source_id: str, *, token: str | None = None,
                 http: httpx.Client | None = None) -> None:
        self._dsid = data_source_id
        token = token or os.environ.get("NOTION_TOKEN", "")
        self._http = http or httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )

    # -- public ------------------------------------------------------------ #
    def upsert_all(self, listings: list[Listing]) -> dict[str, int]:
        """Upsert a batch; returns counts of created/updated/transitioned rows."""
        stats = {"created": 0, "updated": 0, "off_market": 0}
        seen_keys: set[str] = set()
        for listing in listings:
            seen_keys.add(listing.dedupe_key)
            existing = self._find_existing(listing)
            if existing is None:
                self._create(listing)
                stats["created"] += 1
            else:
                self._update(existing, listing)
                stats["updated"] += 1
        stats["off_market"] = self._sweep_missing(seen_keys)
        return stats

    # -- query ------------------------------------------------------------- #
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20))
    def _query_page(self, body: dict) -> dict:
        """POST one page of a data-source query and return the raw response."""
        resp = self._http.post(f"{API}/data_sources/{self._dsid}/query", json=body)
        resp.raise_for_status()
        return resp.json()

    def _query(self, filter_body: dict | None = None) -> list[dict]:
        """Query the data source, following ``next_cursor`` to the last page.

        The Notion data-source query API returns at most ``page_size`` (max 100)
        results per call and signals more via ``has_more`` + ``next_cursor``
        (POST body ``start_cursor``). Earlier this only read page 1, so the
        off-market sweep never saw rows beyond the first 100 — those rows could
        never be flipped to Off Market. We now paginate fully.

        See https://developers.notion.com/reference/query-a-data-source and the
        pagination reference at
        https://developers.notion.com/reference/intro#pagination
        """
        results: list[dict] = []
        cursor: str | None = None
        while True:
            body: dict = {"page_size": 100}
            if filter_body:
                body["filter"] = filter_body
            if cursor:
                body["start_cursor"] = cursor
            data = self._query_page(body)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:  # defensive: has_more but no cursor → stop
                break
        return results

    def _find_existing(self, listing: Listing) -> dict | None:
        """Find a page by normalized address (title) + MLS number."""
        results = self._query(
            {
                "and": [
                    {"property": "Address", "title": {"equals": _title_value(listing)}},
                    {"property": "MLS Num", "rich_text": {"equals": listing.mls_number}},
                ]
            }
        )
        return results[0] if results else None

    # -- writes ------------------------------------------------------------ #
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20))
    def _create(self, listing: Listing) -> dict:
        listing.first_seen = listing.first_seen or date.today()
        listing.last_seen = date.today()
        listing.status = ListingStatus.NEW_TODAY
        resp = self._http.post(
            f"{API}/pages",
            json={
                "parent": {"type": "data_source_id", "data_source_id": self._dsid},
                "properties": listing_to_properties(listing),
            },
        )
        resp.raise_for_status()
        return resp.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20))
    def _update(self, page: dict, listing: Listing) -> dict:
        props = page.get("properties", {})
        prev_price = _number_prop(props, "List Price")
        prev_status = _select_prop(props, "Status")
        prev_history = _text_prop(props, "Price History")

        # Status transition rules.
        new_status = listing.status
        if listing.list_price is not None and prev_price is not None and \
                abs(listing.list_price - prev_price) >= 1:
            new_status = ListingStatus.PRICE_DROP if listing.list_price < prev_price \
                else ListingStatus.ACTIVE
            change = f"{date.today():%Y-%m-%d}: ${prev_price:,.0f} -> ${listing.list_price:,.0f}"
            listing.price_history_note = (prev_history + "\n" + change).strip() if prev_history \
                else change
        elif prev_status == ListingStatus.NEW_TODAY.value:
            new_status = ListingStatus.ACTIVE
        listing.status = new_status
        listing.last_seen = date.today()

        resp = self._http.patch(
            f"{API}/pages/{page['id']}",
            json={"properties": listing_to_properties(listing, include_added=False)},
        )
        resp.raise_for_status()
        return resp.json()

    def _sweep_missing(self, seen_keys: set[str]) -> int:
        """Flip rows not seen this run toward Off Market (2 consecutive misses).

        The miss counter is encoded in the ``Flags`` text as ``miss:N``.
        """
        off = 0
        active = self._query(
            {"property": "Status", "select": {"does_not_equal": ListingStatus.OFF_MARKET.value}}
        )
        for page in active:
            props = page["properties"]
            # ``seen_keys`` holds ``Listing.dedupe_key`` values, which are
            # ``normalize_address(address).upper().strip()`` (see models.py +
            # normalize.py). The stored Notion title is the *raw* address, so we
            # must run it through the same normalization before comparing —
            # otherwise an active listing whose raw address differs from its
            # normalized form ("Avenue" vs "AVE") is wrongly swept Off Market.
            key = normalize_address(_text_prop(props, "Address") or "").upper().strip()
            if key in seen_keys:
                continue
            misses = _miss_count(_text_prop(props, "Flags")) + 1
            new_status = ListingStatus.OFF_MARKET.value if misses >= 2 else \
                _select_prop(props, "Status")
            if new_status == ListingStatus.OFF_MARKET.value:
                off += 1
            self._http.patch(
                f"{API}/pages/{page['id']}",
                json={
                    "properties": {
                        "Status": {"select": {"name": new_status}},
                        "Flags": _rich_text(f"miss:{misses}"),
                    }
                },
            ).raise_for_status()
        return off


# --------------------------------------------------------------------------- #
# Property mapping (Listing -> Notion properties payload)                      #
# --------------------------------------------------------------------------- #
def _title_value(listing: Listing) -> str:
    return listing.address


def _rich_text(value: str | None) -> dict:
    return {"rich_text": [{"text": {"content": value}}] if value else []}


def _select(value: str | None) -> dict:
    return {"select": {"name": value}} if value else {"select": None}


def _multi(values: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in values[:100]]}


def _number(value: float | None) -> dict:
    return {"number": value}


def listing_to_properties(listing: Listing, *, include_added: bool = True) -> dict:
    """Map a :class:`Listing` to the exact Notion property payload.

    Column names match the database schema precisely (see README / runbook).
    """
    props: dict[str, dict] = {
        "Address": {"title": [{"text": {"content": listing.address}}]},
        "Town": _rich_text(listing.town),
        "County": _select(listing.county.value),
        "MLS": _select(listing.source.value),
        "MLS Num": _rich_text(listing.mls_number),
        "List Price": _number(listing.list_price),
        "Beds": _number(listing.beds),
        "Baths": _number(listing.baths),
        "SqFt": _number(listing.sqft),
        "$ per SqFt": _number(listing.price_per_sqft),
        "Year Built": _number(listing.year_built),
        "Type": _select(listing.property_type.value if listing.property_type else None),
        "Status": _select(listing.status.value),
        "Trend Grade": _select(listing.trend_grade),
        "20Y Appr Pct": _number(listing.appr_20y_pct),
        "Safety 0-10": _number(listing.safety_score),
        "Walk Score": _number(listing.walk_score),
        "Commute Min": _number(listing.commute_minutes),
        "HOA per Mo": _number(listing.hoa_per_month),
        "Taxes per Yr": _number(listing.taxes_per_year),
        "DOM": _number(listing.days_on_market),
        "Fixer Signals": _multi(listing.fixer_signals),
        "Reno Items": _multi(listing.reno_items),
        "Flags": _rich_text(listing.flags),
        "Last Reno": _rich_text(listing.last_reno),
        "Price History": _rich_text(listing.price_history_note),
        "Pros": _rich_text(listing.pros),
        "Score": _number(listing.score),
        "Latitude": _number(listing.latitude),
        "Longitude": _number(listing.longitude),
        "Link": {"url": listing.listing_url},
        "Last Seen": {"date": {"start": (listing.last_seen or date.today()).isoformat()}},
    }
    if include_added and listing.first_seen:
        props["Added"] = {"date": {"start": listing.first_seen.isoformat()}}
    return props


# --------------------------------------------------------------------------- #
# Read helpers for existing Notion pages                                       #
# --------------------------------------------------------------------------- #
def _number_prop(props: dict, name: str) -> float | None:
    return props.get(name, {}).get("number")


def _select_prop(props: dict, name: str) -> str | None:
    sel = props.get(name, {}).get("select")
    return sel.get("name") if sel else None


def _text_prop(props: dict, name: str) -> str | None:
    field = props.get(name, {})
    rich = field.get("rich_text") or field.get("title") or []
    return "".join(rt.get("plain_text", "") for rt in rich) or None


def _miss_count(flags: str | None) -> int:
    if not flags:
        return 0
    for token in flags.split():
        if token.startswith("miss:"):
            try:
                return int(token.split(":", 1)[1])
            except ValueError:
                return 0
    return 0
