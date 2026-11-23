"""USPS-style address normalization and cross-MLS dedupe.

A single property frequently appears on more than one board (e.g. a Fairview
home that straddles the Bergen/Hudson marketing line lands on both NJMLS and
HCMLS). We normalize addresses to a canonical form, then collapse duplicates,
keeping the *richest* record and recording every MLS number we saw it under.
"""

from __future__ import annotations

import re

try:
    import usaddress
except Exception:  # pragma: no cover - optional dependency at runtime
    usaddress = None

from property_scout.models import Listing

# Common USPS street-suffix abbreviations (subset; extend as needed).
_SUFFIX_MAP = {
    "AVENUE": "AVE", "AV": "AVE", "AVE": "AVE",
    "STREET": "ST", "STR": "ST", "ST": "ST",
    "ROAD": "RD", "RD": "RD",
    "DRIVE": "DR", "DR": "DR",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CT": "CT",
    "PLACE": "PL", "PL": "PL",
    "BOULEVARD": "BLVD", "BLVD": "BLVD",
    "TERRACE": "TER", "TER": "TER",
    "CIRCLE": "CIR", "CIR": "CIR",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "PARKWAY": "PKWY", "PKWY": "PKWY",
}
_DIRECTION_MAP = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
_UNIT_MAP = {"APARTMENT": "UNIT", "APT": "UNIT", "UNIT": "UNIT", "#": "UNIT", "SUITE": "STE"}
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_WS = re.compile(r"\s+")


def _token_normalize(token: str) -> str:
    upper = token.upper()
    return _DIRECTION_MAP.get(upper, _SUFFIX_MAP.get(upper, _UNIT_MAP.get(upper, upper)))


def normalize_address(address: str) -> str:
    """Return a canonical, comparison-friendly form of a street address.

    Uppercases, strips punctuation, expands/standardizes directionals,
    suffixes, and unit designators. Falls back to a regex tokenizer if the
    optional ``usaddress`` parser is unavailable.

    Example:
        >>> normalize_address("412 Example Avenue, Fairview, NJ 07022")
        '412 EXAMPLE AVE FAIRVIEW NJ 07022'
    """
    cleaned = _NON_ALNUM.sub(" ", address.upper())
    cleaned = _WS.sub(" ", cleaned).strip()
    if usaddress is not None:
        try:
            tagged, _ = usaddress.tag(cleaned)
            parts = [_token_normalize(v) for v in tagged.values()]
            return _WS.sub(" ", " ".join(parts)).strip()
        except Exception:
            pass  # fall through to token path
    return " ".join(_token_normalize(t) for t in cleaned.split())


def annotate_normalized(listings: list[Listing]) -> list[Listing]:
    """Populate ``normalized_address`` on each listing in place; returns it."""
    for listing in listings:
        listing.normalized_address = normalize_address(listing.address)
    return listings


def _richness(listing: Listing) -> int:
    """Count populated, signal-bearing fields — higher wins a dedupe tie."""
    fields = (
        listing.list_price, listing.beds, listing.baths, listing.sqft,
        listing.year_built, listing.property_type, listing.days_on_market,
        listing.hoa_per_month, listing.taxes_per_year, listing.listing_url,
        listing.latitude, listing.longitude,
    )
    base = sum(1 for f in fields if f not in (None, ""))
    # Prefer records with a non-empty raw description (drives analyze.py).
    if listing.raw.get("description"):
        base += 1
    return base


def dedupe(listings: list[Listing]) -> list[Listing]:
    """Collapse cross-MLS duplicates by normalized address.

    For each address cluster, keep the richest record and merge the others'
    MLS numbers into ``also_seen_mls`` (so the agent can cross-reference both
    boards). The kept record's own ``mls_number`` is never added to that list.

    Returns:
        Deduped listings, order-stable on first appearance of each address.
    """
    annotate_normalized(listings)
    order: list[str] = []
    best: dict[str, Listing] = {}
    seen_mls: dict[str, set[str]] = {}

    for listing in listings:
        key = listing.dedupe_key
        mls_tag = f"{listing.source.value}:{listing.mls_number}"
        if key not in best:
            order.append(key)
            best[key] = listing
            seen_mls[key] = {mls_tag}
            continue
        seen_mls[key].add(mls_tag)
        if _richness(listing) > _richness(best[key]):
            best[key] = listing

    result: list[Listing] = []
    for key in order:
        winner = best[key]
        winner_tag = f"{winner.source.value}:{winner.mls_number}"
        winner.also_seen_mls = sorted(seen_mls[key] - {winner_tag})
        result.append(winner)
    return result
