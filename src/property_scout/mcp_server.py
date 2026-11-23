"""MCP server so AI agents can query scored listings and tune the scoring weights.

It reads the same flat ``listings.json`` that powers the map:
``property-scout-export`` writes it from Notion each morning, and
``docs/listings.json`` ships fictional sample rows for offline use. Scores are
recomputed with :mod:`property_scout.score` over the loaded pool, so every
number an agent sees can be explained subscore by subscore.

Everything here is read-only. ``try_weights`` re-ranks in memory and returns the
YAML block to paste into the Notion config page if you decide to keep the new
weights; nothing is written anywhere.

Run over stdio:  ``property-scout-mcp --listings docs/listings.json``
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from property_scout.config import ScoringWeights, parse_config
from property_scout.models import County, Listing, ListingStatus, MlsBoard, PropertyType
from property_scout.score import SUBSCORE_KEYS, score_pool

DEFAULT_LISTINGS = Path("docs/listings.json")
MAX_LIMIT = 50

SUBSCORE_HELP: dict[str, str] = {
    "price_vs_area_value": "Lower $/sqft than the rest of the pool scores higher.",
    "fixer_upside": (
        "More fixer signals and reno items (Claude's read of the listing remarks) "
        "plus an older home scores higher."
    ),
    "trend_20y": "Higher 20-year appreciation (Zillow ZHVI) scores higher.",
    "safety": "Higher municipal safety score (FBI Crime Data Explorer, 0-10) scores higher.",
    "location_convenience": "Higher Walk Score and a shorter commute score higher.",
    "carry_costs": "Lower annual taxes plus HOA scores higher (inverted).",
}


# --------------------------------------------------------------------------- #
# Loading: flat export rows -> Listing                                         #
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(round(number))


def _enum[E: Enum](enum_cls: type[E], value: Any) -> E | None:
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part) for part in value]


def listing_from_row(row: dict[str, Any]) -> Listing | None:
    """Turn one flat ``listings.json`` row into a :class:`Listing`.

    Returns ``None`` for rows that can't be identified (no MLS number, or a
    board/county this pipeline doesn't cover), so one bad row never breaks a load.
    """
    board = _enum(MlsBoard, row.get("MLS"))
    county = _enum(County, row.get("County"))
    mls_number = str(row.get("MLS Num") or "").strip()
    if board is None or county is None or not mls_number:
        return None
    return Listing(
        source=board,
        mls_number=mls_number,
        address=str(row.get("Address") or ""),
        town=str(row.get("Town") or ""),
        county=county,
        list_price=_num(row.get("List Price")),
        beds=_int(row.get("Beds")),
        baths=_num(row.get("Baths")),
        sqft=_int(row.get("SqFt")),
        year_built=_int(row.get("Year Built")),
        property_type=_enum(PropertyType, row.get("Type")),
        status=_enum(ListingStatus, row.get("Status")) or ListingStatus.ACTIVE,
        days_on_market=_int(row.get("DOM")),
        hoa_per_month=_num(row.get("HOA per Mo")),
        taxes_per_year=_num(row.get("Taxes per Yr")),
        listing_url=row.get("Link") or None,
        latitude=_num(row.get("Latitude")),
        longitude=_num(row.get("Longitude")),
        appr_20y_pct=_num(row.get("20Y Appr Pct")),
        trend_grade=row.get("Trend Grade") or None,
        safety_score=_num(row.get("Safety 0-10")),
        walk_score=_int(row.get("Walk Score")),
        commute_minutes=_num(row.get("Commute Min")),
        fixer_signals=_str_list(row.get("Fixer Signals")),
        reno_items=_str_list(row.get("Reno Items")),
        last_reno=row.get("Last Reno") or None,
        flags=row.get("Flags") or None,
        pros=row.get("Pros") or None,
        raw=row,
    )


def load_listings(path: Path) -> list[Listing]:
    """Load every usable row from a flat ``listings.json`` export."""
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError(f"{path} should hold a JSON list of listings")
    listings = [listing_from_row(row) for row in rows if isinstance(row, dict)]
    return [listing for listing in listings if listing is not None]


@dataclass
class ListingStore:
    """The listings an agent can query, reloaded whenever the export file changes."""

    path: Path
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    weights_source: str = "defaults"
    _mtime: float | None = field(default=None, init=False, repr=False)
    _listings: list[Listing] = field(default_factory=list, init=False, repr=False)

    def listings(self) -> list[Listing]:
        if not self.path.exists():
            raise ValueError(
                f"No listings file at {self.path}. Run `property-scout-export --out {self.path}`."
            )
        mtime = self.path.stat().st_mtime
        if mtime != self._mtime:
            self._listings = load_listings(self.path)
            self._mtime = mtime
        return self._listings

    def scored(self, weights: ScoringWeights | None = None) -> list[Listing]:
        """Score copies of the pool (best first); the loaded listings are never mutated."""
        pool = [dataclasses.replace(listing) for listing in self.listings()]
        return score_pool(pool, weights or self.weights)


# --------------------------------------------------------------------------- #
# Tool logic (plain Python, so it is easy to test without an MCP client)       #
# --------------------------------------------------------------------------- #
def _top_factors(listing: Listing, n: int = 2) -> list[str]:
    present = [(k, v) for k, v in listing.subscores.items() if v is not None]
    present.sort(key=lambda kv: kv[1], reverse=True)
    return [key for key, _ in present[:n]]


def _summary(listing: Listing, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "mls_number": listing.mls_number,
        "board": listing.source.value,
        "address": listing.address,
        "town": listing.town,
        "county": listing.county.value,
        "price": listing.list_price,
        "beds": listing.beds,
        "baths": listing.baths,
        "sqft": listing.sqft,
        "status": listing.status.value,
        "score": listing.score,
        "exported_score": listing.raw.get("Score"),
        "top_factors": _top_factors(listing),
        "fixer_signals": listing.fixer_signals,
        "flags": listing.flags,
        "link": listing.listing_url,
    }


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


@dataclass
class ScoutTools:
    """The four read-only tools, bound to one :class:`ListingStore`."""

    store: ListingStore

    def search_listings(
        self,
        town: str | None = None,
        county: str | None = None,
        max_price: float | None = None,
        min_beds: int | None = None,
        min_score: int | None = None,
        fixer_only: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for rank, listing in enumerate(self.store.scored(), start=1):
            if town and listing.town.lower() != town.strip().lower():
                continue
            if county and listing.county.value.lower() != county.strip().lower():
                continue
            if max_price is not None and (listing.list_price or 0) > max_price:
                continue
            if min_beds is not None and (listing.beds or 0) < min_beds:
                continue
            if min_score is not None and (listing.score or 0) < min_score:
                continue
            if fixer_only and not (listing.fixer_signals or listing.reno_items):
                continue
            results.append(_summary(listing, rank))
            if len(results) >= _clamp_limit(limit):
                break
        return results

    def explain_score(self, mls_number: str) -> dict[str, Any]:
        ranked = self.store.scored()
        wanted = mls_number.strip()
        matches = (
            (rank, item) for rank, item in enumerate(ranked, start=1) if item.mls_number == wanted
        )
        found = next(matches, None)
        if found is None:
            raise ValueError(
                f"No listing with MLS number {mls_number!r}. Use search_listings to find one."
            )
        rank, listing = found
        weights = self.store.weights.model_dump()
        breakdown = []
        for key in SUBSCORE_KEYS:
            value = listing.subscores.get(key)
            used = 5.0 if value is None else value
            breakdown.append(
                {
                    "subscore": key,
                    "value_0_to_10": value,
                    "missing_data_neutral_5": value is None,
                    "weight": weights[key],
                    "points": round(used / 10.0 * weights[key], 2),
                    "what_it_measures": SUBSCORE_HELP[key],
                }
            )
        return {
            "mls_number": listing.mls_number,
            "address": listing.address,
            "score": listing.score,
            "rank": rank,
            "pool_size": len(ranked),
            "breakdown": breakdown,
            "inputs": {
                "price": listing.list_price,
                "price_per_sqft": listing.price_per_sqft,
                "year_built": listing.year_built,
                "appreciation_20y_pct": listing.appr_20y_pct,
                "safety_0_to_10": listing.safety_score,
                "walk_score": listing.walk_score,
                "commute_minutes": listing.commute_minutes,
                "taxes_per_year": listing.taxes_per_year,
                "hoa_per_month": listing.hoa_per_month,
            },
            "remarks_analysis": {
                "fixer_signals": listing.fixer_signals,
                "reno_items": listing.reno_items,
                "last_reno": listing.last_reno,
                "flags": listing.flags,
                "pros": listing.pros,
            },
            "note": (
                "Subscores are min-max scaled to 0-10 within the loaded pool, so they "
                "shift as listings come and go."
            ),
        }

    def try_weights(self, weights: dict[str, int], limit: int = 10) -> dict[str, Any]:
        unknown = sorted(set(weights) - set(SUBSCORE_KEYS))
        if unknown:
            raise ValueError(f"Unknown subscore(s) {unknown}; valid: {list(SUBSCORE_KEYS)}")
        for key, value in weights.items():
            if not 0 <= value <= 100:
                raise ValueError(f"{key} must be between 0 and 100, got {value}")
        merged = {**self.store.weights.model_dump(), **weights}
        total = sum(merged.values())
        if total != 100:
            raise ValueError(
                f"Weights must sum to 100 (after filling unspecified ones from the "
                f"current weights), got {total}: {merged}"
            )
        proposed = ScoringWeights(**merged)

        before = {
            listing.mls_number: (rank, listing.score)
            for rank, listing in enumerate(self.store.scored(), start=1)
        }
        after = self.store.scored(proposed)
        top: list[dict[str, Any]] = []
        new_in_top: list[str] = []
        shown = _clamp_limit(limit)
        for rank, listing in enumerate(after[:shown], start=1):
            old_rank, old_score = before[listing.mls_number]
            if old_rank > shown:
                new_in_top.append(listing.mls_number)
            top.append(
                {
                    "rank": rank,
                    "previous_rank": old_rank,
                    "mls_number": listing.mls_number,
                    "address": listing.address,
                    "score": listing.score,
                    "previous_score": old_score,
                }
            )
        yaml_block = "weights:\n" + "".join(
            f"  {key}: {getattr(proposed, key)}\n" for key in SUBSCORE_KEYS
        )
        return {
            "weights": proposed.model_dump(),
            "top": top,
            "new_in_top": new_in_top,
            "yaml_for_notion_config": yaml_block,
            "note": (
                "Nothing was saved. To keep these weights, replace the weights section of "
                "the yaml block on the Notion config page."
            ),
        }

    def current_weights(self) -> dict[str, Any]:
        return {
            "weights": self.store.weights.model_dump(),
            "source": self.store.weights_source,
            "subscores": SUBSCORE_HELP,
            "listings_file": str(self.store.path),
            "listings_loaded": len(self.store.listings()),
        }


# --------------------------------------------------------------------------- #
# MCP wiring                                                                  #
# --------------------------------------------------------------------------- #
INSTRUCTIONS = (
    "Property Scout ranks NJ fixer-upper listings with a weighted 0-100 deal score. "
    "Use search_listings to find candidates, explain_score to see why a listing "
    "scored the way it did, and try_weights to test different scoring priorities "
    "before changing the live config. All tools are read-only."
)


def build_server(store: ListingStore) -> Any:
    """Create the MCP server (stdio) exposing the four read-only tools."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SystemExit(
            "The MCP server needs the 'mcp' package: pip install 'property-scout[mcp]'"
        ) from exc

    tools = ScoutTools(store)
    server = MCPServer(name="property-scout", instructions=INSTRUCTIONS)

    def checked[T](result: Callable[[], T]) -> T:
        # Bad input becomes a ToolError, so the agent sees the actual message.
        try:
            return result()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def search_listings(
        town: str | None = None,
        county: str | None = None,
        max_price: float | None = None,
        min_beds: int | None = None,
        min_score: int | None = None,
        fixer_only: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Rank scored listings, best first, with optional filters.

        county is Bergen, Passaic or Hudson. fixer_only keeps listings with at least
        one fixer signal or reno item. Each result's rank is its place in the whole
        pool, before filters.
        """
        return checked(
            lambda: tools.search_listings(
                town, county, max_price, min_beds, min_score, fixer_only, limit
            )
        )

    @server.tool(annotations=read_only)
    def explain_score(mls_number: str) -> dict[str, Any]:
        """Break one listing's score into its six weighted subscores and their inputs."""
        return checked(lambda: tools.explain_score(mls_number))

    @server.tool(annotations=read_only)
    def try_weights(
        price_vs_area_value: int | None = None,
        fixer_upside: int | None = None,
        trend_20y: int | None = None,
        safety: int | None = None,
        location_convenience: int | None = None,
        carry_costs: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Re-rank the pool under new weights without saving anything.

        Pass only the weights you want to change; the rest keep their current
        values. The final weights must sum to 100. Returns the new top listings
        with their previous ranks, plus the yaml to paste into the Notion config.
        """
        given = {
            "price_vs_area_value": price_vs_area_value,
            "fixer_upside": fixer_upside,
            "trend_20y": trend_20y,
            "safety": safety,
            "location_convenience": location_convenience,
            "carry_costs": carry_costs,
        }
        chosen = {k: v for k, v in given.items() if v is not None}
        return checked(lambda: tools.try_weights(chosen, limit))

    @server.tool(annotations=read_only)
    def current_weights() -> dict[str, Any]:
        """Show the weights in use, what each subscore measures, and the data source."""
        return checked(tools.current_weights)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="property-scout-mcp",
        description="MCP server (stdio) over scored Property Scout listings.",
    )
    parser.add_argument(
        "--listings",
        default=os.environ.get("PROPERTY_SCOUT_LISTINGS", str(DEFAULT_LISTINGS)),
        help="Flat listings.json export (default: docs/listings.json).",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="YAML config with the current weights (same schema as config.sample.yaml).",
    )
    args = parser.parse_args(argv)

    if args.config_file:
        weights = parse_config(Path(args.config_file).read_text()).weights
        source = args.config_file
    else:
        weights, source = ScoringWeights(), "defaults"
    store = ListingStore(Path(args.listings), weights=weights, weights_source=source)
    store.listings()  # fail fast on a missing or malformed file
    build_server(store).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
