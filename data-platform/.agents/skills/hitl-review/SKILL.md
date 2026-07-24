---
name: hitl-review
description: Review flagged items in a browser and approve the corrected text into the inbox — a reusable human-in-the-loop surface. Use to check OCR drafts before landing (hotspots highlighted, source page beside the text), and use when wiring ANY new producer that needs human sign-off (an alias-table proposal, a doctype classification, a low-confidence extraction): the producer writes a manifest, the same server reviews it. Covers both running a review and implementing a new one.
allowed-tools: Bash(uv run python tools/hitl/server.py *), Bash(uv run python tools/ocr/to_hitl.py *)
---

# Human-in-the-loop review

High-stakes text (증권신고서 금액, 조문번호, 당사자명) must not land unreviewed, and
a reviewer should not re-read 70,000 characters to find the twenty that are wrong.
`tools/hitl/` is one browser review surface that any producer feeds through a
manifest: it highlights the flagged spans, shows the source beside the text, and
on approval writes the corrected `.txt` into the inbox. It is an operator tool,
off the build path — the pipeline still calls no model and no server, and the
review server is stdlib-only (no pip install, no CDN, no network).

## Use it — review and approve

For OCR drafts, the adapter builds the manifest for you, then run the server:

```
uv run python tools/ocr/to_hitl.py <drafts_dir> -o <manifest.json> --collection sto
uv run python tools/hitl/server.py --manifest <manifest.json>   # http://127.0.0.1:8765
```

In the browser: each item shows the source page image beside the OCR text, with
low-confidence lines (hotspots) in yellow. Fix them in place, then **승인 → inbox
저장**. Approve writes the corrected text to the item's `save_to` (control chars
stripped so it clears the inbox), records it as reviewed beside the manifest, and
then `make build` indexes it. `--collection sto` lands it in the `sto` collection
folder, so a later `make query Q="<q>" COLLECTION=sto` scopes to it.

Hotspots are the OCR engine's own per-line confidence (`rec_scores` below 0.92),
NOT an embedding similarity to some answer key: embeddings are typo-robust so they
miss the character errors that matter, and a novel filing has no answer key.

## Wire a new producer — the manifest is the whole contract

Anything that needs human sign-off reuses the same server by writing a manifest.
Nothing HITL-specific belongs in the producer beyond emitting this JSON:

```
{
  "task": "<label>",
  "items": [
    {
      "id": "<slug>",                                  // unique, url-safe
      "title": "<human title>",
      "text": "<the draft text to review>",
      "hotspots": [{"line": <1-based into text>, "text": "<span>",
                    "score": <float>, "reason": "<why>"}],
      "images": ["<abs path to a source page png>"],   // optional side-by-side
      "save_to": "<abs path to write the approved .txt>"
    }
  ]
}
```

Then `uv run python tools/hitl/server.py --manifest <manifest.json>`. The server
reads it, serves the review UI, and on approval writes `save_to`. To land into a
collection, point `save_to` at `data/inbox/documents/<collection>/<name>.txt`.
`tools/ocr/to_hitl.py` is the worked example — copy its shape for a new producer.

## Constraints

Stdlib only, and it stays that way. The two browser assets are VENDORED, never a
CDN: `tools/hitl/assets/htmx.min.js` and `app.css` (Tailwind purged against
`server.py`). A CDN would put the network on an offline tool and break
git-shareability. Regenerate the CSS after changing the markup with the Tailwind
standalone CLI: `tailwindcss -i tools/hitl/input.css -o tools/hitl/assets/app.css --minify`.

The reviewer is the gate: never auto-approve, and never wire the server into
`make build`. `save_to` is written verbatim after the same control-char strip the
inbox demands, so the approved bytes are the source of truth from then on.

## Reporting

State how many items and hotspots were reviewed, where each was landed
(collection and path), and anything the reviewer flagged as still uncertain. The
approved `.txt` in the inbox — not the PDF or the OCR draft — is what the build
indexes.
