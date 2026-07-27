"""Common Human-in-the-Loop review server. Stdlib only; no framework, no network.

A reusable review surface: any producer writes a MANIFEST (JSON) of items to be
checked, this serves a browser UI to check them, and on approval writes the
corrected text back to the item's `save_to` path. OCR draft review is the first
consumer (tools/ocr), but nothing here is OCR-specific -- a manifest is the whole
interface, so an alias-table review, a doctype-classification review, or anything
else can reuse it unchanged.

It is an OPERATOR tool, off the build path: the pipeline stays pure Python with no
model and no server. Dependencies are the Python standard library plus two
vendored static files (htmx, a purged Tailwind CSS) -- no pip install, no CDN, no
network, so it runs air-gapped and ships in git.

Manifest shape:
    {
      "task": "<label>",
      "items": [
        {
          "id": "<slug>",
          "title": "<human title>",
          "text": "<the draft text to review>",
          "hotspots": [{"line": <int>, "text": "<span>", "score": <float>,
                        "reason": "<why flagged>"}],
          "images": ["<abs path to a source page image>", ...],
          "save_to": "<abs path to write the approved .txt>"
        }
      ]
    }

Run:
    uv run python tools/hitl/server.py --manifest <manifest.json> [--port 8765]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ASSETS = Path(__file__).with_name("assets")
# The C0 control set pipeline/extract.py rejects; strip it so an approved file
# clears the inbox exactly as a clean extraction would.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_control(text: str) -> str:
    return _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


class Review:
    """Loaded manifest plus reviewed-state persisted beside it."""

    def __init__(self, manifest_path: Path):
        self.path = Path(manifest_path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.task = data.get("task", "review")
        self.items = {item["id"]: item for item in data.get("items", [])}
        self.order = [item["id"] for item in data.get("items", [])]
        self.state_path = self.path.with_suffix(".state.json")
        self.state = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.exists()
            else {}
        )

    def mark_done(self, item_id: str, saved_to: str) -> None:
        self.state[item_id] = {"reviewed": True, "saved_to": saved_to}
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def done(self, item_id: str) -> bool:
        return bool(self.state.get(item_id, {}).get("reviewed"))


# --------------------------------------------------------------------------
# HTML rendering (Tailwind utility classes; the CSS is purged against this file)
# --------------------------------------------------------------------------
def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<link rel='stylesheet' href='/asset/app.css'>"
        "<script src='/asset/htmx.min.js'></script>"
        "</head><body class='min-h-screen bg-slate-50 text-slate-800'>"
        f"{body}</body></html>"
    )


def _index_html(review: Review) -> str:
    rows = []
    for item_id in review.order:
        item = review.items[item_id]
        done = review.done(item_id)
        badge = (
            "<span class='rounded bg-emerald-100 text-emerald-700 px-2 py-0.5 text-xs'>승인됨</span>"
            if done
            else f"<span class='rounded bg-amber-100 text-amber-700 px-2 py-0.5 text-xs'>핫스팟 {len(item.get('hotspots', []))}</span>"
        )
        rows.append(
            f"<a href='/item/{urllib.parse.quote(item_id)}' "
            "class='flex items-center justify-between rounded-lg border border-slate-200 bg-white px-4 py-3 hover:bg-slate-100'>"
            f"<span class='font-medium'>{html.escape(item.get('title', item_id))}</span>{badge}</a>"
        )
    return _page(
        review.task,
        "<div class='mx-auto max-w-3xl p-6'>"
        f"<h1 class='mb-1 text-xl font-semibold'>{html.escape(review.task)}</h1>"
        "<p class='mb-4 text-sm text-slate-500'>핫스팟(낮은 OCR 신뢰도)을 확인·교정하고 승인하세요. 승인 시 inbox에 저장됩니다.</p>"
        f"<div class='space-y-2'>{''.join(rows)}</div></div>",
    )


def _highlight(text: str, hotspots: list) -> str:
    """Render text as HTML with flagged lines wrapped in a yellow mark."""
    flagged = {hotspot.get("line") for hotspot in hotspots}
    out = []
    for number, line in enumerate(text.split("\n"), start=1):
        escaped = html.escape(line) or "&nbsp;"
        if number in flagged:
            out.append(f"<mark data-line='{number}' class='block bg-yellow-200'>{escaped}</mark>")
        else:
            out.append(f"<span class='block'>{escaped}</span>")
    return "".join(out)


def _item_html(review: Review, item_id: str) -> str:
    item = review.items[item_id]
    hotspots = item.get("hotspots", [])
    images = "".join(
        f"<img src='/img/{urllib.parse.quote(item_id)}/{index}' loading='lazy' "
        "class='w-full rounded border border-slate-200 mb-3'>"
        for index in range(len(item.get("images", [])))
    ) or "<p class='text-sm text-slate-400'>(원본 이미지 없음)</p>"

    chips = "".join(
        f"<span class='rounded bg-yellow-100 text-yellow-800 px-2 py-0.5 text-xs'>L{h.get('line')} · {h.get('score'):.2f}</span>"
        for h in hotspots
    ) or "<span class='text-xs text-slate-400'>핫스팟 없음</span>"

    editor = (
        "<div id='editor' contenteditable='true' spellcheck='false' "
        "class='min-h-[60vh] whitespace-pre-wrap rounded border border-slate-300 bg-white p-3 font-mono text-sm leading-6 focus:outline focus:outline-2 focus:outline-emerald-500'>"
        f"{_highlight(item.get('text', ''), hotspots)}</div>"
    )

    body = (
        "<div class='sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white/90 px-6 py-3 backdrop-blur'>"
        f"<div><a href='/' class='text-sm text-slate-500 hover:underline'>&larr; 목록</a>"
        f"<h1 class='text-lg font-semibold'>{html.escape(item.get('title', item_id))}</h1></div>"
        "<button "
        f"hx-post='/save/{urllib.parse.quote(item_id)}' hx-include='#payload' hx-target='#result' "
        "hx-vals='js:{text: document.getElementById(\"editor\").innerText}' "
        "class='rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-700'>승인 → inbox 저장</button>"
        "</div>"
        "<div class='grid grid-cols-2 gap-4 p-6'>"
        f"<div class='max-h-[85vh] overflow-y-auto'>{images}</div>"
        "<div>"
        f"<div class='mb-2 flex flex-wrap gap-1'>{chips}</div>"
        f"{editor}"
        "<textarea id='payload' name='text' class='hidden'></textarea>"
        "<div id='result' class='mt-3 text-sm'></div>"
        f"<p class='mt-2 text-xs text-slate-400'>저장 위치: {html.escape(item.get('save_to', ''))}</p>"
        "</div></div>"
    )
    return _page(item.get("title", item_id), body)


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    review: Review = None  # set on the server instance below

    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, markup: str, status: int = 200) -> None:
        self._send(markup.encode("utf-8"), "text/html; charset=utf-8", status)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/") if p]
        review = self.server.review

        if not parts:
            return self._html(_index_html(review))
        if parts[0] == "item" and len(parts) == 2 and parts[1] in review.items:
            return self._html(_item_html(review, parts[1]))
        if parts[0] == "asset" and len(parts) == 2:
            asset = (ASSETS / parts[1]).resolve()
            if asset.parent == ASSETS.resolve() and asset.exists():
                ctype = "text/css" if asset.suffix == ".css" else "application/javascript"
                return self._send(asset.read_bytes(), ctype)
        if parts[0] == "img" and len(parts) == 3 and parts[1] in review.items:
            images = review.items[parts[1]].get("images", [])
            try:
                index = int(parts[2])
            except ValueError:
                index = -1
            if 0 <= index < len(images) and Path(images[index]).exists():
                return self._send(Path(images[index]).read_bytes(), "image/png")
        self._html("<p class='p-6'>not found</p>", 404)

    def do_POST(self) -> None:
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.strip("/").split("/") if p]
        review = self.server.review
        if not (parts[:1] == ["save"] and len(parts) == 2 and parts[1] in review.items):
            return self._html("<p>bad save</p>", 400)

        length = int(self.headers.get("Content-Length", 0))
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        text = _strip_control((fields.get("text", [""])[0]).strip()) + "\n"

        item = review.items[parts[1]]
        target = Path(item["save_to"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        review.mark_done(parts[1], str(target))
        self._html(
            f"<span class='rounded bg-emerald-100 text-emerald-700 px-2 py-1'>저장됨 → {html.escape(str(target))} "
            f"({len(text)} chars). 색인하려면 `make build`.</span>"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, help="Path to the review manifest JSON.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    review = Review(Path(args.manifest))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.review = review
    print(f"HITL review: http://127.0.0.1:{args.port}/  ({len(review.items)} item(s): {review.task})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
