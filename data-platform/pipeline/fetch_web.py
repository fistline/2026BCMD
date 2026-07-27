"""Operator tool: land an EXTERNAL web file (PDF/HTML) in the inbox.

Sibling of `fetch_law.py` for sources that have NO official API. Same landing and
provenance: files go to `data/inbox/documents/` and through the normal pipeline,
never straight into `data/raw` or the serving index.

Policy (AGENTS.md invariant 8): **API first, then scrape.** If a source offers an
official API, use `fetch_law.py`. For sources with no API, scrape their PUBLIC
pages with `scrapling` — escalating as the page demands:

    static (Fetcher / stdlib) -> render (DynamicFetcher, runs JS) -> stealth
    (StealthyFetcher, a real browser that gets past indiscriminate bot-blocking).

`--stealth auto` (the default) starts light and escalates to a stealth browser
only when a page actively blocks a plain fetch, so most files never spin up a
browser. Be a good citizen: `--delay` rate-limits batch fetches, and this tool is
for PUBLIC, human-viewable pages -- not authenticated/private data you are not
entitled to. robots.txt is reported for awareness, not enforced as a wall.

Why this exists: DART 첨부(감정평가서·계약서·투자설명서 PDF) live behind the
`dart.fss.or.kr` viewer, and some issuers self-publish 증권신고서 on their own
site -- both public, neither with an attachment API. This lands them.

CLI:
    uv run python -m pipeline.fetch_web --url https://issuer.example/report.pdf
    uv run python -m pipeline.fetch_web --url https://issuer.example/ir --render
    uv run python -m pipeline.fetch_web --url <bot-blocked-public-url> --stealth always \
        --dest data/inbox/documents/sto --name 증권신고서_뮤직카우.pdf
    uv run python -m pipeline.fetch_web --from-manifest links.json --delay 3

Stealth/render need scrapling with a browser: `uv sync --extra web` then
`uv run scrapling install`. The plain static path is stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

from pipeline.fetch_law import FetchError, _decode, _http_get, _land

USER_AGENT = "local-agent-platform-fetch_web/0.2 (operator research tool)"

# Signatures of an ACTIVE bot-block, used to decide when to ESCALATE to a stealth
# browser (not to refuse). A blocking status alone is not enough: we require a
# vendor challenge header or a challenge body, so an ordinary 403/permission error
# or expired link surfaces its real HTTP error instead of triggering a browser spin-up.
_ANTIBOT_HEADERS = ("x-datadome", "x-iinfo", "x-sucuri-id")
_ANTIBOT_BODY = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "attention required",
    "ddos protection by",
    "enable javascript and cookies",
    "verify you are human",
    "captcha-delivery",
    "px-captcha",
)


class _ScraplingUnavailable(RuntimeError):
    """scrapling is not installed; the static path falls back to stdlib urllib."""


def is_antibot_response(status: int, headers: dict | None, body: str) -> str | None:
    """Return a reason string if the response is an active bot-block, else None.

    Conservative on purpose: a bare 403/permission error is NOT flagged, so we do
    not spin up a browser for a page that simply isn't there or isn't ours. We flag
    only an explicit vendor challenge header or a challenge body, optionally
    corroborated by a WAF edge returning a blocking status.
    """
    h = {str(k).lower(): str(v or "") for k, v in (headers or {}).items()}
    reasons: list[str] = []
    if h.get("cf-mitigated", "").lower() == "challenge":
        reasons.append("cf-mitigated=challenge")
    for name in _ANTIBOT_HEADERS:
        if name in h:
            reasons.append(f"header:{name}")
    low = (body or "")[:6000].lower()
    reasons += [f"body:{sig!r}" for sig in _ANTIBOT_BODY if sig in low]
    if not reasons:
        return None
    server = h.get("server", "").lower()
    if status in (403, 429, 503) and any(edge in server for edge in ("cloudflare", "sucuri")):
        reasons.append(f"{server or 'edge'} {status}")
    return "; ".join(dict.fromkeys(reasons))


def robots_allows(robots_text: str, url: str, user_agent: str = USER_AGENT) -> bool:
    """Pure verdict: does this robots.txt body permit `user_agent` to fetch `url`?

    Reported for awareness only (see robots_verdict); not enforced as a hard wall.
    An empty body (no rules) permits everything, per the robots convention.
    """
    parser = urllib.robotparser.RobotFileParser()
    parser.parse((robots_text or "").splitlines())
    return parser.can_fetch(user_agent, url)


def robots_verdict(url: str, user_agent: str = USER_AGENT) -> tuple[bool, str]:
    """Fetch and evaluate the host's robots.txt for `url`. Returns (allowed, reason).

    Informational: a Disallow is logged, not obeyed as a block. Unreachable
    robots.txt (5xx / connection reset, e.g. a WAF that kills the connection) is
    reported as such so the operator knows the host is bot-hostile before the
    stealth escalation kicks in.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"unsupported URL scheme {parts.scheme!r} (expected http/https): {url}")
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        text = _decode(_http_get(robots_url, timeout=15))
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500:
            return True, f"{parts.netloc}: no robots.txt ({error.code})"
        return False, f"{parts.netloc}/robots.txt {error.code} (host may be bot-hostile)"
    except Exception as error:  # noqa: BLE001
        return False, f"{parts.netloc}: robots.txt unreachable ({error}) — host may be bot-hostile"
    if robots_allows(text, url, user_agent):
        return True, f"{parts.netloc}/robots.txt allows this path"
    return False, f"{parts.netloc}/robots.txt disallows this path for * agents"


