"""Tests for the off-market sweep key matching and miss-counter transitions.

Covers the dedupe_key/Address normalization bug fix in
``NotionSync._sweep_missing``: a seen listing must stay put even when its
stored Notion title is the *raw* (un-normalized) address, while a genuinely
unseen listing increments a miss counter and flips to Off Market on the second
consecutive miss.
"""

from __future__ import annotations

from property_scout.models import County, Listing, ListingStatus, MlsBoard
from property_scout.normalize import normalize_address
from property_scout.notion_sync import NotionSync


class _FakePatch:
    def raise_for_status(self) -> None:  # noqa: D401 - httpx.Response shim
        return None


class _FakeHttp:
    """Records PATCH calls so we can assert on the status transitions written."""

    def __init__(self) -> None:
        self.patches: list[tuple[str, dict]] = []

    def patch(self, url: str, *, json: dict) -> _FakePatch:
        self.patches.append((url, json))
        return _FakePatch()


def _page(page_id: str, *, address: str, status: str, flags: str = "") -> dict:
    """Build a minimal Notion page object as returned by the query API."""
    return {
        "id": page_id,
        "properties": {
            "Address": {"title": [{"plain_text": address}]},
            "Status": {"select": {"name": status}},
            "Flags": {"rich_text": ([{"plain_text": flags}] if flags else [])},
        },
    }


def _sync_with_pages(pages: list[dict]) -> tuple[NotionSync, _FakeHttp]:
    http = _FakeHttp()
    sync = NotionSync("ds-test", token="t", http=http)  # type: ignore[arg-type]
    # Stub the query so _sweep_missing returns our canned "active" rows.
    sync._query = lambda filter_body=None: pages  # type: ignore[method-assign]
    return sync, http


def _seen_key(address: str) -> str:
    """Reproduce Listing.dedupe_key for an address we DID see this run."""
    listing = Listing(
        source=MlsBoard.HCMLS, mls_number="x", address=address,
        town="Fairview", county=County.HUDSON,
    )
    listing.normalized_address = normalize_address(address)
    return listing.dedupe_key


def test_seen_listing_is_not_swept_despite_raw_title():
    # Stored title is the RAW address ("Avenue"); seen_keys holds the
    # normalized form ("AVE"). The fixed sweep normalizes both sides.
    page = _page("p1", address="412 Example Avenue, Fairview, NJ 07022",
                 status=ListingStatus.ACTIVE.value)
    sync, http = _sync_with_pages([page])
    seen = {_seen_key("412 Example Ave, Fairview, NJ 07022")}

    off = sync._sweep_missing(seen)

    assert off == 0
    assert http.patches == []  # seen row left completely untouched


def test_unseen_listing_first_miss_increments_but_stays_active():
    page = _page("p2", address="9 Elm St, Fairview, NJ 07022",
                 status=ListingStatus.ACTIVE.value)
    sync, http = _sync_with_pages([page])

    off = sync._sweep_missing(seen_keys=set())  # nothing seen this run

    assert off == 0  # one miss is not enough
    assert len(http.patches) == 1
    _, body = http.patches[0]
    assert body["properties"]["Status"]["select"]["name"] == ListingStatus.ACTIVE.value
    assert body["properties"]["Flags"]["rich_text"][0]["text"]["content"] == "miss:1"


def test_unseen_listing_second_miss_flips_off_market():
    page = _page("p3", address="9 Elm St, Fairview, NJ 07022",
                 status=ListingStatus.ACTIVE.value, flags="miss:1")
    sync, http = _sync_with_pages([page])

    off = sync._sweep_missing(seen_keys=set())

    assert off == 1
    _, body = http.patches[0]
    assert body["properties"]["Status"]["select"]["name"] == ListingStatus.OFF_MARKET.value
    assert body["properties"]["Flags"]["rich_text"][0]["text"]["content"] == "miss:2"
