"""Does the chunk ceiling still fit the corpus it is being applied to?

`MAX_CHUNK_CHARS` is a DERIVED value, not a preference: it is the largest ceiling
at which no chunk exceeds the embedder's token cap, given how densely this text
tokenises. Korean legal prose runs about 1.9 characters per token, so 650 chars
lands near 350 tokens under a 512 cap. English prose and source code run about
3.2, where the same 650 chars is barely 200 tokens.

A comment saying that would be the wrong instrument. This repo already has the
evidence: `_MAX_TOKENS = 512  # a 1200-char chunk fits well under this` stopped
anyone re-measuring for months, and it was wrong -- 28.08 % of the corpus was
being truncated [M:token-density]. A confident sentence about a derived value
entrenches it. So the derivation is asserted here instead, where it can FAIL when
the data stops matching it, and stays silent while it holds.

Two assertions, and only the first has an exact answer:

  1. NOTHING TRUNCATES. Every chunk's `embed_text` must fit under the token cap.
     No threshold, no judgement -- either a chunk is over the cap or it is not.
     This is the one that was silently false.

  2. THE CEILING IS NOT WASTED. For each material slice of the corpus, the ceiling
     translated into tokens (`MAX_CHUNK_CHARS / density`) should be a reasonable
     share of the cap. A slice whose text is much sparser gets chunks far smaller
     than the model could hold, which is not a correctness bug but is a derivation
     that no longer describes that data.

A slice below MATERIAL_SHARE is reported, never failed: eight chunks of Python in
a Korean legal corpus do not get to block a build. The share is the point at which
"the constant is wrong for this data" starts to cost something.

    uv run python tools/check_chunk_fit.py
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Below this share of the index, a divergent slice is reported and not failed.
MATERIAL_SHARE = 0.05
# A material slice whose ceiling translates to less than this share of the token
# cap is being chunked for text it is not made of.
MIN_CEILING_USE = 0.60


def main() -> int:
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    from pipeline import get_paths, get_settings
    from pipeline.build_rag import OnnxEmbedder
    from pipeline.chunking import MAX_CHUNK_CHARS

    settings = get_settings()
    if settings.embedding_provider != "onnx_int8":
        print(f"SKIP: provider is {settings.embedding_provider!r}; no token cap to fit")
        return 0

    index = get_paths().index_sqlite
    if not index.exists():
        print(f"SKIP: no index at {index}")
        return 0

    cap = OnnxEmbedder._MAX_TOKENS
    tokenizer = Tokenizer.from_file(
        hf_hub_download(settings.embedding_model, "tokenizer.json", local_files_only=True)
    )

    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT collection, doc_type, title, rel_path, heading, content FROM chunks"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        print("SKIP: the index holds no chunks")
        return 0

    texts = [
        "# " + (title or rel or "")
        + "\n" + ("## " + (heading or "") if (heading or "") != (title or "") else "")
        + "\n" + (content or "")
        for _, _, title, rel, heading, content in rows
    ]
    encoded = tokenizer.encode_batch(texts)

    slices: dict = defaultdict(list)
    over_cap = 0
    for (collection, doc_type, *_, content), tokens in zip(rows, encoded, strict=True):
        length = len(tokens.ids)
        if length > cap:
            over_cap += 1
        if content and length > 5:
            slices[(collection, doc_type)].append((len(content) / length, length))

    total = len(rows)
    failures: list = []

    if over_cap:
        failures.append(
            f"{over_cap} of {total} chunks ({100 * over_cap / total:.2f} %) exceed the "
            f"{cap}-token cap, so their tails are encoded into no vector at all"
        )

    print(f"{total} chunk(s), cap {cap} tokens, MAX_CHUNK_CHARS {MAX_CHUNK_CHARS}")
    print(f"{'slice':34s} {'share':>7} {'chars/tok':>10} {'ceiling':>9} {'use':>6}")
    for key in sorted(slices, key=lambda key: -len(slices[key])):
        densities = sorted(value[0] for value in slices[key])
        count = len(densities)
        share = count / total
        density = densities[count // 2]
        ceiling_tokens = MAX_CHUNK_CHARS / density
        use = ceiling_tokens / cap
        flag = ""
        if use < MIN_CEILING_USE:
            flag = "  <- under-chunked" if share >= MATERIAL_SHARE else "  (below share)"
        label = f"{key[0][:18]}/{key[1]}"
        print(
            f"{label:34s} {100 * share:>6.1f}% {density:>10.3f} "
            f"{ceiling_tokens:>8.0f}t {100 * use:>5.0f}%{flag}"
        )
        if use < MIN_CEILING_USE and share >= MATERIAL_SHARE:
            failures.append(
                f"{label} is {100 * share:.1f} % of the index at {density:.2f} chars/token, so "
                f"MAX_CHUNK_CHARS={MAX_CHUNK_CHARS} gives it ~{ceiling_tokens:.0f} tokens against a "
                f"{cap} cap ({100 * use:.0f} %) -- the ceiling was derived for denser text than this"
            )

    if failures:
        print(f"\nFAIL: {len(failures)} chunk-fit problem(s):")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nMAX_CHUNK_CHARS is derived, not chosen. Re-derive it for the data actually "
            "present:\n`make chunk-ceiling` computes the largest ceiling that truncates "
            "nothing, but only DOWNWARD from the\ncurrent one -- a sparser corpus needs the "
            "ceiling raised, which has to be measured from the source text."
        )
        return 1
    print("\nOK: nothing truncates, and every material slice uses the ceiling it was derived for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
