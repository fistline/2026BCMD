"""Agent hook: Scrapling MCP server for fetching pages the corpus lacks.

This node's FIRST_SOURCE is `inbox_documents`, so Scrapling is NOT a dependency
of the build and nothing in the pipeline imports it. This module is the seam:
it reports whether the hook is available and prints the MCP client
configuration, so wiring up web fetching is a config change rather than a code
change.

Order of preference when the agent needs something that is not indexed:
  1. an official API for the data (stable, documented, permitted),
  2. Scrapling for the public page -- a stealth browser if it bot-blocks.
Be polite (rate-limit, take only what you need) and fetch only PUBLIC pages, not
authenticated/private data you are not entitled to (§ invariant 8).

Anything fetched is external input. Land it in `data/inbox/documents/` and let
the normal pipeline promote, transform and index it, so retrieved material
carries the same provenance as everything else. Never write it straight into
`data/raw` or the serving index.

Enable it:
    uv add "scrapling[ai]"
    uv run scrapling install
    claude mcp add ScraplingServer "$(pwd)/.venv/bin/scrapling" mcp

CLI:
    uv run python -m agent.tools.scrapling_mcp --config
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

SERVER_NAME = "ScraplingServer"
INSTALL_HINT = 'uv add "scrapling[ai]" && uv run scrapling install'


def _scrapling_executable() -> str | None:
    """Prefer the copy inside this project's venv over anything on PATH."""
    candidate = Path(sys.executable).parent / "scrapling"
    if candidate.exists():
        return str(candidate)
    return shutil.which("scrapling")


def scrapling_available() -> bool:
    return importlib.util.find_spec("scrapling") is not None


def mcp_server_config(http: bool = False, host: str = "127.0.0.1", port: int = 8000) -> dict:
    """MCP client entry for Scrapling's built-in server.

    stdio is the default transport (`scrapling mcp`). `--http` switches to
    Streamable HTTP, which is what you want when several agents share one
    fetcher process rather than each spawning its own.
    """
    executable = _scrapling_executable() or "scrapling"
    args = ["mcp"]
    if http:
        args.extend(["--http", "--host", host, "--port", str(port)])
    return {"mcpServers": {SERVER_NAME: {"command": executable, "args": args}}}


def scrapling_mcp_status() -> dict:
    """Report whether the hook can be enabled, without importing Scrapling."""
    executable = _scrapling_executable()
    installed = scrapling_available()
    return {
        "server_name": SERVER_NAME,
        "package_installed": installed,
        "executable": executable,
        "enabled": bool(installed and executable),
        "install_hint": INSTALL_HINT,
        "register_hint": f'claude mcp add {SERVER_NAME} "{executable or "scrapling"}" mcp',
        "policy": (
            "API first, then scrape public pages (a stealth browser if bot-blocked). "
            "Be polite and fetch only public data. Land fetched pages in "
            "data/inbox/documents/ so they are promoted, transformed and indexed "
            "with normal provenance."
        ),
    }


def fetch(url: str, stealthy: bool = False, headless: bool = True):
    """Fetch one page directly, for use outside an MCP client.

    Mirrors the documented Scrapling API: `Fetcher.fetch(url)` for ordinary
    pages, and `StealthyFetcher.fetch(url, headless=True, network_idle=True)`
    when a page needs a real browser to render. Returns Scrapling's page object,
    which exposes `.css(selector)` for extraction.
    """
    if not scrapling_available():
        raise RuntimeError(
            f"scrapling is not installed. Enable this hook with: {INSTALL_HINT}"
        )

    from scrapling.fetchers import Fetcher, StealthyFetcher

    if stealthy:
        return StealthyFetcher.fetch(url, headless=headless, network_idle=True)
    return Fetcher.fetch(url)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", action="store_true", help="Print the MCP client JSON entry.")
    parser.add_argument("--http", action="store_true", help="Configure Streamable HTTP transport.")
    args = parser.parse_args(argv)

    if args.config:
        print(json.dumps(mcp_server_config(http=args.http), indent=2))
        return 0

    status = scrapling_mcp_status()
    print(json.dumps(status, indent=2))
    return 0 if status["enabled"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