def _urllib_fetch(url: str, user_agent: str, timeout: int) -> tuple[int, dict, bytes]:
    """stdlib fetch. Captures a 4xx/5xx body and headers so a bot-block challenge
    can be inspected (and escalated) rather than lost to an exception."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status or 0), dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        body = b""
        try:
            body = error.read()
        except Exception:  # noqa: BLE001
            pass
        headers = dict(error.headers.items()) if error.headers else {}
        return int(error.code or 0), headers, body


def _scrapling_fetch(url: str, mode: str, timeout: int) -> tuple[int, dict, bytes]:
    """Fetch via scrapling. mode: 'static' | 'render' | 'stealth'. Raises
    _ScraplingUnavailable if scrapling is not installed."""
    try:
        from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
    except Exception as error:  # noqa: BLE001
        raise _ScraplingUnavailable(str(error)) from error
    if mode == "stealth":
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    elif mode == "render":
        page = DynamicFetcher.fetch(url, headless=True, network_idle=True)
    else:
        page = Fetcher.fetch(url)
    status = int(getattr(page, "status", 0) or 0)
    headers = dict(getattr(page, "headers", {}) or {})
    body = getattr(page, "body", None)
    if body is None:
        body = getattr(page, "html_content", "") or ""
    body_bytes = body.encode("utf-8", "replace") if isinstance(body, str) else bytes(body)
    return status, headers, body_bytes


def _fetch(url: str, mode: str, user_agent: str, timeout: int) -> tuple[int, dict, bytes]:
    if mode in ("render", "stealth"):
        try:
            return _scrapling_fetch(url, mode, timeout)
        except _ScraplingUnavailable as error:
            raise FetchError(
                f"--{mode} needs scrapling with a browser engine: {error}. Install it with "
                f"`uv sync --extra web` then `uv run scrapling install`."
            ) from error
    try:
        return _scrapling_fetch(url, "static", timeout)
    except _ScraplingUnavailable:
        return _urllib_fetch(url, user_agent, timeout)


def fetch_web(
    url: str,
    *,
    dest_dir: str | None = None,
    stealth: str = "auto",
    render: bool = False,
    name: str | None = None,
    user_agent: str = USER_AGENT,
    stream=sys.stdout,
) -> Path:
    """Land one external file in the inbox, escalating to a stealth browser if a
    plain fetch is actively bot-blocked (stealth='auto', the default)."""
    allowed, reason = robots_verdict(url, user_agent)
    print(f"robots: {reason}" + ("" if allowed else " — proceeding (public page; operator's call)"), file=stream)

    mode = "stealth" if stealth == "always" else ("render" if render else "static")
    status, headers, body = _fetch(url, mode, user_agent, 30)
    antibot = is_antibot_response(status, headers, body[:6000].decode("utf-8", "replace"))
    if antibot and stealth != "never" and mode != "stealth":
        print(f"bot-block detected ({antibot}); escalating to a stealth browser…", file=stream)
        mode = "stealth"
        status, headers, body = _fetch(url, "stealth", user_agent, 30)
        antibot = is_antibot_response(status, headers, body[:6000].decode("utf-8", "replace"))
    if antibot:
        raise FetchError(
            f"still bot-blocked after {mode} ({antibot}). Try `--stealth always`, or open "
            f"{url} in your browser and drop the file into data/inbox/documents/<collection>/."
        )
    if status >= 400 or not body:
        raise FetchError(f"fetch failed: HTTP {status}, {len(body)} bytes. Check the URL and your access.")

    filename = name or Path(urllib.parse.urlsplit(url).path).name or "download"
    path = _land(filename, body, Path(dest_dir) if dest_dir else None)
    print(f"landed: {path} ({len(body)} bytes, HTTP {status}, via {mode})", file=stream)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None, help="Single URL to fetch.")
    parser.add_argument("--from-manifest", default=None, help="JSON file: a list of {url, name?, dest?}.")
    parser.add_argument("--dest", default=None, help="Landing directory (default: the inbox), e.g. data/inbox/documents/sto")
    parser.add_argument("--name", default=None, help="Override the landed filename (single --url only).")
    parser.add_argument(
        "--stealth",
        choices=("auto", "always", "never"),
        default="auto",
        help="auto: escalate to a stealth browser only when bot-blocked (default). always: start stealthy. never: plain only.",
    )
    parser.add_argument("--render", action="store_true", help="Run JS in a browser (scrapling DynamicFetcher) even without a block.")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between fetches in --from-manifest mode (politeness).")
    args = parser.parse_args(argv)

    if not args.url and not args.from_manifest:
        parser.error("give --url or --from-manifest")

    try:
        if args.url:
            fetch_web(args.url, dest_dir=args.dest, stealth=args.stealth, render=args.render, name=args.name)
        if args.from_manifest:
            items = json.loads(Path(args.from_manifest).read_text("utf-8"))
            for index, item in enumerate(items):
                if index:
                    time.sleep(max(0.0, args.delay))
                fetch_web(
                    item["url"],
                    dest_dir=item.get("dest", args.dest),
                    stealth=args.stealth,
                    render=args.render,
                    name=item.get("name"),
                )
    except FetchError as error:
        print(f"fetch failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
