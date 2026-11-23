# Property Scout

Daily real-estate deal-flow pipeline for a licensed NJ buyer's agent hunting
undervalued fixer-uppers across **Bergen, Passaic, and Hudson** counties.

At 06:00 America/New_York the pipeline reads its config from a Notion page, pulls
active MLS listings from two boards, filters, dedupes, enriches and scores them
0 to 100, and upserts the results into a Notion pipeline database that powers a
map and ranked views.

This replaced an earlier email-parsing MVP with a proper API ingest.

> **Status.** The pipeline runs end to end offline on bundled, fictional sample
> listings (`--sources mock`), and the 100-test suite runs in CI on pull requests and pushes to main.
> Both MLS adapters are tested against mocked feed responses. Connecting them to a
> live feed requires an MLS data license and credentials (see
> [`docs/NJMLS_feed_request_email.md`](docs/NJMLS_feed_request_email.md)).
> **No MLS data is included in this repository.**

---

## Architecture

```
                          ┌──────────────────────────────────────────────┐
                          │  Notion config page (NOTION_CONFIG_PAGE_ID)   │
                          │   • ```yaml config block  • runbook  • Run Log│
                          └───────────────┬──────────────────────────────┘
                                          │ config.py: fetch + extract + pydantic
                                          ▼
  ┌─────────── ingest/ (source-agnostic) ────────────┐
  │  reso_trestle.py   HCMLS  → Trestle RESO/OData    │
  │  njmls_readvantage.py NJMLS → RESO or RETS (auto) │   → List[Listing]
  │  mock.py           offline sample JSON            │
  │  base.py           BaseListingSource + registry   │
  └───────────────────────┬───────────────────────────┘
                          ▼
        normalize.py  ── USPS normalize + cross-MLS dedupe (keep richest)
                          ▼
        enrich/  geocode (Census → Google) · zhvi (Zillow appreciation, A–F)
                 crime (FBI CDE → 0–10) · convenience (Walk Score + Google Routes)
                 records (NJ parcels, best-effort)
                          ▼
        analyze.py  ── 1 Claude call per new listing → strict JSON
                       (fixer signals, last reno, red flags, pros)
                          ▼
        score.py  ── 6 subscores, min-max 0–10 within the pool,
                     weighted → 0–100 (carry costs inverted)
                          ▼
        notion_sync.py  ── upsert keyed on (norm address + MLS#),
                           status transitions, write lat/lng, never delete
                          ▼
        run.py  ── orchestrate, structured logs, 10-minute budget check,
                   append Run Log, write healthcheck.json

        mcp_server.py  ── read-only MCP tools over the scored export:
                          search, explain a score, try new weights
```

The Notion DB feeds a map view (via `Latitude`/`Longitude`) and ranked board
views (via `Score`, `Trend Grade`, `Status`).

---

## Setup

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env          # then fill in credentials
make test                     # runs green offline (MockSource)
make run-mock                 # dry-run the whole pipeline, no network
```

Run for real once credentials are in `.env`:

```bash
uv run property-scout --sources hcmls,njmls
```

Useful flags: `--check` (preflight credentials, no writes, non-zero on FAIL),
`--dry-run` (skip Notion upsert), `--no-enrich` (skip network enrichment),
`--config-file config.sample.yaml` (use local YAML instead of Notion).

---

## Getting each MLS feed

### HCMLS (Hudson County): CoreLogic Trestle, RESO Web API
1. Request the **Liberty Board of REALTORS® / HCMLS** data feed at
   <https://trestle.corelogic.com>.
2. CoreLogic issues an **OAuth2 client-credentials** pair (Client ID + Secret).
3. Set `TRESTLE_CLIENT_ID`, `TRESTLE_CLIENT_SECRET`, `TRESTLE_TOKEN_URL`,
   `TRESTLE_BASE_URL` in `.env`.
4. `ingest/reso_trestle.py` exchanges the secret for a bearer token, then issues
   OData `$filter` queries against the RESO **Property** resource.

### NJMLS (Bergen / Passaic): RE/Advantage IDX/data feed
1. Execute an **NJMLS IDX/data agreement**; the feed is delivered through
   **RE/Advantage**.
2. RE/Advantage may provision either a **RESO Web API** or a classic **RETS**
   endpoint. `ingest/njmls_readvantage.py` handles both: with
   `NJMLS_TRANSPORT=auto` (the default) it probes for RESO Web API and falls back
   to RETS (Login → capability discovery → DMQL2 `Search` → COMPACT-DECODED parse).
3. For RETS, set `NJMLS_READVANTAGE_LOGIN_URL`, `_USERNAME`, `_PASSWORD` (and
   `_USER_AGENT` / `_USER_AGENT_PASSWORD` if RE/Advantage requires a RETS UA).
   For RESO Web API, set `NJMLS_RESO_BASE_URL`, `NJMLS_RESO_CLIENT_ID`,
   `NJMLS_RESO_CLIENT_SECRET`, `NJMLS_RESO_TOKEN_URL`.

Both adapters implement `BaseListingSource` and return the same normalized
`Listing`, so everything downstream is board-agnostic. `MockSource` reads
`data/sample_listings.json` for offline work.

---

## Config lives in Notion (not in code)

`config.py` fetches the Notion config page, extracts the first ```yaml fenced
block, and validates it with pydantic (`AppConfig`). The agent edits filters,
scoring weights, the commute anchor, and fixer keywords **in Notion**; no code
change or redeploy needed. A malformed edit fails the run loudly (for example,
weights that don't sum to 100). See `config.sample.yaml` for the schema.

Point the pipeline at your workspace with two IDs in `.env`:
- `NOTION_CONFIG_PAGE_ID`: the config/runbook page
- `NOTION_PIPELINE_DATA_SOURCE_ID`: the pipeline database's data source

`notion_sync.py` uses Notion's **data-source** API
(`POST /v1/data_sources/{id}/query`, page parents of type `data_source_id`),
which requires `Notion-Version: 2025-09-03` (set in `config.py`). The query is
fully paginated via `start_cursor`/`has_more` so the off-market sweep sees the
whole DB, not just the first 100 rows.

---

## Scoring

Six subscores, each min-max normalized to 0–10 **within the day's candidate
pool**, then weighted (weights from Notion config, must sum to 100):

| Subscore               | Weight | Higher score when...                      |
|------------------------|:------:|-------------------------------------------|
| price_vs_area_value    |   25   | lower $/sqft vs the pool                  |
| fixer_upside           |   20   | more fixer signals, older home            |
| trend_20y              |   15   | higher 20-year appreciation               |
| safety                 |   15   | higher municipal safety (0–10)            |
| location_convenience   |   15   | higher Walk Score, shorter commute        |
| carry_costs            |   10   | lower taxes + HOA (**inverted**)          |

Missing data for a subscore defaults to the neutral midpoint (5/10) so one gap
never zeroes out a strong listing. The fixer signals come from `analyze.py`
(Claude reading the listing remarks) and degrade to keyword matching when no
`ANTHROPIC_API_KEY` is set.

---

## Deploy on GitHub Actions (primary)

The pipeline is designed to run unattended on a free GitHub Actions cron, with
all credentials stored as encrypted repository secrets.

### 1. Create the repo

```bash
git init && git add -A && git commit -m "property-scout"
gh repo create property-scout --private --source=. --push
```

Keep your deployment repo private: it holds your MLS credentials, and its
workflow runs against live data.

The workflow file `.github/workflows/daily.yml` is already included.

### 2. Add the repository secrets

In **GitHub → repo → Settings → Secrets and variables → Actions → New
repository secret**, add each of these (exact names; the workflow reads them
verbatim):

| Secret name | What it is |
|---|---|
| `NOTION_TOKEN` | Notion internal-integration token (required) |
| `NOTION_CONFIG_PAGE_ID` | Notion config/runbook page ID (required) |
| `NOTION_PIPELINE_DATA_SOURCE_ID` | Notion pipeline data source ID (required) |
| `ANTHROPIC_API_KEY` | Claude key for per-listing analysis (optional; degrades gracefully) |
| `TRESTLE_CLIENT_ID` | HCMLS / CoreLogic Trestle OAuth2 client ID |
| `TRESTLE_CLIENT_SECRET` | Trestle OAuth2 client secret |
| `TRESTLE_TOKEN_URL` | Trestle token endpoint (e.g. `https://api-trestle.corelogic.com/trestle/oidc/connect/token`) |
| `TRESTLE_BASE_URL` | Trestle OData base (e.g. `https://api-trestle.corelogic.com/trestle/odata`) |
| `NJMLS_READVANTAGE_LOGIN_URL` | NJMLS RE/Advantage login URL |
| `NJMLS_READVANTAGE_USERNAME` | NJMLS feed username |
| `NJMLS_READVANTAGE_PASSWORD` | NJMLS feed password |
| `NJMLS_READVANTAGE_USER_AGENT` | RETS User-Agent (optional; defaults to `property-scout/0.1`) |
| `NJMLS_READVANTAGE_USER_AGENT_PASSWORD` | RETS UA password / `RETS-UA-Authorization` (optional) |
| `NJMLS_RESO_BASE_URL`, `NJMLS_RESO_CLIENT_ID`, `NJMLS_RESO_CLIENT_SECRET`, `NJMLS_RESO_TOKEN_URL` | NJMLS RESO Web API credentials, if provisioned instead of RETS |
| `WALKSCORE_KEY` | Walk Score API key (optional) |
| `GOOGLE_MAPS_KEY` | Google Maps Platform key for Geocoding + Routes (optional) |
| `DATA_GOV_KEY` | api.data.gov key for the FBI Crime Data Explorer (optional; `FBI_CDE_KEY` also works) |

Optional secrets that are absent simply WARN in the preflight and that
enrichment is skipped; the run still succeeds. A useful run needs the Notion
values plus at least one MLS feed's credentials.

### 3. Cron schedule (DST-robust)

GitHub cron is **always UTC** and ignores `TZ`, so a fixed UTC time would drift
an hour twice a year. The workflow therefore runs **hourly** and gates the job
to the single hour that is `06:00` in `America/New_York` that day, firing once
daily at 06:00 Eastern through both EDT and EST with no manual DST maintenance.
(GitHub-hosted cron can lag a few minutes under load.) Until the `NOTION_TOKEN`
secret is configured, scheduled runs skip cleanly instead of failing.

### 4. Manual run

Go to **Actions → property-scout-daily → Run workflow** (the
`workflow_dispatch` trigger). A manual run bypasses the time gate and executes
immediately; use it for the first smoke test.

### 5. Logs & healthcheck

- **Logs:** Actions → the run → the `Run pipeline` step (structured JSON logs).
- **Healthcheck:** every run uploads `healthcheck.json` as a build artifact
  (`healthcheck-<run_id>`), recording `ok`, `scored`, and `elapsed_seconds`.
  Set `PS_HEALTHCHECK` to write it somewhere else.
- **Run Log:** a real (non-dry) run also appends a one-line summary to the Notion
  config page's Run Log, followed by a short **Top New Today** digest (top 3 by
  score: address, score, price, one-line why).

