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
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="Compare only the first N pages (default 2). Both backends run on every page, "
             "so a whole filing is minutes of work for a spot check that two are enough for.",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as scratch:
        images, labels = collect_pages(args.inputs, args.dpi, Path(scratch))
        if args.pages > 0 and len(images) > args.pages:
            print(f"[compare] {len(images)} page(s) available; comparing the first {args.pages}")
            images = images[: args.pages]
        print(f"[compare] {'; '.join(labels)}: {len(images)} page(s) compared")
        paddle_pages = ocr_ppocr(images)
        onnx_pages = ocr_onnx(images)

    paddle_lines, paddle_scores = _flat(paddle_pages)
    onnx_lines, onnx_scores = _flat(onnx_pages)
    paddle_text = "\n".join(paddle_lines)
    onnx_text = "\n".join(onnx_lines)

    ratio = difflib.SequenceMatcher(None, paddle_text, onnx_text).ratio()
    # Two DIFFERENT questions, and only one of them is about recognition:
    #   raw        -- would a reviewer see the same document? (layout included)
    #   normalised -- did the two backends READ the same characters?
    # Korean OCR output differs constantly in spacing (정의,발행자 vs 정의, 발행자)
    # and in where a line is cut, neither of which is a misread. Reporting only
    # the raw number turns a whitespace difference into an alarm about accuracy.
    squashed = ("".join(paddle_text.split()), "".join(onnx_text.split()))
    text_ratio = difflib.SequenceMatcher(None, *squashed).ratio()
    print(f"\ncharacter similarity      : {ratio:.4f}  (raw, layout included)")
    print(f"  ignoring all whitespace : {text_ratio:.4f}  (did they READ the same characters)")
    # A third question the first two cannot separate: are the same lines present
    # in a different ORDER (a reading-order difference, harmless once indexed),
    # or is a line missing from one side (content actually lost)? Comparing the
    # whitespace-stripped line multisets answers it.
    paddle_set = ["".join(line.split()) for line in paddle_lines if line.strip()]
    onnx_set = ["".join(line.split()) for line in onnx_lines if line.strip()]
    shared = len(set(paddle_set) & set(onnx_set))
    only_paddle = sorted(set(paddle_set) - set(onnx_set))
    only_onnx = sorted(set(onnx_set) - set(paddle_set))
    print(f"lines  paddle / onnx      : {len(paddle_lines)} / {len(onnx_lines)}")
    print(
        f"  identical lines         : {shared} shared, {len(only_paddle)} only in paddle, "
        f"{len(only_onnx)} only in onnx"
    )
    for label, scores in (("paddle", paddle_scores), ("onnx", onnx_scores)):
        if scores:
            below = sum(1 for score in scores if score < 0.92)
            print(
                f"scores {label:<7}          : median {statistics.median(scores):.3f}, "
                f"{below}/{len(scores)} below the 0.92 hotspot threshold"
            )
    show_diff = text_ratio < 0.98 or ratio < 0.98
    if text_ratio < 0.98:
        print(
            f"\nRECOGNITION differs ({text_ratio:.4f} ignoring whitespace): the backends read "
            "different CHARACTERS, not just different spacing. The diff is below -- read it "
            "against the source page before landing either one."
        )
    elif ratio < 0.98:
        print(
            f"\nRecognition agrees ({text_ratio:.4f} ignoring whitespace) but LAYOUT differs "
            f"({ratio:.4f} raw): spacing and line breaks move. Harmless for the indexed text, "
            "but a review UI that cites line numbers will point at different lines."
        )
    if show_diff:
        # Printed for BOTH branches. It used to print only for the layout one --
        # so the recognition case, the one that actually fired, said "check the
        # diff" and then showed none.
        for line in list(difflib.unified_diff(paddle_lines, onnx_lines, "paddle", "onnx", lineterm=""))[:60]:
            print(line)
    if only_paddle or only_onnx:
        print("\nlines present on ONE side only (whitespace ignored):")
        for label, lines in (("paddle", only_paddle), ("onnx", only_onnx)):
            for line in lines[:8]:
                print(f"  {label:6} {line[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
