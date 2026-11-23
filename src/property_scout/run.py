"""Pipeline orchestrator — the 06:00 America/New_York daily entrypoint.

Stages: load config (Notion) -> ingest (MLS adapters) -> filter -> dedupe ->
geocode -> enrich (appreciation/safety/convenience/records) -> analyze (Claude)
-> score -> upsert (Notion) -> append a run summary to the Notion Run Log.

Designed to finish inside a <10 minute budget with structured logging and
exponential backoff inside each network client. A failure in one enrichment
provider degrades that listing's data rather than aborting the run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

import httpx
import structlog

from property_scout.analyze import analyze_listing, apply_analysis
from property_scout.config import (
    NOTION_VERSION,
    AppConfig,
    load_config,
    notion_config_page_id,
    parse_config,
    pipeline_data_source_id,
)
from property_scout.enrich.convenience import enrich_convenience
from property_scout.enrich.geocode import enrich_geocode
from property_scout.enrich.records import enrich_records
from property_scout.enrich.zhvi import download_zhvi, enrich_appreciation, load_zhvi
from property_scout.ingest.base import BaseListingSource, build_source, fetch_all
from property_scout.models import Listing
from property_scout.normalize import dedupe
from property_scout.notion_sync import NotionSync
from property_scout.score import score_pool

log = structlog.get_logger("property_scout")
HEALTHCHECK_PATH = Path(os.environ.get("PS_HEALTHCHECK", "healthcheck.json"))


def _filter_listings(listings: list[Listing], config: AppConfig) -> list[Listing]:
    """Apply the configured hard filters (counties / price / beds / type / DOM)."""
    f = config.filters
    out: list[Listing] = []
    for listing in listings:
        if f.counties and listing.county.value not in f.counties:
            continue
        if f.min_price and (listing.list_price or 0) < f.min_price:
            continue
        if f.max_price and (listing.list_price or 0) > f.max_price:
            continue
        if f.min_beds and (listing.beds or 0) < f.min_beds:
            continue
        if f.property_types and listing.property_type and \
                listing.property_type.value not in f.property_types:
            continue
        if f.max_days_on_market and (listing.days_on_market or 0) > f.max_days_on_market:
            continue
        out.append(listing)
    return out


def _county_median_appr(listings: list[Listing], df, county: str) -> float:
    """Median 20y appreciation across the pool's ZIPs for grading. Best-effort."""
    from property_scout.enrich.zhvi import _zip_of, compute_appreciation

    vals: list[float] = []
    for listing in listings:
        if listing.county.value != county:
            continue
        zc = _zip_of(listing.address)
        if not zc:
            continue
        res = compute_appreciation(df, zc)
        if res and "20y" in res.appr_pct:
            vals.append(res.appr_pct["20y"])
    if not vals:
        return 0.0
    vals.sort()
    return vals[len(vals) // 2]


def run_pipeline(
    config: AppConfig,
    sources: list[BaseListingSource],
    *,
    notion: NotionSync | None = None,
    enrich: bool = True,
) -> dict:
    """Execute the full pipeline and return a run-summary dict.

    Args:
        config: Validated config (filters, weights, anchor).
        sources: Instantiated ingest adapters.
        notion: Sync client; if ``None``, the upsert stage is skipped (dry run).
        enrich: When ``False``, skip all network enrichment (fast offline path).
    """
    started = time.monotonic()
    summary: dict = {"date": date.today().isoformat(), "errors": []}

    # 1. Ingest
    listings = fetch_all(sources, config.filters)
    summary["ingested"] = len(listings)
    log.info("ingested", count=len(listings))

    # 2. Filter + 3. Dedupe
    listings = _filter_listings(listings, config)
    listings = dedupe(listings)
    summary["after_dedupe"] = len(listings)
    log.info("deduped", count=len(listings))

    if enrich:
        http = httpx.Client(timeout=30.0)
        try:
            # 4. Geocode (required for map + convenience)
            for listing in listings:
                try:
                    enrich_geocode(listing, http=http)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"geocode {listing.mls_number}: {exc}")

            # 5a. Appreciation (one ZHVI load shared across the pool)
            try:
                download_zhvi(http=http)
                df = load_zhvi()
                medians = {
                    c: _county_median_appr(listings, df, c)
                    for c in {listing.county.value for listing in listings}
                }
                for listing in listings:
                    enrich_appreciation(listing, df, medians.get(listing.county.value, 0.0))
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"zhvi: {exc}")

            # 5b. Convenience + records (per-listing, best-effort)
            for listing in listings:
                try:
                    enrich_convenience(listing, config.commute_anchor, http=http)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"convenience {listing.mls_number}: {exc}")
                with contextlib.suppress(Exception):  # records never block
                    enrich_records(listing, http=http)
        finally:
            http.close()

    # 6. Analyze (Claude, per new listing)
    for listing in listings:
        try:
            apply_analysis(listing, analyze_listing(listing, config))
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"analyze {listing.mls_number}: {exc}")

    # 7. Score
    score_pool(listings, config.weights)
    summary["scored"] = len(listings)
    summary["top_score"] = listings[0].score if listings else None

    # 8. Upsert
    if notion is not None:
        try:
            summary["notion"] = notion.upsert_all(listings)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"notion: {exc}")

    summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    summary["within_budget"] = summary["elapsed_seconds"] <= config.run_budget_seconds
    summary["listings"] = [_summarize(listing) for listing in listings]
    return summary


def _summarize(listing: Listing) -> dict:
    return {
        "address": listing.address,
        "town": listing.town,
        "mls": f"{listing.source.value}:{listing.mls_number}",
        "also_seen": listing.also_seen_mls,
        "price": listing.list_price,
        "score": listing.score,
        "status": listing.status.value,
        "trend_grade": listing.trend_grade,
        "fixer_signals": list(listing.fixer_signals),
        "subscores": listing.subscores,
    }


