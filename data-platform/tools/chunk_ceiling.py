"""What is the largest MAX_CHUNK_CHARS that never truncates? Computed, not swept.

Chunk size is the most expensive axis this repo has: every value costs a full
rebuild, so a five-point sweep is hours. But the question "does a chunk of C
characters fit under the embedder's token cap" does not need a rebuild at all --
it needs the token-length distribution of the text that is already indexed.

This is the tool for that. It reads real chunk text, tokenises `embed_text` under
the configured model, and reports the truncation share at each candidate ceiling
so the ceiling can be chosen before any index is built.

WHY A TOOL AND NOT ARITHMETIC. The obvious calculation is `cap x mean chars per
token`, and it is wrong: done by hand here it produced 950, because the mean was
2.054 chars/token while the DENSEST chunks run at 1.310. Truncation is decided by
the tail, not the mean, and a tail is not something to estimate. The measured
answer was 650 [M:token-density].

IT ONLY LOOKS DOWNWARD, and refuses to pretend otherwise. The text it samples is
the text already in the index, so a chunk is at most the CURRENT MAX_CHUNK_CHARS
long. Asked about a larger ceiling it would sample too few characters and report a
clean sheet -- the first version of this tool did exactly that, calling 1200 clean
against an index built at 650 while 1200 was measured at 28.08 % truncation
[M:token-density]. That answer would have argued for reverting the very change
that fixed it. So candidates above the current ceiling are rejected, not estimated;
to explore upward you have to build.

WHAT IT DOES NOT ANSWER. Whether the ceiling it finds RETRIEVES better. Smaller
chunks fragment more, and fragmentation interacts with SECTION_CAP in a way that
reverses sign at 1200 [M:cap-chunk-coupling]. This tool removes one rebuild from
the search; it does not remove the eval.

    uv run python tools/chunk_ceiling.py
    uv run python tools/chunk_ceiling.py --cap 512 --candidates 1200,900,800,700,650
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _embed_text(title, rel_path, heading, content) -> str:
    """The string the embedder actually sees. Mirrors transform/models/gold/chunks.sql."""
    lines = ["# " + (title or rel_path or "")]
    lines.append("## " + (heading or "") if (heading or "") != (title or "") else "")
    lines.append(content or "")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cap", type=int, default=None, help="Token cap (default: the embedder's).")
    parser.add_argument(
        "--candidates",
        default="1200,1000,900,800,750,700,650,600",
        help="Comma-separated MAX_CHUNK_CHARS values to score.",
    )
    arguments = parser.parse_args()

    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    from pipeline import get_paths, get_settings
    from pipeline.build_rag import OnnxEmbedder

    settings = get_settings()
    cap = arguments.cap or OnnxEmbedder._MAX_TOKENS
    index = get_paths().index_sqlite
    if not index.exists():
        print(f"FAIL: no index at {index}. This reads the CURRENT chunk text; run `make build`.")
        return 1

    tokenizer = Tokenizer.from_file(
        hf_hub_download(settings.embedding_model, "tokenizer.json", local_files_only=True)
    )

    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT title, rel_path, heading, content FROM chunks"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        print("FAIL: the index holds no chunks")
        return 1

    from pipeline.chunking import MAX_CHUNK_CHARS

    prefixes = [_embed_text(title, rel, heading, "") for title, rel, heading, _ in rows]
    total = len(rows)
    longest = max(len(content or "") for *_, content in rows)

    candidates = sorted({int(value) for value in arguments.candidates.split(",")}, reverse=True)
    too_high = [value for value in candidates if value > MAX_CHUNK_CHARS]
    candidates = [value for value in candidates if value <= MAX_CHUNK_CHARS]
    if not candidates:
        print(
            f"FAIL: every candidate exceeds the current MAX_CHUNK_CHARS ({MAX_CHUNK_CHARS}). "
            f"The index holds chunks of at most {longest} characters, so a larger ceiling cannot "
            f"be sampled from it -- build it and measure."
        )
        return 1

    print(f"{total} chunk(s), token cap {cap}, model {settings.embedding_model}")
    print(f"current MAX_CHUNK_CHARS {MAX_CHUNK_CHARS}, longest chunk {longest} chars")
    if too_high:
        print(f"skipped (above the current ceiling, unsampleable): {', '.join(map(str, too_high))}")
    print(f"{'MAX_CHUNK_CHARS':>16} {'max tokens':>11} {'over cap':>10}")

    clean = None
    for candidate in candidates:
        texts = [
            prefix + (content or "")[:candidate]
            for prefix, (*_, content) in zip(prefixes, rows, strict=True)
        ]
        lengths = [len(encoded.ids) for encoded in tokenizer.encode_batch(texts)]
        over = sum(1 for length in lengths if length > cap)
        marker = ""
        if over == 0 and clean is None:
            clean, marker = candidate, "  <- largest clean ceiling"
        print(f"{candidate:>16} {max(lengths):>11} {100 * over / total:>9.2f}%{marker}")

    print()
    if clean is None:
        print(
            "No candidate is clean. Either extend --candidates downward, or accept a share and "
            "say so where the constant lives."
        )
        return 1
    print(
        f"MAX_CHUNK_CHARS = {clean} truncates nothing on this corpus. It is a CEILING, not a "
        f"recommendation:\nsmaller chunks fragment more, and fragmentation flips the sign of "
        f"SECTION_CAP [M:cap-chunk-coupling].\nBuild it, then `make eval` and read per kind."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