The job runs `property-scout --check` (preflight) **before** the pipeline and
fails loudly (non-zero exit) on a Notion upsert error or a blown run budget.

### CI

`.github/workflows/ci.yml` runs on pull requests and pushes to main: `pytest`, an
offline mock run of the full pipeline, `ruff`, and `mypy`.

---

## First-run checklist

1. **Preflight the credentials** (writes nothing to Notion):
   ```bash
   property-scout --check          # or: property-scout-check
   ```
   Confirm Notion is `PASS` and your MLS feed(s) are `PASS`. Missing optional
   keys showing `WARN` is fine. Any `FAIL` exits non-zero.
2. **Dry-run the full pipeline** (no Notion writes):
   ```bash
   property-scout --sources hcmls,njmls --dry-run
   ```
   Inspect `healthcheck.json` and the logged top scores. Add `--no-enrich` for a
   fully offline pass, or `--sources mock` to use the bundled sample data.
3. **One real manual run:** push to GitHub, then **Actions → Run workflow**.
   Verify rows appear in the Notion DB and a Run Log line is appended.
4. **Enable cron:** nothing to toggle. The hourly schedule in `daily.yml` is
   active once the workflow is on the default branch and self-gates to 06:00
   Eastern.

---

## Scheduling alternative: macOS launchd

