"""Content-addressed embedding cache: encode a passage once, not once per build.

`build_index` drops `chunks_vec` and rebuilds it from scratch every run, so
landing ONE new document re-encodes the whole corpus. Measured on this corpus
(12,643 vectors, bge-m3 int8, 8 workers): ~37 minutes, of which everything but
the new document is recomputation of bytes that did not change. That is the
largest single cost in the pipeline and it is not a model problem -- no
accelerator makes recomputing an unchanged vector worthwhile.

Three decisions worth stating, because each has a wrong-looking cheaper option:

  * The key is `sha256(embed_text)`, NOT the `content_sha256` column that gold
    already carries. `gold.chunks` holds both `content` and `embed_text`, and it
    is `embed_text` that gets encoded (build_rag). They differ (heading prefix),
    so keying on `content_sha256` would hand two chunks with identical bodies but
    different headings the same vector -- silently, and only for the chunks where
    it matters most.
  * The validity key is the VECTOR signature (provider | model | dim | precision),
    not `index_signature`. The index signature also covers the FTS tokenizer,
    the n-gram widths, the doctype profiles and Kiwi -- none of which move a
    vector. Invalidating 12k vectors because the FTS tokenizer changed is a
    37-minute mistake, so the two signatures are deliberately separate.
  * The cache is its own file under `data/processed/`, not a table inside
    `index.sqlite`. The serving index is what `make sync` ships to spokes and a
    spoke has no use for a build cache; keeping it out also keeps the shipped
    artefact the same size. `make clean` removes it with the rest of
    `data/processed`; `make clean-index` deliberately does not, so wiping the
    index to rebuild it stays cheap.

Bit-stability is preserved exactly. The cache stores the same float32 blob the
index stores (`serialize_vector`), so a reused vector re-packs to the identical
bytes a freshly encoded one would; the float64 the encoder produced is not
carried, but nothing downstream of `build_index` sees it.

A cache failure is never a build failure: a corrupt or unreadable cache file is
reported and bypassed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import sys
from collections.abc import Sequence
from pathlib import Path

# Bumped only when the on-disk layout changes. Part of the validity key, so an
# old cache is dropped rather than misread.
CACHE_LAYOUT = "v1"

# Passages per cache commit. Small enough that an interrupted cold build keeps
# almost all of its work, large enough that the commit itself is noise.
CHECKPOINT = 256


def embed_key(text: str) -> str:
    """Cache key for one passage: the hash of the exact string that gets encoded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vector_signature(embedder, dimensions: int) -> str:
    """Everything that changes the VECTOR for a fixed input string.

    Deliberately narrower than `index_signature`: the keyword half, the doctype
    profiles and Kiwi all belong there and none of them belong here.

    IT DOES INCLUDE THE EXECUTION PROVIDER, and index_signature deliberately does
    not. That asymmetry is the whole point and it is not an inconsistency:

      * index_signature travels. `make sync` ships the index to spokes, so putting
        the provider in it would hand every CPU-only spoke an index it refuses to
        read.
      * This cache does NOT travel. It lives under data/processed/ and is never
        synced, so it can afford to be stricter -- and it has to be. The cache
        commits every 256 passages, so a build that runs on one provider and a
        later build that runs on another leave ONE cache holding vectors from two
        providers. Measured, two providers on the same asset agree to cosine
        0.999991 and still flipped a top-10 -- and `verify_sample`'s 0.97 floor is
        far too loose to notice. index_signature would stay byte-identical
        throughout.

    Appended only when non-empty (the CPU provider contributes nothing), so every
    cache that exists today keeps its exact signature and nothing re-encodes.
    """
    model = getattr(embedder, "model_name", "") or ""
    # Empty for providers with no precision axis (hashing, sentence_transformers),
    # so their signature is unchanged by the existence of this field.
    precision = getattr(embedder, "precision", "") or ""
    parts = [CACHE_LAYOUT, embedder.name, model, str(dimensions), precision]
    suffix = getattr(embedder, "vector_space_suffix", "") or ""
    if suffix:
        parts.append(suffix)
    return "|".join(parts)


