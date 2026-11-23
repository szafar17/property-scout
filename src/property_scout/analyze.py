"""Per-listing qualitative analysis via one Claude API call.

For each *new* listing we ask Claude to read the MLS remarks/description and
return a strict JSON object: fixer signals, last renovation (year + items),
red-flag list, and 2-3 buyer pros. The call is forced to emit JSON matching a
fixed schema and retried once on a parse/transport failure.

Requires ``ANTHROPIC_API_KEY``. No-ops gracefully (returns empty analysis) when
the key is absent or the listing has no descriptive text, so offline/mock runs
never fail here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from property_scout.config import AppConfig
from property_scout.models import Listing

MODEL = "claude-sonnet-4-5"  # cost-effective for short structured extraction

_SYSTEM = (
    "You are a real-estate analyst assisting a licensed NJ buyer's agent who "
    "hunts undervalued fixer-uppers. Read the listing remarks and return STRICT "
    "JSON only, no prose. Be conservative: only report a renovation year if the "
    "text states or strongly implies one."
)

_SCHEMA_HINT = {
    "fixer_signals": ["short keyword phrases, e.g. 'as-is', 'original kitchen'"],
    "reno_items": ["specific items needing work, e.g. 'roof', 'kitchen', 'HVAC'"],
    "last_reno_year": "integer year or null",
    "last_reno_items": ["items renovated, if any"],
    "red_flags": ["concerns, e.g. 'flood zone', 'tenant-occupied'"],
    "pros": ["2-3 concise buyer-facing positives"],
}


@dataclass(slots=True)
class Analysis:
    """Structured output of the Claude analysis call."""

    fixer_signals: list[str] = field(default_factory=list)
    reno_items: list[str] = field(default_factory=list)
    last_reno_year: int | None = None
    last_reno_items: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    pros: list[str] = field(default_factory=list)


def _keyword_prescreen(text: str, keywords: list[str]) -> list[str]:
    """Cheap local keyword pass; seeds/falls back when the API is unavailable."""
    lower = text.lower()
    return [k for k in keywords if k in lower]


def _merge_signals(primary: list[str], keyword_hits: list[str]) -> list[str]:
    """Union analysis signals with config-keyword hits, order-stable, no dups.

    Guarantees the configured ``fixer_keywords`` always drive the "Fixer
    Signals" column even when Claude's extraction misses an exact phrase the
    agent told us to watch for. Comparison is case-insensitive.
    """
    out: list[str] = []
    seen: set[str] = set()
    for sig in [*primary, *keyword_hits]:
        norm = sig.strip().lower()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(sig)
    return out


def _build_prompt(listing: Listing) -> str:
    desc = listing.raw.get("description") or listing.raw.get("PublicRemarks") or ""
    return (
        f"Listing: {listing.address}\n"
        f"Type: {listing.property_type.value if listing.property_type else 'unknown'}, "
        f"Beds {listing.beds}, Baths {listing.baths}, "
        f"Year built {listing.year_built}, List ${listing.list_price}\n\n"
        f"MLS remarks:\n\"\"\"\n{desc}\n\"\"\"\n\n"
        f"Return JSON exactly matching these keys:\n{json.dumps(_SCHEMA_HINT, indent=2)}"
    )


def _parse(content: str) -> Analysis:
    """Parse Claude's JSON response into an :class:`Analysis`."""
    # Tolerate code fences if the model wraps the JSON.
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    data = json.loads(content)
    return Analysis(
        fixer_signals=list(data.get("fixer_signals", [])),
        reno_items=list(data.get("reno_items", [])),
        last_reno_year=data.get("last_reno_year"),
        last_reno_items=list(data.get("last_reno_items", [])),
        red_flags=list(data.get("red_flags", [])),
        pros=list(data.get("pros", [])),
    )


def analyze_listing(listing: Listing, config: AppConfig) -> Analysis:
    """Run one Claude call to extract structured signals; retry once.

    Falls back to a keyword-only :class:`Analysis` if no API key is configured
    or the description is empty, so the pipeline degrades gracefully offline.
    """
    desc = listing.raw.get("description") or listing.raw.get("PublicRemarks") or ""
    keyword_hits = _keyword_prescreen(desc, config.fixer_keywords)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not desc.strip():
        return Analysis(fixer_signals=keyword_hits)

    try:
        from anthropic import Anthropic
    except ImportError:  # pragma: no cover
        return Analysis(fixer_signals=keyword_hits)

    client = Anthropic(api_key=api_key)
    prompt = _build_prompt(listing)
    last_err: Exception | None = None
    for _ in range(2):  # one initial + one retry
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=700,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(block.text for block in resp.content if block.type == "text")
            analysis = _parse(text)
            # Always fold in config-keyword hits so fixer_keywords reliably
            # drive the "Fixer Signals" column, even if Claude omitted one.
            analysis.fixer_signals = _merge_signals(analysis.fixer_signals, keyword_hits)
            return analysis
        except Exception as exc:  # noqa: BLE001 - retry-once contract
            last_err = exc
            continue
    # Both attempts failed; degrade to keyword signals rather than crashing.
    return Analysis(fixer_signals=keyword_hits or [f"analysis-failed: {last_err}"])


def apply_analysis(listing: Listing, analysis: Analysis) -> Listing:
    """Write an :class:`Analysis` onto a listing's analysis fields."""
    listing.fixer_signals = analysis.fixer_signals
    listing.reno_items = analysis.reno_items
    if analysis.last_reno_year or analysis.last_reno_items:
        items = ", ".join(analysis.last_reno_items)
        year = analysis.last_reno_year or ""
        listing.last_reno = f"{year} {items}".strip()
    listing.flags = "; ".join(analysis.red_flags) if analysis.red_flags else None
    listing.pros = " | ".join(analysis.pros) if analysis.pros else None
    return listing