To run on an always-on Mac instead of GitHub Actions:

```bash
make install-schedule     # installs com.propertyscout.daily, 06:00 ET daily
make uninstall-schedule
```

The plist sets `TZ=America/New_York`; logs land in `logs/`. Provide credentials
via a local `.env` (see `.env.example`) rather than GitHub secrets.

---

## Live map (GitHub Pages)

A self-contained interactive map (`docs/index.html`, Leaflet) plots every
listing as a score-colored pin with filters, popup cards, and a ranked side
list, plus a **Today digest**: counts of **New Today** and **Price Drop** and
the top 3 New-Today listings by score.

The page loads its data at runtime from `./listings.json`. The committed seed
`docs/listings.json` holds **fictional sample listings** so the page renders
before the first run.

Publishing is **opt-in**. With the repository variable `PUBLISH_PAGES=true`, the
daily workflow refreshes `listings.json` with `property-scout-export` (a read-only
Notion query) and deploys `./docs` via the official Pages actions
(`configure-pages` → `upload-pages-artifact` → `deploy-pages`). Also set
**Settings → Pages → Build and deployment → Source = GitHub Actions**.

> Publishing real listings on a public page is governed by your MLS's IDX
> display rules. Check your data license before pointing the export at live data.

```bash
property-scout-export --out docs/listings.json   # read-only; skips without a token
```

