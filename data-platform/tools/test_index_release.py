"""The release pointer is the only checksum anyone trusts. These are its refusals.

`index_release.json` is what makes a mutable release asset safe to install: it is
tracked, so changing the bytes it names requires a commit. That guarantee is worth
exactly as much as the validation behind it, and the validation runs in `make
check` on every commit -- where a silent weakening would go unnoticed for months.

So each case below is a way the pointer could lie, and asserts that it is caught:
a hand-edited tag, a field quietly dropped, an incrementally-built index dressed
as canonical, a truncated digest, a signature swapped under a tag that no longer
derives from it.

Pure stdlib, no network, no venv, no index -- it runs beside the other cheap
invariants at the top of `make verify`.

    python3 tools/test_index_release.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from index_release import problems_with, tag_for

SIGNATURE = (
    "v4|bill:2/3/8/2,statute:1/5/4/1|onnx_int8|Xenova/bge-m3|1024|cjk2-3|fts2|"
    "unicode61 remove_diacritics 2 tokenchars '_'|kiwi-add-0.23.0"
)
CORPUS = "c:9cb3690c0c9e"


CHUNKS = 20_344
VECTORS = "v1|onnx_int8|Xenova/bge-m3|1024|int8"


def valid() -> dict:
    return {
        "tag": tag_for(CORPUS, SIGNATURE, CHUNKS, VECTORS),
        "asset": "index.sqlite.xz",
        "repo": "owner/name",
        "sha256_xz": "a" * 64,
        "sha256_sqlite": "b" * 64,
        "bytes_xz": 96_254_760,
        "bytes_sqlite": 195_440_640,
        "corpus_id": CORPUS,
        "index_signature": SIGNATURE,
        "build_kind": "canonical",
        "chunk_count": CHUNKS,
        "vector_signature": VECTORS,
    }


CASES = (
    ("a valid pointer", lambda p: p, False),
    ("a hand-edited tag", lambda p: {**p, "tag": "index-deadbeef1234-aaaaaaaa"}, True),
    ("a dropped field", lambda p: {k: v for k, v in p.items() if k != "sha256_sqlite"}, True),
    ("an incremental build", lambda p: {**p, "build_kind": "incremental"}, True),
    ("a truncated digest", lambda p: {**p, "sha256_xz": "abc123"}, True),
    # The tag is derived from the signature, so swapping one without the other is
    # what a doctored pointer looks like: the bytes it names were built by
    # something else.
    ("a swapped signature", lambda p: {**p, "index_signature": SIGNATURE.replace("1024", "768")}, True),
    # The chunking axis is invisible to index_signature: MAX_CHUNK_CHARS 1200 -> 650
    # reshaped this index without moving one character of it [M:chunk-650]. It is in
    # the tag through chunk_count, and this is the case that proves it.
    ("a rechunked index under the old tag", lambda p: {**p, "chunk_count": 13_047}, True),
    # index_signature omits the execution provider on purpose, so a CPU-built and a
    # GPU-built index agree on it AND on chunk_count while holding different vectors.
    ("a different execution provider", lambda p: {**p, "vector_signature": VECTORS + "|coreml"}, True),
    ("a quoted byte count", lambda p: {**p, "bytes_xz": "96254760"}, True),
    ("an unexpected asset name", lambda p: {**p, "asset": "index.sqlite.zst"}, True),
)


def main() -> int:
    """Quiet on success -- it sits in `make check`, where one green line per gate is
    the whole point. A failure prints every case, because then the detail matters."""
    failures = 0
    for name, mutate, should_fail in CASES:
        problems = problems_with(mutate(valid()))
        caught = bool(problems)
        if caught != should_fail:
            failures += 1
            verdict = "NOT CAUGHT" if should_fail else f"wrongly rejected: {problems}"
            print(f"FAIL  {name}: {verdict}")
        elif "-v" in sys.argv:
            print(f"ok    {name} -> {problems[0] if problems else 'accepted'}")

    # The tag must be a function of its four inputs and nothing else: a fetch pins
    # a tag, so a drifting derivation would silently orphan a published artifact.
    if tag_for(CORPUS, SIGNATURE, CHUNKS, VECTORS) != tag_for(CORPUS, SIGNATURE, CHUNKS, VECTORS):
        failures += 1
        print("FAIL  tag_for is not deterministic")
    for label, other in (
        ("the signature", tag_for(CORPUS, SIGNATURE + " ", CHUNKS, VECTORS)),
        ("the chunk count", tag_for(CORPUS, SIGNATURE, CHUNKS + 1, VECTORS)),
        ("the corpus", tag_for("c:000000000000", SIGNATURE, CHUNKS, VECTORS)),
        ("the vector space", tag_for(CORPUS, SIGNATURE, CHUNKS, VECTORS + "|coreml")),
    ):
        if tag_for(CORPUS, SIGNATURE, CHUNKS, VECTORS) == other:
            failures += 1
            print(f"FAIL  tag_for ignores a change in {label}")

    if failures:
        print(f"\nFAIL: {failures} case(s)")
        return 1
    print(f"\nOK: {len(CASES)} pointer case(s), tag derivation stable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
