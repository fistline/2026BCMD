---
name: source-onboarding
description: Bring a NEW external data source into the corpus — research it, choose official-API vs scrape (scrapling), acquire ONE sample, propose how it lands in the pipeline, and STOP for a human decision before building. Use when asked to "add a source" (DART/법령/의안/a website/an API), to "acquire 증권신고서/공시/판례 for firm X", or when deciding "is there an API for Y or do we scrape". Step 0 triages one-off vs a committed feed, so start here even for a single pull. Fetching is always an explicit operator step, never wired into the build.
---

# Onboarding a data source

Every source this project carries — 법령, 의안, DART 증권신고서 — came in through the
same path: prefer an official API, else scrape the public page, land a file in the
inbox, let the normal build index it. This skill is that path written down, with
the two places it must pause for a human. It orchestrates the pieces that already
exist (`pipeline/fetch_law.py` for official APIs, `pipeline/fetch_web.py` for
scraping public pages with scrapling, `agent/tools/scrapling_mcp.py` for
agent-time fetching, `doctype-profile-authoring`); it does not add a framework.

## Step 0 — triage before anything

Most "new sources" need nothing built. If the request is a one-off (a handful of
documents, no recurring feed) and an operator fetcher already covers it, just run
it, land the files in `data/inbox/documents/`, and `make build`. `fetch_law.py`
is already a general fetcher (law / bill / dart). Only continue past Step 0 when
the source will RECUR — an ongoing feed, or roughly five or more documents of a
new shape. A connector is code someone maintains forever.

**Before that first `make build` on a tree whose index came from `make
fetch-index`, run `make warm-cache`.** A fetched tree has no vector cache, so
adding one document re-encodes all 20 344 chunks (1948.2 s) instead of just the
new ones. The cache does not have to be downloaded — it is derived from the index
already installed, in 4.8 s, byte-identical to a real one [M:cache-from-index].
Skipping it does not break anything; it just spends half an hour proving that.
The build that follows is marked `build_kind=incremental`: queryable, but it
cannot record an eval floor or be published, so a release still costs one
`make index-canonical`.

## Procedure

1. **Research: is there an API, and where does the data live?** Find the official
   API (docs, auth, rate limits). Note per PATH whether an API serves it — DART is
   one source with two: `opendart.fss.or.kr` (API) serves the filing body, but the
   `dart.fss.or.kr` viewer 첨부 (감정평가서/계약서 PDF) has no API. robots.txt is
   worth a read for awareness (a Disallow or a killed connection tells you a host
   is bot-hostile, so expect to need `--stealth`), but it is not a gate — public
   pages are fetchable.

2. **Choose the channel — API or scrape.** In order of preference:
   (a) an official API → extend `pipeline/fetch_law.py` (stdlib, `.env` creds);
   (b) no API → scrape the PUBLIC page with `pipeline/fetch_web.py`. It lands the
   file in the inbox and picks the lightest fetch that works, escalating
   static → render (`--render`, runs JS) → stealth (`--stealth`, a real browser
   that gets past indiscriminate bot-blocking). A bot-hostile host (the DART
   viewer, a Cloudflare CDN like musicow) just means `--stealth auto`/`always`.
   The limits are courtesy and scope: rate-limit, take only what you need, and
   fetch only PUBLIC data — not authenticated/private content. If even a stealth
   browser is blocked (rare), save the file from your own browser into the inbox.
   See `references/scrapling-fetch.md` and `references/dart-source.md`.
   (`agent/tools/scrapling_mcp.py` is the agent-time equivalent used during retrieval.)

   A downloaded PDF that is a SCANNED IMAGE (pdftotext yields ~0 chars) is
   converted OFFLINE with `tools/ocr/ocr_prepare.py` (`uv sync --extra ocr`;
   PP-OCRv5 Korean by default, `--vl` for table/stamp-heavy pages). It writes a
   `.md` draft plus an `.ocr.json` of low-confidence hotspots. Review it in the
   common HITL server (`tools/ocr/to_hitl.py <drafts> -o m.json --collection sto`
   then `tools/hitl/server.py --manifest m.json`): hotspots are highlighted, the
   source page sits beside the text, and approve lands the reviewed `.txt` in the
   inbox. The OCR model runs here, never in `make build` — same rule as the
   doctype profiles; the review server is stdlib-only (no model, no network).

3. **Acquire ONE sample first.** Pull a single representative document (for
   `fetch_law dart`, that is `--list-only` then one body) and read the actual
   bytes. Do not write a connector against a shape you have not seen.

4. **Profile the sample if the shape is new.** If it is a document type the
   parser does not know, hand off to `doctype-profile-authoring` (its Step 0 will
   re-confirm one-off vs committed). Reuse an existing profile when the structure
   matches; two profiles for one shape is worse than one imperfect marker.

5. **Propose the pipeline flow.** State how it lands: inbox → immutable raw →
   Meltano EL → DuckLake → SQLMesh bronze/silver/gold → the serving index, and
   whether re-fetch is idempotent (a stable id in the filename).

6. **STOP at the decision gate.** Present a decision packet — source, the
   channel recommendation WITH the robots evidence, the sample, the proposed
   flow, and the open choices — and wait for a human verdict. Acquiring from a new
   source and committing a connector are not reversible in the way a local edit
   is; do not build or bulk-fetch before this.

7. **Build config-over-code.** Add a subcommand or a few flags to
   `fetch_law.py` (its shape: a `fetch_*` function + an argparse subparser,
   stdlib-only, credentials from `.env`), verified against a live response. Do
   not add a generic client or a second framework.

8. **Validate and commit.** `make verify` must pass. Commit logic only; `data/`
   is git-ignored. Land nothing straight into `data/raw` or the serving index.

## Constraints

API first, then scrape — invariant 8. For a source with no API, scrape its PUBLIC
pages with `fetch_web` (scrapling), escalating to a stealth browser when a page
bot-blocks public content. The limits are courtesy and scope, not the presence of
a WAF: rate-limit, take only what you need, and do not fetch authenticated/private
data you are not entitled to. Fetching is always an explicit operator step, never
wired into `make build`.

Secrets live in `.env` (git-ignored), never committed or echoed. The read and
build paths never call the network; a fetcher is an operator tool run explicitly,
never wired into `make build`. Re-fetch must be idempotent: key the landed
filename on the source's own stable id so the inbox → raw promotion de-duplicates.

## Reporting

Give the per-path robots verdict, the channel chosen and why, the sample you
pulled, and the proposed flow — then stop at the gate. This procedure produces a
plan and a decision request for a human, not a silent build.
