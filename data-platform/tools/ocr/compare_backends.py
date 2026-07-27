"""Do the paddle and onnxruntime OCR backends read the same page the same way?

Swapping PaddleOCR for RapidOCR keeps the MODELS (ch_PP-OCRv5_det_mobile +
korean_PP-OCRv5_rec_mobile) but changes the pre- and post-processing around them,
and those steps decide where a text line starts and what confidence it carries.
The hotspot threshold the review UI depends on (0.92) was calibrated against the
paddle numbers, so "same models" is not the same as "same output" and must be
checked on a real page before a review workflow moves over.

Reports, per page: character-level similarity of the concatenated text, the count
of lines each backend found, and the score distribution. Operator tool -- it runs
both stacks, so both extras have to be installed.

    make ocr-compare FILE=scan.pdf
"""

from __future__ import annotations

import argparse
import difflib
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ocr_prepare import collect_pages, ocr_onnx, ocr_ppocr  # noqa: E402


def _flat(pages: list) -> tuple:
    lines = [cell["text"] for page in pages for cell in page]
    scores = [cell["score"] for page in pages for cell in page if cell.get("score") is not None]
    return lines, scores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+", help="Scanned PDFs, images, or a directory.")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as scratch:
        images, labels = collect_pages(args.inputs, args.dpi, Path(scratch))
        print(f"[compare] {'; '.join(labels)}: {len(images)} page(s)")
        paddle_pages = ocr_ppocr(images)
        onnx_pages = ocr_onnx(images)

    paddle_lines, paddle_scores = _flat(paddle_pages)
    onnx_lines, onnx_scores = _flat(onnx_pages)
    paddle_text = "\n".join(paddle_lines)
    onnx_text = "\n".join(onnx_lines)

    ratio = difflib.SequenceMatcher(None, paddle_text, onnx_text).ratio()
    print(f"\ncharacter similarity      : {ratio:.4f}")
    print(f"lines  paddle / onnx      : {len(paddle_lines)} / {len(onnx_lines)}")
    for label, scores in (("paddle", paddle_scores), ("onnx", onnx_scores)):
        if scores:
            below = sum(1 for score in scores if score < 0.92)
            print(
                f"scores {label:<7}          : median {statistics.median(scores):.3f}, "
                f"{below}/{len(scores)} below the 0.92 hotspot threshold"
            )
    if ratio < 0.98:
        print(
            "\nThe two backends disagree on more than 2% of characters. Read the diff below "
            "against the source page before switching a review workflow over."
        )
        for line in list(difflib.unified_diff(paddle_lines, onnx_lines, "paddle", "onnx", lineterm=""))[:60]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
