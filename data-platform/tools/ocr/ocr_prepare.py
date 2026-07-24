"""Operator tool: OCR a scanned PDF to Korean text for the inbox.

NOT part of the build and NOT importable by it -- it lives under tools/, outside
the `pipeline` package, so `make build` physically cannot call it. The build runs
no model (§ invariant 2). This converts a scanned PDF to text OFFLINE, the
operator REVIEWS it, and lands the reviewed .txt in data/inbox/documents/ -- the
same seam as pipeline/fetch_law.py, and the same discipline as the doctype
profiles: a model does its work once, offline; the build never calls a model.

Two engines, both Apache-2.0, both fully local:

  default   PaddleOCR PP-OCRv5 Korean recognizer. NON-generative (detect + argmax),
            so it is deterministic and it never fabricates text -- it misreads a
            character, it does not invent a 조문 number, 의안번호 or 금액. This is
            the right default for high-stakes legal text.
  --vl      PaddleOCR-VL (0.9B). A generative VLM with stronger table / stamp /
            layout handling, for dense 감정평가서 / 신탁계약서 pages where flat
            line-OCR loses table structure. Greedy-decoded, so it is reproducible
            run-to-run on one machine but not bit-identical across machines --
            which is exactly why OCR stays OUTSIDE the deterministic build.

A VLM can hallucinate plausible-but-wrong legal text, so the output is a DRAFT.
REVIEW it against the source image (조문번호, 의안번호, 금액, 당사자명) before you
land it. Model weights download to the local Paddle/HF cache on first run and are
never committed (git carries logic, not data).

Install (operators only; the core build never needs this):
    uv sync --extra ocr

Usage:
    uv run python tools/ocr/ocr_prepare.py <scan.pdf> -o out.md
    uv run python tools/ocr/ocr_prepare.py <scan.pdf> --vl -o out.md
    # then review out.md, and save the corrected text as
    #   data/inbox/documents/<name>.txt   (control chars already stripped)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# A line the recogniser is less sure about than this is flagged as a hotspot for
# the review UI (matches the "confidence > 0.92" gate in the proposed design).
HOTSPOT_THRESHOLD = 0.92

# The exact control set pipeline/extract.py rejects. Stripping it here means the
# landed .txt passes the inbox the same way a clean extraction would; a raw OCR
# or pdftotext dump would trip this and be rejected.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

INSTALL_HINT = "uv sync --extra ocr"


def _require():
    """Import the optional OCR stack, or fail with an actionable message."""
    try:
        import fitz  # noqa: F401  (pymupdf)
        import paddleocr  # noqa: F401
    except ImportError as error:
        raise SystemExit(
            f"the OCR stack is not installed. Operators enable it with:\n    {INSTALL_HINT}\n"
            f"(missing: {error.name}). The core build never needs this."
        )


def strip_control(text: str) -> str:
    """Remove the C0 control chars the inbox rejects; keep \\t and newlines."""
    return _CONTROL_RE.sub("", text)


def pdf_has_text(pdf_path: Path, threshold: int = 100) -> bool:
    """True if the PDF already yields real text (so OCR is unnecessary)."""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        chars = sum(len(page.get_text("text")) for page in doc)
    finally:
        doc.close()
    return chars >= threshold


def rasterize(pdf_path: Path, dpi: int, out_dir: Path) -> list:
    """Render every page to a PNG and return the paths, in order."""
    import fitz

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    images = []
    try:
        for index in range(doc.page_count):
            pixmap = doc[index].get_pixmap(dpi=dpi)
            path = out_dir / f"page_{index + 1:04d}.png"
            pixmap.save(path)
            images.append(path)
    finally:
        doc.close()
    return images


def ocr_ppocr(images: list) -> list:
    """PP-OCRv5 Korean, deterministic detect+recognize.

    Returns one list of {"text", "score"} lines per page, so a per-line
    confidence survives to the review UI as a hotspot signal.
    """
    from paddleocr import PaddleOCR

    # Mobile detection, not the default server detector: the server model is ~an
    # order of magnitude slower on CPU (measured ~46 min for a 27-page scan) for
    # no meaningful gain on these dense document scans. BOTH models must be pinned
    # -- setting only the detector makes PaddleOCR silently drop the Korean
    # recogniser for a generic (PP-OCRv6) one, which reads Korean pages as CJK
    # noise (0% Hangul). Escalate to --vl when table structure matters.
    engine = PaddleOCR(
        lang="korean",
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )
    pages = []
    for image in images:
        lines = []
        for result in engine.predict(str(image)):
            payload = result if isinstance(result, dict) else (getattr(result, "json", {}) or {})
            record = payload.get("res", payload)
            texts = payload.get("rec_texts") or record.get("rec_texts") or []
            scores = payload.get("rec_scores") or record.get("rec_scores") or []
            for index, text in enumerate(texts):
                score = float(scores[index]) if index < len(scores) else None
                lines.append({"text": text, "score": score})
        pages.append(lines)
    return pages


def ocr_vl(images: list) -> list:
    """PaddleOCR-VL (0.9B), greedy. Stronger tables/stamps; Markdown per page.

    No per-line confidence, so every line's score is None (no hotspots).
    """
    from paddleocr import PaddleOCRVL

    engine = PaddleOCRVL()
    pages = []
    for image in images:
        lines = []
        for result in engine.predict(str(image)):
            payload = result if isinstance(result, dict) else (getattr(result, "json", {}) or {})
            record = payload.get("res", payload)
            markdown = record.get("markdown") or record.get("text") or ""
            lines.extend({"text": line, "score": None} for line in markdown.split("\n"))
        pages.append(lines)
    return pages


def _render(pages: list, hotspot_threshold: float = HOTSPOT_THRESHOLD):
    """Turn structured pages into (markdown_text, hotspots).

    Hotspot line numbers are 1-based into the returned text, so the review UI can
    highlight exactly the low-confidence lines.
    """
    out_lines: list = []
    hotspots: list = []
    for page_number, page in enumerate(pages, start=1):
        out_lines.append(f"## page {page_number}")
        out_lines.append("")
        for cell in page:
            out_lines.append(cell["text"])
            score = cell.get("score")
            if score is not None and score < hotspot_threshold:
                hotspots.append(
                    {"line": len(out_lines), "text": cell["text"], "score": round(score, 4),
                     "reason": "low OCR confidence"}
                )
        out_lines.append("")
    return "\n".join(out_lines), hotspots


def ocr_pdf(pdf_path: Path, out_path: Path, use_vl: bool = False, dpi: int = 200, scratch: Optional[Path] = None) -> Path:
    """Rasterise, OCR, strip control chars, write the draft. Returns out_path."""
    _require()
    pdf_path = Path(pdf_path)
    out_path = Path(out_path)
    if not pdf_path.exists():
        raise SystemExit(f"no such file: {pdf_path}")
    if pdf_has_text(pdf_path):
        print(
            f"note: {pdf_path.name} already has extractable text -- OCR may be "
            "unnecessary; pdftotext -nopgbrk may be enough.",
            file=sys.stderr,
        )

    scratch = Path(scratch) if scratch else out_path.parent / (".ocr_pages_" + pdf_path.stem)
    images = rasterize(pdf_path, dpi, scratch)
    engine = "PaddleOCR-VL" if use_vl else "PP-OCRv5(korean)"
    print(f"[ocr] {pdf_path.name}: {len(images)} page(s) at {dpi} DPI via {engine}", file=sys.stderr)

    pages = ocr_vl(images) if use_vl else ocr_ppocr(images)
    text, hotspots = _render(pages)
    text = strip_control(text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"<!-- OCR draft: {pdf_path.name} via {engine}. REVIEW against the source before landing. -->\n\n"
    out_path.write_text(header + text + "\n", encoding="utf-8")

    # Sidecar for the HITL review server: the hotspots (low-confidence lines) and
    # where the source page images are, so review can highlight and show them.
    sidecar = out_path.with_suffix(".ocr.json")
    sidecar.write_text(
        json.dumps(
            {"source": pdf_path.name, "engine": engine, "header_lines": 2,
             "hotspots": hotspots, "images_dir": str(scratch), "pages": len(images)},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"[ocr] wrote {out_path} ({len(text)} chars, {len(hotspots)} hotspot(s)) + {sidecar.name}. "
        "REVIEW it (조문/의안번호/금액/당사자명) against the source, then approve into the inbox "
        "(tools/hitl/server.py) or save the corrected .txt yourself, and run `make build`.",
        file=sys.stderr,
    )
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", help="Scanned PDF to OCR.")
    parser.add_argument("-o", "--out", required=True, help="Output draft path (.md).")
    parser.add_argument("--vl", action="store_true", help="Use PaddleOCR-VL (tables/stamps) instead of PP-OCRv5.")
    parser.add_argument("--dpi", type=int, default=200, help="Rasterisation DPI (default 200).")
    args = parser.parse_args(argv)
    ocr_pdf(Path(args.pdf), Path(args.out), use_vl=args.vl, dpi=args.dpi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
