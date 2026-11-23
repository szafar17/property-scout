"""Preflight credential check — validate secrets before the first 6 AM run.

``property-scout --check`` (or ``python -m property_scout.cli``) probes every
external dependency the pipeline needs, using only the configured environment
variables, and prints a PASS / FAIL / WARN table. It **never writes anything to
Notion** and never mutates external state — it is read-only by construction:

- Notion: reads the pipeline *data source schema* (GET /v1/data_sources/{id}).
- Trestle (HCMLS): requests one OAuth2 client-credentials token.
- NJMLS (RE/Advantage): performs a RETS Login (no Search).
- WalkScore / Google Maps: presence-only (no paid call is made on a probe).

A missing credential is reported as **WARN** and skipped (so a partially
configured environment doesn't hard-fail). Any genuine FAIL — a present
credential that does not work — makes the process exit non-zero, so CI/cron can
gate the real run on a clean preflight.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import dataclass

import httpx

from property_scout.config import (
    NOTION_VERSION,
    pipeline_data_source_id,
)

API = "https://api.notion.com/v1"


@dataclass(slots=True)
class CheckResult:
    """One row of the preflight table."""

    name: str
    status: str  # "PASS" | "FAIL" | "WARN"
    detail: str = ""

    @property
    def ok(self) -> bool:
        # WARN (missing optional cred) does not fail the run; only FAIL does.
        return self.status != "FAIL"


def _check_notion(http: httpx.Client) -> CheckResult:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return CheckResult("Notion token + data source", "WARN", "NOTION_TOKEN not set")
    dsid = pipeline_data_source_id()
    if not dsid:
        return CheckResult(
            "Notion token + data source", "FAIL", "NOTION_PIPELINE_DATA_SOURCE_ID not set"
        )
    try:
        resp = http.get(
            f"{API}/data_sources/{dsid}",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
            },
        )
        if resp.status_code != 200:
            return CheckResult(
                "Notion token + data source", "FAIL",
                f"HTTP {resp.status_code}: {resp.text[:160]}",
            )
        body = resp.json()
        if body.get("object") != "data_source":
            return CheckResult(
                "Notion token + data source", "FAIL",
                f"unexpected object {body.get('object')!r}",
            )
        props = body.get("properties", {})
        return CheckResult(
            "Notion token + data source", "PASS",
            f"schema readable ({len(props)} properties)",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Notion token + data source", "FAIL", str(exc))


def _check_trestle(http: httpx.Client) -> CheckResult:
    client_id = os.environ.get("TRESTLE_CLIENT_ID")
    client_secret = os.environ.get("TRESTLE_CLIENT_SECRET")
    token_url = os.environ.get("TRESTLE_TOKEN_URL")
    if not (client_id and client_secret and token_url):
        return CheckResult("Trestle OAuth (HCMLS)", "WARN", "TRESTLE_* not fully set")
    try:
        # Build a throwaway auth manager so we exercise the real token path.
        from property_scout.ingest.reso_trestle import TrestleAuth

        auth = TrestleAuth(client_id, client_secret, token_url, http=http)
        tok = auth.token()
        return CheckResult("Trestle OAuth (HCMLS)", "PASS", f"token len {len(tok)}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Trestle OAuth (HCMLS)", "FAIL", str(exc))


def _check_njmls() -> CheckResult:
    login_url = os.environ.get("NJMLS_READVANTAGE_LOGIN_URL")
    username = os.environ.get("NJMLS_READVANTAGE_USERNAME")
    password = os.environ.get("NJMLS_READVANTAGE_PASSWORD")
    if not (login_url and username and password):
        return CheckResult(
            "NJMLS RETS login (RE/Advantage)", "WARN",
            "NJMLS_READVANTAGE_* not fully set",
        )
    try:
        from property_scout.ingest.njmls_readvantage import RetsClient

        client = RetsClient(login_url, username, password)
        caps = client.login()
        with suppress(Exception):
            client.logout()
        if not caps:
            return CheckResult(
                "NJMLS RETS login (RE/Advantage)", "FAIL",
                "login returned no capability URLs",
            )
        return CheckResult(
            "NJMLS RETS login (RE/Advantage)", "PASS",
            f"{len(caps)} capabilities, Search={'Search' in caps}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("NJMLS RETS login (RE/Advantage)", "FAIL", str(exc))


def _check_presence(name: str, env: str) -> CheckResult:
    return (
        CheckResult(name, "PASS", "present")
        if os.environ.get(env)
        else CheckResult(name, "WARN", f"{env} not set")
    )


def run_checks(*, http: httpx.Client | None = None) -> list[CheckResult]:
    """Run every preflight probe and return the result rows (no writes)."""
    owns = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        return [
            _check_notion(http),
            _check_trestle(http),
            _check_njmls(),
            _check_presence("Anthropic key (analysis)", "ANTHROPIC_API_KEY"),
            _check_presence("WalkScore key", "WALKSCORE_KEY"),
            _check_presence("Google Maps key", "GOOGLE_MAPS_KEY"),
        ]
    finally:
        if owns:
            http.close()


def format_table(results: list[CheckResult]) -> str:
    """Render the results as an aligned PASS/FAIL/WARN table."""
    width = max((len(r.name) for r in results), default=10)
    lines = [f"{'CHECK'.ljust(width)}  STATUS  DETAIL", f"{'-' * width}  ------  ------"]
    for r in results:
        lines.append(f"{r.name.ljust(width)}  {r.status.ljust(6)}  {r.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the standalone preflight check. Exits non-zero on FAIL."""
    results = run_checks()
    print(format_table(results))
    failed = [r for r in results if r.status == "FAIL"]
    warned = [r for r in results if r.status == "WARN"]
    print()
    print(
        f"Summary: {sum(r.status == 'PASS' for r in results)} pass, "
        f"{len(warned)} warn, {len(failed)} fail."
    )
    if failed:
        print("FAIL — fix the credentials above before running the pipeline.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