def cache_enabled() -> bool:
    """EMBED_CACHE=0 disables reuse (forces a full re-encode). Default on."""
    return os.environ.get("EMBED_CACHE", "1").strip().lower() not in {"0", "false", "no"}


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes, dimensions: int) -> list:
    return list(struct.unpack(f"{dimensions}f", blob))


class VectorCache:
    """SQLite-backed `embed_key -> vector`, valid for exactly one vector signature."""

    def __init__(self, path: Path, signature: str, dimensions: int):
        self.path = Path(path)
        self.signature = signature
        self.dimensions = dimensions
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "embed_key TEXT PRIMARY KEY, embedding BLOB NOT NULL)"
        )
        stored = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'vector_signature'"
        ).fetchone()
        if stored is None or stored[0] != signature:
            # A different embedder/model/dimension/precision: every stored vector
            # is from another vector space. Drop them rather than mix spaces.
            self._connection.execute("DELETE FROM vectors")
            self._connection.execute(
                "INSERT INTO meta (key, value) VALUES ('vector_signature', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (signature,),
            )
            self._connection.commit()

    def get_many(self, keys: Sequence[str]) -> dict:
        """Cached vectors for `keys`, keyed by embed_key. Missing keys are absent."""
        found: dict = {}
        keys = list(keys)
        # Chunked IN() so a 12k-key lookup stays under SQLite's variable limit.
        for start in range(0, len(keys), 500):
            window = keys[start : start + 500]
            placeholders = ",".join("?" * len(window))
            for key, blob in self._connection.execute(
                f"SELECT embed_key, embedding FROM vectors WHERE embed_key IN ({placeholders})",
                window,
            ):
                if len(blob) != self.dimensions * 4:
                    # Dimension drift the signature failed to catch: skip rather
                    # than hand back a vector of the wrong width.
                    continue
                found[key] = _unpack(blob, self.dimensions)
        return found

    def put_many(self, items: Sequence[tuple]) -> None:
        with self._connection:
            self._connection.executemany(
                "INSERT INTO vectors (embed_key, embedding) VALUES (?, ?) "
                "ON CONFLICT(embed_key) DO UPDATE SET embedding = excluded.embedding",
                [(key, _pack(vector)) for key, vector in items],
            )

    def prune(self, live_keys: Sequence[str]) -> int:
        """Drop entries no longer present in the corpus. Returns rows removed."""
        with self._connection:
            self._connection.execute("CREATE TEMP TABLE IF NOT EXISTS live (k TEXT PRIMARY KEY)")
            self._connection.execute("DELETE FROM live")
            self._connection.executemany(
                "INSERT OR IGNORE INTO live (k) VALUES (?)", [(key,) for key in live_keys]
            )
            cursor = self._connection.execute(
                "DELETE FROM vectors WHERE embed_key NOT IN (SELECT k FROM live)"
            )
            return cursor.rowcount or 0

    def count(self) -> int:
        return int(self._connection.execute("SELECT count(*) FROM vectors").fetchone()[0])

    def close(self) -> None:
        self._connection.close()


