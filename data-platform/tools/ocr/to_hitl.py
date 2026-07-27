"""Adapter: turn OCR drafts into a HITL review manifest.

The seam between the OCR producer (tools/ocr/ocr_prepare.py) and the common
review server (tools/hitl/server.py). It reads the .md drafts and their
.ocr.json sidecars (hotspots + page-image dir) from a drafts directory and writes
one manifest the generic server can serve -- nothing OCR-specific leaks into the
server. Each item's approved text lands in data/inbox/documents/<collection>/.

Stdlib only; no OCR/model dependency (it just reads files the OCR step wrote).

Usage:
    uv run python tools/ocr/to_hitl.py <drafts_dir> -o <manifest.json> [--collection sto]
    uv run python tools/hitl/server.py --manifest <manifest.json>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from pipeline import get_paths

_HEADER_RE = re.compile(r"\A<!--.*?-->\n\n", re.DOTALL)


def build_manifest(
    drafts_dir: Path,
    inbox: Path,
    collection: str | None,
    task: str,
    hotspot_threshold: float = 0.80,
) -> dict:
    """Build a review manifest from OCR drafts.

    The OCR sidecar captures hotspots generously (every line below its own 0.92
    gate). `hotspot_threshold` re-filters those to a stricter, actionable set at
    display time -- on a clean scan only the genuinely-bad lines survive, while a
    poor scan stays mostly-flagged, which honestly says "review this one closely".
    """
    drafts_dir = Path(drafts_dir)
    items = []
    for md in sorted(drafts_dir.glob("*.md")):
        meta = {}
        sidecar = md.with_suffix(".ocr.json")
        if sidecar.exists():
            meta = json.loads(sidecar.read_text(encoding="utf-8"))

        # The manifest text is the draft body with the OCR header comment removed,
        # so hotspot line numbers (recorded against the body) line up exactly.
        body = _HEADER_RE.sub("", md.read_text(encoding="utf-8"))
        hotspots = [h for h in meta.get("hotspots", []) if h.get("score", 1.0) < hotspot_threshold]

        images_dir = Path(meta["images_dir"]) if meta.get("images_dir") else (drafts_dir / (".ocr_pages_" + md.stem))
        images = [str(p) for p in sorted(images_dir.glob("page_*.png"))] if images_dir.exists() else []

        target_dir = inbox / collection if collection else inbox
        items.append(
            {
                "id": md.stem,
                "title": md.stem,
                "text": body,
                "hotspots": hotspots,
                "images": images,
                "save_to": str(target_dir / (md.stem + ".txt")),
            }
        )
    return {"task": task, "items": items}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("drafts_dir", help="Directory of OCR .md drafts (+ .ocr.json sidecars).")
    parser.add_argument("-o", "--out", required=True, help="Manifest JSON to write.")
    parser.add_argument("--collection", default=None, help="Land approved text into this inbox collection folder.")
    parser.add_argument("--task", default="OCR draft review", help="Manifest task label.")
    parser.add_argument("--hotspot-threshold", type=float, default=0.80, help="Flag lines below this OCR confidence (default 0.80).")
    args = parser.parse_args(argv)

    inbox = get_paths().inbox
    manifest = build_manifest(Path(args.drafts_dir), inbox, args.collection, args.task, args.hotspot_threshold)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    total_hotspots = sum(len(item["hotspots"]) for item in manifest["items"])
    print(
        f"wrote {out} ({len(manifest['items'])} item(s), {total_hotspots} hotspot(s)). "
        f"Review: uv run python tools/hitl/server.py --manifest {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