---

## MCP server (for AI agents)

`property-scout-mcp` exposes the scored listings to an AI agent (Claude Desktop,
Claude Code, or any MCP client) over stdio. It reads the same flat
`listings.json` the map uses and recomputes scores with `score.py`, so every
number it returns can be explained. All four tools are read-only.

| Tool | What it does |
|---|---|
| `search_listings` | Ranks listings best first, with filters for town, county, max price, min beds, min score, and fixer-only |
| `explain_score` | Breaks one listing's score into its six weighted subscores, their inputs, and Claude's read of the remarks |
| `try_weights` | Re-ranks the pool under new weights (only the ones you change; the rest stay put), shows rank movement, and returns the yaml to paste into the Notion config. Saves nothing |
| `current_weights` | Shows the weights in use and what each subscore measures |

So an agent can answer "which Hudson listings under $450k have the most fixer
upside?", "why did this one score 66?", or "what changes if safety counts twice
as much?" and hand back the config change to make.

```bash
uv pip install -e ".[mcp]"
property-scout-mcp --listings docs/listings.json --config-file config.sample.yaml
```

Point it at the daily export (`property-scout-export --out listings.json`) for
live data; it reloads the file whenever it changes. Claude Desktop config:

```json
{
  "mcpServers": {
    "property-scout": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/property-scout", "--extra", "mcp",
               "property-scout-mcp", "--listings", "docs/listings.json"]
    }
  }
}
```

Claude Code: `claude mcp add property-scout -- uv run --directory /path/to/property-scout --extra mcp property-scout-mcp --listings docs/listings.json`

---

## Notion database columns

`Address`(title), `Town`, `County`, `MLS`, `MLS Num`, `List Price`, `Beds`,
`Baths`, `SqFt`, `$ per SqFt`, `Year Built`, `Type`, `Status`, `Trend Grade`,
`20Y Appr Pct`, `Safety 0-10`, `Walk Score`, `Commute Min`, `HOA per Mo`,
`Taxes per Yr`, `DOM`, `Fixer Signals`, `Reno Items`, `Flags`, `Last Reno`,
`Price History`, `Pros`, `Score`, `Latitude`, `Longitude`, `Link`, `Added`,
`Last Seen`.

## Status lifecycle

`New Today` → `Active` (next day) → `Price Drop` (on price change, appends to
`Price History`) → `Off Market` (after 2 consecutive misses). Rows are never
deleted.

---

## Tests

```bash
make test        # 100 tests, no network or credentials needed
```

| Area | File(s) |
|---|---|
| Scoring | `test_score.py` |
| Address normalization + cross-MLS dedupe | `test_normalize.py` |
| MLS transport: RESO/OData vs RETS/DMQL, auto-detect + fallback | `test_njmls.py` |
| Enrichment: geocoding, Zillow ZHVI, crime, convenience, parcel records | `test_enrich_*.py` |
| Remarks analysis: keyword fallback, signal merge | `test_analyze.py` |
| Config extraction + validation | `test_config.py` |
| Off-market sweep + miss-counter transitions | `test_notion_sweep.py` |
| Map export, run digest, mock end-to-end run, preflight | `test_export_map.py`, `test_run_digest.py`, `test_run_mock.py`, `test_cli.py` |
| MCP server: search, explain, weight what-ifs, client round trip | `test_mcp_server.py` |

---

## Data and licensing

- No MLS listing data, credentials, or Notion workspace IDs are committed.
- `data/sample_listings.json` and `docs/listings.json` are fictional: street
  names are placeholders ("Example Ave", "Sample St") and MLS numbers are made up.
- MLS feeds are licensed per participant; this code does not grant or imply access.

## License

[MIT](LICENSE) © 2026 Sahil Zafar
