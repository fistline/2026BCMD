# Web fetching with fetch_web: API first, then scrape (scrapling, stealth-capable)

Load this when a source has NO official API and you must pull a file over the web.
The tool is `pipeline/fetch_web.py`. It is an OPERATOR tool (never on the build or
read path): it lands a file in `data/inbox/documents/` and the normal pipeline
promotes, transforms and indexes it, so a fetched file carries the same provenance
as everything else.

Install (needed for `--render`/stealth; the static path is stdlib-only):

```
uv sync --extra web && uv run scrapling install
```

## Policy (AGENTS.md invariant 8)

**API first, then scrape.** If a source offers an official API, use it
(`fetch_law.py`). For sources with no API, scrape their PUBLIC pages with
scrapling. The only real limits: be polite (rate-limit, take only what you need),
and this is for PUBLIC, human-viewable content — not authenticated/private data
you are not entitled to. robots.txt is reported for awareness, not enforced as a
wall; an indiscriminate bot-block on public pages is something to get past, not a
"no".

## Escalation (start light, go heavier only if blocked)

`fetch_web` picks the lightest fetch that works and escalates automatically:

```
static (Fetcher / stdlib)  →  render (DynamicFetcher, runs JS)  →  stealth
(StealthyFetcher, a real browser that gets past bot-blocking)
```

- `--stealth auto` (default): try static, and if the response is an active
  bot-block (Cloudflare/DataDome/Incapsula challenge), escalate to a stealth
  browser and retry. Most files never spin up a browser.
- `--stealth always`: start stealthy (skip the escalation dance).
- `--stealth never`: plain fetch only; report a block instead of escalating.
- `--render`: run JS in a browser even without a block (for JS-rendered pages).

A bare `403`/permission error is NOT treated as a bot-block, so we do not spin up
a browser for a page that simply is not there or is not ours — you see the real
HTTP error.

## Source matrix (the ones we know)

| Host | Has API? | Channel |
| --- | --- | --- |
| `opendart.fss.or.kr` | yes | `fetch_law dart` (API) |
| `law.go.kr` / `open.assembly.go.kr` | yes | `fetch_law law` / `bill` (API) |
| `dart.fss.or.kr` viewer (첨부 PDF) | no (attachments) | `fetch_web` — bot-hostile, so `--stealth auto` escalates |
| `cdn.musicow.com` (self-published 증권신고서) | no | `fetch_web --stealth always` (Cloudflare) |
| `sou.place` / cloudfront | public JSON API | already fetched via the API |
| a new issuer's IR / disclosures page | usually no | `fetch_web` (`--render` if JS-heavy) |

## Usage

```
# ordinary public file
uv run python -m pipeline.fetch_web --url https://issuer.example/ir/report.pdf \
    --dest data/inbox/documents/sto

# JS-rendered page
uv run python -m pipeline.fetch_web --url https://issuer.example/disclosures --render

# a bot-blocked public file (Cloudflare etc.) — start stealthy
uv run python -m pipeline.fetch_web --url "<viewer-or-cdn-pdf-url>" --stealth always \
    --dest data/inbox/documents/sto --name 증권신고서_뮤직카우.pdf

# a batch (polite delay between items)
uv run python -m pipeline.fetch_web --from-manifest links.json --delay 3
#   links.json = [{"url": "<url>", "name": "<filename>", "dest": "data/inbox/documents/sto"}]
```

## After landing

A downloaded PDF that is a SCANNED IMAGE (pdftotext yields ~0 chars) is converted
OFFLINE with `tools/ocr/ocr_prepare.py`, reviewed in the HITL server, and only the
approved `.txt` enters the inbox. Then `make build`. If even a stealth browser
cannot fetch a file (rare), open it in your own browser and drop the file in the
inbox directly — same destination, done by hand.