def _fmt_price(price: float | None) -> str:
    """Render a list price like ``$355,000`` (or ``$?`` when unknown)."""
    return f"${price:,.0f}" if isinstance(price, (int, float)) else "$?"


def _digest_why(item: dict) -> str:
    """One-line "why" for a listing: top fixer signal + trend grade."""
    signals = item.get("fixer_signals") or []
    top_signal = signals[0] if signals else None
    grade = item.get("trend_grade")
    parts: list[str] = []
    if top_signal:
        parts.append(top_signal)
    if grade:
        parts.append(f"trend {grade}")
    return ", ".join(parts) if parts else "no fixer signal yet"


def new_today_digest(summary: dict, *, limit: int = 3) -> list[dict]:
    """Top ``limit`` New-Today listings by score, for the Run Log + map digest.

    Reads from ``summary["listings"]`` (the per-listing dicts from
    :func:`_summarize`). Returns an empty list when there are no New-Today rows.
    """
    new_today = [
        item for item in summary.get("listings", [])
        if item.get("status") == "New Today"
    ]
    new_today.sort(key=lambda i: (i.get("score") or -1), reverse=True)
    return new_today[:limit]


def append_run_log(page_id: str, summary: dict) -> None:
    """Append a one-line run summary plus a top-3 New-Today digest to the Run Log.

    Two blocks are appended (idempotent-friendly — only ever appends, never
    edits/deletes): a summary paragraph, then a short bulleted digest of the
    top New-Today listings (address, score, price, one-line why). No-ops cleanly
    when ``NOTION_TOKEN`` is absent.
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return
    text = (
        f"{datetime.now():%Y-%m-%d %H:%M} ET — ingested {summary.get('ingested', 0)}, "
        f"deduped {summary.get('after_dedupe', 0)}, scored {summary.get('scored', 0)}, "
        f"top {summary.get('top_score')}, {summary.get('elapsed_seconds', 0)}s, "
        f"errors {len(summary.get('errors', []))}."
    )

    children: list[dict] = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }
    ]

    digest = new_today_digest(summary)
    if digest:
        children.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "Top New Today:"}}
                    ]
                },
            }
        )
        for item in digest:
            addr = item.get("address", "")
            score = item.get("score")
            line = (
                f"{addr} — {score} · {_fmt_price(item.get('price'))} · {_digest_why(item)}"
            )
            children.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    },
                }
            )

    with httpx.Client(timeout=30.0) as http:
        http.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            json={"children": children},
        )


def write_healthcheck(summary: dict) -> None:
    """Persist a small healthcheck JSON for external monitors."""
    HEALTHCHECK_PATH.write_text(
        json.dumps(
            {
                "ts": datetime.now().isoformat(),
                "ok": not summary.get("errors"),
                "scored": summary.get("scored", 0),
                "elapsed_seconds": summary.get("elapsed_seconds"),
            },
            indent=2,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Property Scout daily pipeline")
    parser.add_argument(
        "--sources", default="hcmls,njmls",
        help="Comma-separated source names (e.g. 'hcmls,njmls' or 'mock').",
    )
    parser.add_argument("--config-file", help="Local YAML config instead of Notion.")
    parser.add_argument("--dry-run", action="store_true", help="Skip the Notion upsert.")
    parser.add_argument("--no-enrich", action="store_true", help="Skip network enrichment.")
    parser.add_argument(
        "--check", action="store_true",
        help="Preflight: validate creds (Notion/Trestle/NJMLS/keys) and exit. "
             "Writes nothing to Notion; non-zero exit on any FAIL.",
    )
    args = parser.parse_args(argv)

    # Preflight short-circuits everything else and never touches Notion writes.
    if args.check:
        from property_scout.cli import main as check_main
        return check_main()

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    # Ensure adapters register themselves.
    import property_scout.ingest.mock  # noqa: F401
    import property_scout.ingest.njmls_readvantage  # noqa: F401
    import property_scout.ingest.reso_trestle  # noqa: F401

    if args.config_file:
        config = parse_config(Path(args.config_file).read_text())
    elif os.environ.get("NOTION_TOKEN"):
        config = load_config()
    else:
        # Fully offline / dry runs (e.g. `--sources mock --dry-run --no-enrich`)
        # shouldn't require a Notion token just to read config — fall back to
        # validated defaults so the mock pipeline runs without any creds.
        log.warning("no-notion-token-using-default-config")
        config = AppConfig()

    sources = [build_source(name.strip()) for name in args.sources.split(",") if name.strip()]
    notion = None if args.dry_run else NotionSync(pipeline_data_source_id())

    summary = run_pipeline(config, sources, notion=notion, enrich=not args.no_enrich)
    write_healthcheck(summary)
    config_page_id = notion_config_page_id()
    if not args.dry_run and config_page_id:
        append_run_log(config_page_id, summary)
    log.info(
        "run-complete",
        **{k: summary[k] for k in ("scored", "elapsed_seconds", "within_budget")},
    )

    # Exit non-zero so GitHub Actions fails the job loudly on a *critical*
    # failure: a Notion upsert error or a blown run budget. Per-listing
    # enrichment errors (geocode/convenience/records/analyze) are best-effort
    # and do NOT fail the job — they only degrade one row's data.
    critical = [e for e in summary.get("errors", []) if e.startswith("notion:")]
    if critical:
        for err in critical:
            log.error("critical-error", error=err)
        return 1
    return 0 if summary.get("within_budget", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