def encode_with_cache(embedder, texts: Sequence[str], cache_path, dimensions: int) -> tuple:
    """Encode `texts`, reusing cached vectors. Returns (vectors, stats).

    Order is preserved exactly, and duplicate strings are encoded once. With the
    cache disabled or unusable this degrades to a plain `embedder.encode`, so the
    caller has one code path.
    """
    texts = list(texts)
    stats = {"total": len(texts), "hits": 0, "encoded": 0, "pruned": 0, "cache": "on"}
    if not texts:
        return [], {**stats, "cache": "empty"}

    if not cache_enabled():
        return list(embedder.encode(texts)), {**stats, "encoded": len(texts), "cache": "off"}

    keys = [embed_key(text) for text in texts]
    signature = vector_signature(embedder, dimensions)
    try:
        cache = VectorCache(cache_path, signature, dimensions)
    except sqlite3.Error as error:
        # Never let a cache problem stop a build.
        print(f"[cache] unusable ({error}); encoding everything", file=sys.stderr)
        return list(embedder.encode(texts)), {**stats, "encoded": len(texts), "cache": "error"}

    try:
        cached = cache.get_many(sorted(set(keys)))
        # Encode each MISSING key once, even when the same passage appears under
        # several representatives.
        pending: list = []
        pending_seen: set = set()
        for key, text in zip(keys, texts, strict=True):
            if key in cached or key in pending_seen:
                continue
            pending_seen.add(key)
            pending.append((key, text))

        if pending:
            # Checkpoint every CHECKPOINT passages rather than once at the end. A
            # cold encode of this corpus runs for ~37 minutes; committing only at
            # the end means a Ctrl-C at minute 36 saves nothing, and the next
            # attempt starts from zero. It also caps peak memory: the full result
            # list for 12k x 1024 floats is hundreds of MB of Python floats.
            written: list = []
            for start in range(0, len(pending), CHECKPOINT):
                window = pending[start : start + CHECKPOINT]
                fresh = embedder.encode([text for _, text in window])
                items = list(zip([key for key, _ in window], fresh, strict=True))
                cache.put_many(items)
                written.extend(key for key, _ in items)
                if len(pending) > CHECKPOINT:
                    done = min(start + CHECKPOINT, len(pending))
                    print(
                        f"[cache] encoded {done}/{len(pending)}", file=sys.stderr, flush=True
                    )
            # Re-read what was just written so hits and misses come back through
            # the SAME float32 round-trip. Otherwise the first build after a
            # change returns float64 vectors while every later build returns
            # float32 ones -- identical once packed into the index, but not
            # identical to anything that compares the returned lists.
            for start in range(0, len(written), 500):
                cached.update(cache.get_many(written[start : start + 500]))

        stats["encoded"] = len(pending)
        stats["hits"] = len(texts) - sum(1 for key in keys if key in pending_seen)
        stats["pruned"] = cache.prune(sorted(set(keys)))
        stats["stored"] = cache.count()
        return [cached[key] for key in keys], stats
    finally:
        cache.close()


# A re-encode cannot reproduce the build's batch composition, and the tokenizer
# pads to the longest sequence in the batch -- so the same passage encoded in a
# different grouping legitimately lands ~0.99 from its cached vector (measured:
# 0.9918 for an extreme pairing). This threshold is therefore NOT a float-noise
# bound; it is set to catch the failure the check exists for, which is a vector
# belonging to a different passage or a different model. Those score near zero,
# not near one.
SAMPLE_MIN_COSINE = 0.97


def verify_sample(embedder, cache_path, dimensions: int, texts: Sequence[str], sample: int = 8):
    """Re-encode a sample of cached passages and compare. Returns (checked, worst).

    The guard against silent cache poisoning: a wrong vector never raises, it just
    degrades retrieval slowly. `worst` is the lowest cosine similarity found
    between the cached vector and a fresh encoding; see SAMPLE_MIN_COSINE for why
    that is not expected to be 1.0.
    """
    texts = list(texts)
    if not texts:
        return 0, 1.0
    signature = vector_signature(embedder, dimensions)
    cache = VectorCache(cache_path, signature, dimensions)
    try:
        # Deterministic sample: sorted keys, evenly spaced. No RNG, so a failure
        # is reproducible from the same corpus.
        keys = sorted({embed_key(text): text for text in texts}.items())
        if not keys:
            return 0, 1.0
        step = max(1, len(keys) // sample)
        picked = keys[::step][:sample]
        cached = cache.get_many([key for key, _ in picked])
        picked = [(key, text) for key, text in picked if key in cached]
        if not picked:
            return 0, 1.0
        fresh = embedder.encode([text for _, text in picked])
        worst = 1.0
        for (key, _), vector in zip(picked, fresh, strict=True):
            stored = cached[key]
            dot = sum(a * b for a, b in zip(stored, vector, strict=True))
            norm_a = sum(a * a for a in stored) ** 0.5
            norm_b = sum(b * b for b in vector) ** 0.5
            cosine = dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
            worst = min(worst, cosine)
        return len(picked), worst
    finally:
        cache.close()


__all__ = [
    "CACHE_LAYOUT",
    "VectorCache",
    "cache_enabled",
    "embed_key",
    "encode_with_cache",
    "vector_signature",
    "verify_sample",
]
