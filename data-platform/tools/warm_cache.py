"""Rebuild the vector cache from the index you already have. No second download.

Landing one new document re-encodes the whole corpus unless `vector_cache.sqlite`
is warm -- 1948.2 s against 51.5 s [M:cold-rebuild, M:chunk-650]. A fresh clone
that installed the index with `make fetch-index` has no cache, so its first
`make build` after adding a document pays the full cold price.

It does not have to. THE CACHE IS ALREADY IN THE INDEX. `chunks_vec` stores one
float32 vector per representative chunk under an explicit `chunk_id` primary key,
and the cache key is `sha256(embed_text)` where `embed_text` is a pure function of
columns `chunks` already holds -- title, rel_path, heading, content, composed by
`transform/models/gold/chunks.sql`. So the cache is DERIVABLE, and deriving it
beats shipping it: the file is 92.9 MB (larger than the compressed index), and a
derived cache cannot disagree with the index the way a separately uploaded one can.

Measured before this tool was written: reconstructed keys and bytes matched a real
cache 19 808 / 19 808, and a `make index` on the reconstruction reported
`19808 reused, 0 encoded` in 60 s, producing vectors byte-identical to the
canonical build [M:cache-from-index].

ONLY FROM A PUBLISHED INDEX, on purpose. The signature that decides whether a
cache is valid includes the EXECUTION PROVIDER, and `index_signature` deliberately
does not -- so an index cannot say which provider encoded it. The published one
can, because `index_release.json` carries the `vector_signature` recorded by the
build that made it. For a locally built index there is no such record, and
guessing it would fill a cache with vectors labelled as something they may not be:
exactly the "one cache holding vectors from two providers" failure
`pipeline/vector_cache.py` is written to prevent. A tree that built its own index
already has the cache that build wrote.

A consumer whose provider differs loses nothing: their build computes its own
signature, sees the mismatch, and drops the cache rather than mixing spaces.

    uv run python tools/warm_cache.py
    uv run python tools/warm_cache.py --verify-against-lake   # drift check, needs the lake
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from index_release import POINTER, _sha256

ROOT = Path(__file__).resolve().parent.parent


def embed_text_of(title, rel_path, heading, content) -> str:
    """`gold/chunks.sql`'s embed_text, recomputed from what the index stores.

    The SQL is CONCAT_WS(CHR(10), '# ' || COALESCE(title, rel_path),
    CASE WHEN heading <> title THEN '## ' || heading ELSE '' END, content).

    Two null rules decide 282 of the rows and neither is guessable from the shape:
    CONCAT_WS drops NULL arguments but KEEPS empty strings (so the middle line is
    present-but-empty when heading equals title), and `heading <> title` is NULL
    when either side is NULL, which sends the CASE to its ELSE. Getting these
    wrong does not corrupt anything -- a wrong key is a cache miss and the passage
    is re-encoded -- but it silently buys nothing, which is why
    `--verify-against-lake` exists.
    """
    head = f"# {title if title is not None else rel_path}"
    middle = f"## {heading}" if (heading is not None and title is not None and heading != title) else ""
    return "\n".join([head, middle, content])


def _open_index(path: Path):
    from pipeline.build_rag import connect_index

    return connect_index(path, read_only=True)


def verify_against_lake() -> int:
    """Does the recomputed embed_text still equal the one the lake produced?

    The recomputation is a COPY of an expression that lives in SQL, and copies
    drift. The failure mode is quiet -- every key changes, every lookup misses,
    the build just gets slow again -- so something has to compare them while the
    lake is around to ask. `make verify` runs this after the build.
    """
    from pipeline import get_paths
    from pipeline.build_rag import open_lake

    paths = get_paths()
    if not paths.index_sqlite.exists():
        print("[warm-cache] no index; nothing to verify against.")
        return 0

    lake = open_lake(paths)
    try:
        rows = lake.execute(
            "SELECT chunk_id, title, rel_path, heading, content, embed_text FROM lake.gold.chunks"
        ).fetchall()
    finally:
        lake.close()

    if not rows:
        print("[warm-cache] the lake holds no chunks; nothing to verify.")
        return 0

    mismatches = [
        (chunk_id, expected)
        for chunk_id, title, rel_path, heading, content, expected in rows
        if embed_text_of(title, rel_path, heading, content) != expected
    ]
    if mismatches:
        chunk_id, expected = mismatches[0]
        print(
            f"FAIL: embed_text_of() no longer matches gold/chunks.sql "
            f"({len(mismatches)} of {len(rows)} row(s)).\n"
            f"  first: {chunk_id}\n"
            f"  lake:  {expected[:120]!r}\n"
            "  A cache warmed from an index would key on the wrong text, miss every lookup,\n"
            "  and quietly re-encode the corpus. Update embed_text_of() to match the SQL."
        )
        return 1
    print(f"OK: embed_text_of() matches gold/chunks.sql on all {len(rows)} chunk(s)")
    return 0


def warm(if_missing: bool = False) -> int:
    from pipeline import get_paths

    paths = get_paths()
    index = paths.index_sqlite
    cache = paths.vector_cache

    if not index.exists():
        raise SystemExit(f"[warm-cache] no index at {index}. Run `make fetch-index` first.")
    if cache.exists():
        # `make quickstart` runs this every time, and a second run must not fail on
        # a state that is already correct. An explicit flag rather than a swallowed
        # error: nothing else gets to decide that a refusal was not serious.
        if if_missing:
            print(f"[warm-cache] {cache.name} already exists; leaving it alone.")
            return 0
        raise SystemExit(
            f"[warm-cache] {cache} already exists. It is not overwritten: a cache can hold\n"
            "  vectors from a model or provider this would not notice. Delete it deliberately\n"
            "  if you mean to replace it."
        )
    if not POINTER.exists():
        raise SystemExit(
            f"[warm-cache] no {POINTER.name}, so there is no record of which provider encoded\n"
            "  this index -- and the index does not carry one. Nothing written."
        )

    pointer = json.loads(POINTER.read_text(encoding="utf-8"))
    signature = pointer.get("vector_signature")
    if not signature:
        raise SystemExit(
            f"[warm-cache] {POINTER.name} carries no `vector_signature`; it was written by an\n"
            "  older publish. Re-publish, or build the cache the ordinary way with `make build`."
        )

    actual = _sha256(index)
    if actual != pointer["sha256_sqlite"]:
        raise SystemExit(
            "[warm-cache] this index is not the published one, so the provider that encoded it\n"
            "  is unknown (`index_signature` deliberately omits it). Filling a cache with a\n"
            "  guessed signature is how vectors from two providers end up in one cache.\n"
            f"  published: {pointer['sha256_sqlite']}\n"
            f"  local:     {actual}\n"
            "  A tree that built its own index already has the cache that build wrote."
        )

    connection = _open_index(index)
    try:
        dimensions = int(
            connection.execute(
                "SELECT value FROM index_meta WHERE key = 'embedding_dim'"
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT c.title, c.rel_path, c.heading, c.content, v.embedding
            FROM chunks_vec AS v JOIN chunks AS c ON c.chunk_id = v.chunk_id
            """
        ).fetchall()
    finally:
        connection.close()

    if not rows:
        raise SystemExit("[warm-cache] the index holds no vectors; nothing to warm.")

    expected_bytes = dimensions * 4
    staged = cache.with_suffix(".sqlite.warming")
    staged.unlink(missing_ok=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(staged))
    try:
        out.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        out.execute("CREATE TABLE vectors (embed_key TEXT PRIMARY KEY, embedding BLOB NOT NULL)")
        out.execute("INSERT INTO meta (key, value) VALUES ('vector_signature', ?)", (signature,))
        for title, rel_path, heading, content, embedding in rows:
            # vec0 stores float[dim]; anything else is a layout this tool has never
            # seen and must not silently write into a cache the build will trust.
            if len(embedding) != expected_bytes:
                raise SystemExit(
                    f"[warm-cache] a vector is {len(embedding)} B, expected {expected_bytes} "
                    f"({dimensions} float32). Nothing written."
                )
            key = hashlib.sha256(embed_text_of(title, rel_path, heading, content).encode("utf-8")).hexdigest()
            out.execute("INSERT OR REPLACE INTO vectors (embed_key, embedding) VALUES (?, ?)", (key, embedding))
        out.commit()
        written = out.execute("SELECT count(*) FROM vectors").fetchone()[0]
    except BaseException:
        out.close()
        staged.unlink(missing_ok=True)
        raise
    out.close()

    # Representatives are unique by construction, so one key per row. Fewer means
    # two different chunks recomputed to the same embed_text -- a bug in the
    # recomputation, not a property of the corpus.
    if written != len(rows):
        staged.unlink(missing_ok=True)
        raise SystemExit(
            f"[warm-cache] {len(rows)} vector(s) collapsed into {written} key(s), so "
            "embed_text_of() is not reproducing what was encoded. Nothing written."
        )

    free = shutil.disk_usage(cache.parent).free
    if free < staged.stat().st_size:
        staged.unlink(missing_ok=True)
        raise SystemExit("[warm-cache] not enough free space to install the cache. Nothing written.")

    os.replace(staged, cache)
    print(f"[warm-cache] {written:,} vector(s) -> {cache} ({cache.stat().st_size / 1e6:.0f} MB)")
    print(f"[warm-cache] signature {signature}")
    print("[warm-cache] `make build` now re-encodes only what changed. The result is marked")
    print("[warm-cache] build_kind=incremental, which cannot record an eval floor or be published.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verify-against-lake",
        action="store_true",
        help="Only check that embed_text_of() still matches gold/chunks.sql (needs the lake)",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Succeed quietly when a cache already exists (for `make quickstart`)",
    )
    args = parser.parse_args(argv)
    return verify_against_lake() if args.verify_against_lake else warm(args.if_missing)


if __name__ == "__main__":
    sys.exit(main())
