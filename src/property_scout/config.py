"""Runtime configuration, sourced from a Notion page — never hardcoded.

The agent reads a ```yaml fenced block from the body of the Notion page
"Property Scout — Daily Fixer-Upper Pipeline". This keeps filter criteria,
scoring weights, and commute anchors editable by the agent without code
changes. The YAML is validated with pydantic so a malformed edit fails loud
and early rather than silently skewing scores.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

import httpx
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Notion REST API version. The data-source endpoints used by notion_sync.py
# (POST /v1/data_sources/{id}/query, page parents of type "data_source_id")
# require >= 2025-09-03. See:
#   https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03
#   https://developers.notion.com/reference/query-a-data-source
NOTION_VERSION = "2025-09-03"

_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Validated config schema                                                     #
# --------------------------------------------------------------------------- #
class FilterCriteria(BaseModel):
    """Hard filters applied right after ingest."""

    counties: list[str] = Field(default_factory=lambda: ["Bergen", "Passaic", "Hudson"])
    min_price: float | None = None
    max_price: float | None = None
    min_beds: int | None = None
    property_types: list[str] = Field(
        default_factory=lambda: ["Single Family", "Multi-Family", "Townhouse", "Condo"]
    )
    max_days_on_market: int | None = None
    statuses: list[str] = Field(default_factory=lambda: ["Active", "New Today", "Price Drop"])


class ScoringWeights(BaseModel):
    """Weighted contribution of each subscore. Must sum to 100."""

    price_vs_area_value: int = 25
    fixer_upside: int = 20
    trend_20y: int = 15
    safety: int = 15
    location_convenience: int = 15
    carry_costs: int = 10

    @model_validator(mode="after")
    def _must_sum_to_100(self) -> ScoringWeights:
        total = sum(self.model_dump().values())
        if total != 100:
            raise ValueError(f"scoring weights must sum to 100, got {total}")
        return self


class CommuteAnchor(BaseModel):
    """Destination used to compute commute time in enrich/convenience.py."""

    label: str = "NYC Port Authority"
    address: str = "625 8th Ave, New York, NY 10018"
    mode: str = "transit"  # one of: transit, driving, walking, bicycling
    arrival_local_time: str = "09:00"  # HH:MM, America/New_York


class AppConfig(BaseModel):
    """Top-level validated config parsed from the Notion YAML block."""

    filters: FilterCriteria = Field(default_factory=FilterCriteria)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    commute_anchor: CommuteAnchor = Field(default_factory=CommuteAnchor)
    fixer_keywords: list[str] = Field(
        default_factory=lambda: [
            "tlc", "handyman", "as-is", "as is", "estate sale", "investor",
            "needs work", "fixer", "cash only", "bring all offers", "sweat equity",
            "renovation", "rehab", "value add", "diamond in the rough",
        ]
    )
    run_budget_seconds: int = 600

    @field_validator("fixer_keywords")
    @classmethod
    def _lowercase(cls, v: list[str]) -> list[str]:
        return [k.lower() for k in v]


# --------------------------------------------------------------------------- #
# Notion fetch + parse                                                         #
# --------------------------------------------------------------------------- #
def _notion_headers() -> dict[str, str]:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN is not set; cannot fetch config from Notion.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _block_plain_text(block: dict) -> str:
    """Concatenate rich_text of a single Notion block."""
    btype = block.get("type")
    payload = block.get(btype, {}) if btype else {}
    rich = payload.get("rich_text", []) if isinstance(payload, dict) else []
    return "".join(rt.get("plain_text", "") for rt in rich)


def fetch_page_text(page_id: str, *, client: httpx.Client | None = None) -> str:
    """Return the concatenated plain text of all blocks on a Notion page.

    Paginates through ``/blocks/{id}/children``. Code blocks are returned with
    fences so the ```yaml extractor can find the config block.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        out: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = client.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=_notion_headers(),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            for block in data.get("results", []):
                if block.get("type") == "code":
                    lang = block["code"].get("language", "")
                    out.append(f"```{lang}\n{_block_plain_text(block)}\n```")
                else:
                    out.append(_block_plain_text(block))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(out)
    finally:
        if owns_client:
            client.close()


def extract_yaml_block(page_text: str) -> str:
    """Pull the first ```yaml fenced block out of page text.

    Raises ``ValueError`` if none is found, so a missing config block fails the
    run rather than silently using defaults.
    """
    match = _YAML_BLOCK_RE.search(page_text)
    if not match:
        raise ValueError("No ```yaml config block found on the Notion config page.")
    return match.group(1)


def parse_config(yaml_text: str) -> AppConfig:
    """Parse and validate a YAML config string into :class:`AppConfig`."""
    data = yaml.safe_load(yaml_text) or {}
    return AppConfig.model_validate(data)


@lru_cache(maxsize=1)
def load_config(page_id: str | None = None) -> AppConfig:
    """Fetch, extract, parse, and validate config from Notion (cached per run).

    Args:
        page_id: Override the config page id; defaults to ``NOTION_CONFIG_PAGE_ID``.
    """
    pid = page_id or notion_config_page_id()
    if not pid:
        raise RuntimeError("NOTION_CONFIG_PAGE_ID is not set (see .env.example)")
    text = fetch_page_text(pid)
    return parse_config(extract_yaml_block(text))


def notion_config_page_id() -> str:
    """Return the Notion config page id from ``NOTION_CONFIG_PAGE_ID`` ("" when unset)."""
    return os.environ.get("NOTION_CONFIG_PAGE_ID", "").strip()


def pipeline_data_source_id() -> str:
    """Return the Notion pipeline data source id ("" when unset)."""
    return os.environ.get("NOTION_PIPELINE_DATA_SOURCE_ID", "").strip()
