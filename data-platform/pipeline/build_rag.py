"""Build the retrieval half of the serving index: sqlite-vec + FTS5 + RRF.

Reads `lake.gold.chunks` out of DuckLake and writes three tables into the single
SQLite file at `data/serving/index.sqlite`:

  chunks      plain table holding the text and its provenance
  chunks_vec  sqlite-vec virtual table (vec0) holding one embedding per chunk
  chunks_fts  FTS5 index over the same rows

Why both: a vector index alone misses exact identifiers. "hybrid_search" is one
token to a reader and a fuzzy direction in embedding space, so a pure-vector
query happily returns "the retrieval layer" instead of the function itself. FTS5
nails the literal token; the vector side catches paraphrase. Reciprocal rank
fusion combines the two rankings without needing their scores to be comparable,
which they are not: bm25 is an unbounded negative log-odds and cosine distance
is bounded in [0, 2].

Everything here runs offline. The default embedder is deterministic and local,
so a build never blocks on a model download (§3 invariants).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import struct
import sys
import unicodedata
from collections.abc import Sequence
from functools import cache, lru_cache
from itertools import pairwise
from pathlib import Path

import duckdb
import sqlite_vec

from pipeline import Paths, Settings, get_paths, get_settings, runtime
from pipeline.aliases import expand_query
from pipeline.vector_cache import encode_with_cache

# Allocation failures surface as different exception types per execution provider
# (onnxruntime's Fail/RuntimeException, a DirectML HRESULT, a CUDA OOM), so they
# are recognised by message rather than by class.
_MEMORY_ERROR_MARKERS = (
    "out of memory",
    "oom",
    "failed to allocate",
    "cudaerrormemoryallocation",
    "insufficient",
    "0x8007000e",  # E_OUTOFMEMORY, how DirectML reports it
)


def _is_memory_error(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in _MEMORY_ERROR_MARKERS)

# Scripts that write without spaces between words, so a "word" is not a
# retrievable unit and has to be broken into character n-grams. Written as
# explicit codepoint ranges rather than literal glyphs: a mistyped literal is
# invisible in review and silently swallows the Private Use Area, which Hancom
# HWP files use routinely.
CJK_RANGES = (
    "ᄀ-ᇿ"  # Hangul Jamo
    "぀-ヿ"  # Hiragana + Katakana
    "㄰-㆏"  # Hangul Compatibility Jamo
    "㐀-䶿"  # CJK Unified Ideographs Extension A
    "一-鿿"  # CJK Unified Ideographs
    "ꥠ-꥿"  # Hangul Jamo Extended-A
    "가-힣"  # Hangul Syllables
    "ힰ-퟿"  # Hangul Jamo Extended-B
    "豈-﫿"  # CJK Compatibility Ideographs
    "ｦ-ﾟ"  # Halfwidth Katakana
)
CJK_RUN = re.compile(f"[{CJK_RANGES}]+")

# Order is load-bearing. The ASCII-identifier branch must come first so that
# `hybrid_search` stays ONE token, preserving the agreement with the FTS5
# `tokenchars '_'` setting. The CJK branch must come before the general-word
# branch, otherwise a mixed run like `가상자산ETF` is captured as a single token
# and then shredded entirely into CJK n-grams, destroying the `etf` unigram.
TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"|\d+"
    f"|[{CJK_RANGES}]+"
    r"|[^\W\d_]+",
    re.UNICODE,
)

FTS_TOKENIZE = "unicode61 remove_diacritics 2 tokenchars '_'"

# Character n-gram widths used for CJK runs, on both sides of the index.
CJK_NGRAM_SIZES = (2, 3)
# Width of the n-grams written into chunks_fts and used to build MATCH queries.
# Two, not three: Korean legal vocabulary is saturated with two-character terms
# (증권, 부칙, 신고), and a trigram index cannot match them at all.
FTS_NGRAM_SIZE = 2

MIN_EMBEDDING_DIM = 512


def normalise(text: str) -> str:
    """NFC, never NFKC.

    NFC composes decomposed Hangul jamo, which macOS filesystems and several HWP
    extractors emit, so the two forms hash identically. NFKC would go further and
    fold circled numerals to plain digits, but ①②③ are 항 markers in every Korean
    statute, so folding them would make the embedder and FTS5 index the same
    character differently.
    """
    return unicodedata.normalize("NFC", text)


# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------
def tokenize(text: str) -> list:
    """Lower-cased tokens: ASCII identifiers whole, CJK runs whole, digits.

    Both halves of the index depend on this agreeing with FTS5. If they
    disagreed about whether `hybrid_search` is one token or two, the same query
    would address different vocabularies on each side of the fusion.
    """
    return [match.group(0).lower() for match in TOKEN_RE.finditer(normalise(text))]


def _cjk_ngrams(token: str, sizes: Sequence[int] = CJK_NGRAM_SIZES) -> list:
    """Overlapping character n-grams for one CJK run.

    Korean glues particles onto nouns (가상자산사업자는), so the whole run is
    almost never a retrievable unit. Character n-grams are what make the noun
    inside it reachable.
    """
    grams = []
    for size in sizes:
        if len(token) < size:
            continue
        grams.extend(token[i : i + size] for i in range(len(token) - size + 1))
    return grams or [token]


def _features(text: str) -> dict:
    """Feature counts for one document, shared by every hashing embedder call.

    ASCII and numeric tokens are kept whole; CJK runs are expanded to character
    n-grams. On pure-ASCII input this produces exactly the unigram+bigram feature
    set the previous implementation did, so English and code retrieval cannot
    regress.
    """
    tokens = tokenize(text)
    if not tokens:
        return {}

    counts: dict = {}
    units: list = []
    for token in tokens:
        if CJK_RUN.fullmatch(token):
            grams = _cjk_ngrams(token)
            units.extend(grams)
            for gram in grams:
                counts[gram] = counts.get(gram, 0) + 1
        else:
            units.append(token)
            counts[token] = counts.get(token, 0) + 1

    for left, right in pairwise(units):
        pair = f"{left}_{right}"
        counts[pair] = counts.get(pair, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Optional Kiwi morphological augmentation (KIWI_MORPH=1, `uv sync --extra kiwi`)
# --------------------------------------------------------------------------
# Appends NNG/NNP/XR noun/root lemmas BESIDE the char-bigram backbone on both the
# write path (expand_cjk) and the query path (build_fts_query), so a josa-glued
# query (사업자의) also matches the bare-noun answer (사업자). It NEVER replaces the
# bigrams, so the context-free write>=query lock-step is untouched: this can only
# ADD a josa-free noun match, never silence the keyword half. Off by default;
# index_signature carries a kiwi slot so a Kiwi index is never queried Kiwi-off.
_KIWI_NOUN_TAGS = frozenset({"NNG", "NNP", "XR"})  # common/proper noun, root
_kiwi_analyzer = None


def _kiwi_enabled() -> bool:
    return os.environ.get("KIWI_MORPH", "0").strip().lower() not in {"", "0", "false", "no"}


@lru_cache(maxsize=1)
def _kiwi_model_version() -> str:
    try:
        from importlib.metadata import version

        return version("kiwipiepy_model")
    except Exception:
        return "unknown"


def _kiwi_signature() -> str:
    """Empty when off, so a Kiwi-off index keeps its exact prior signature."""
    return f"kiwi-add-{_kiwi_model_version()}" if _kiwi_enabled() else ""


def _get_kiwi():
    global _kiwi_analyzer
    if _kiwi_analyzer is None:
        from kiwipiepy import Kiwi

        _kiwi_analyzer = Kiwi()
    return _kiwi_analyzer


def kiwi_lemmas(text: str) -> list:
    """Deterministic NNG/NNP/XR noun/root lemmas from the CJK content of `text`.

    Empty unless KIWI_MORPH is set. Only multi-character Hangul/CJK noun forms are
    kept, so ASCII identifiers, digits, 조문 markers (①②③) and HWP Private-Use
    glyphs are never dropped or altered -- they still ride the bigram backbone.
    """
    if not _kiwi_enabled():
        return []
    lemmas = []
    for token in _get_kiwi().tokenize(normalise(text)):
        if token.tag in _KIWI_NOUN_TAGS and len(token.form) >= 2 and CJK_RUN.fullmatch(token.form):
            lemmas.append(token.form)
    return list(dict.fromkeys(lemmas))


def expand_cjk(text: str, size: int = FTS_NGRAM_SIZE) -> str:
    """Rewrite CJK runs as space-separated character n-grams for FTS5.

    FTS5's unicode61 tokenizer treats a whole Hangul run as one token, so
    "가상자산" cannot match inside "가상자산이용자보호법". Rather than switching to
    the trigram tokenizer, which cannot match two-character terms at all and
    doubles the index, we keep unicode61 and change what we feed it.

    The identical expansion is applied to queries in `build_fts_query`. If the
    two ever diverge the keyword half silently returns nothing, which is what
    the index_signature guard exists to catch.
    """

    def replace(match: re.Match) -> str:
        run = match.group(0)
        if len(run) <= size:
            return run
        return " ".join(run[i : i + size] for i in range(len(run) - size + 1))

    expanded = CJK_RUN.sub(replace, normalise(text))
    lemmas = kiwi_lemmas(text)
    return f"{expanded} {' '.join(lemmas)}" if lemmas else expanded


def _bucket(token: str, dimensions: int) -> tuple:
    """Stable (index, sign) for a token. blake2b keeps it identical across runs."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dimensions, 1.0 if (value >> 63) & 1 else -1.0


class HashingEmbedder:
    """Signed feature hashing over tokens, CJK n-grams and adjacent pairs.

    A real sentence encoder is better at genuine paraphrase, but it costs a model
    download on first use, which would make `make build` depend on the network.
    Measured on Korean legal and government retrieval this lands close to a dense
    multilingual model once fused with the keyword half, because statute queries
    reuse the statute's own vocabulary. It will not match 원화 to 금전; for that,
    switch EMBEDDING_PROVIDER (see .env.example).
    """

    name = "hashing"
    # No external model, but the attribute must exist so index_signature never
    # silently reads an empty slot from a provider that forgot to set it.
    model_name = ""

    def __init__(self, dimensions: int):
        if dimensions < MIN_EMBEDDING_DIM:
            raise ValueError(
                f"EMBEDDING_DIM must be at least {MIN_EMBEDDING_DIM} for the hashing "
                f"provider, got {dimensions}. Korean character n-grams produce a much "
                "larger feature vocabulary than English words, and below this the hash "
                "collisions destroy the signal."
            )
        self.dimensions = dimensions

    def encode(self, texts: Sequence[str]) -> list:
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text: str) -> list:
        counts = _features(text)
        vector = [0.0] * self.dimensions
        if not counts:
            return vector

        for token, count in counts.items():
            index, sign = _bucket(token, self.dimensions)
            # Sublinear term frequency: a word repeated ten times is not ten
            # times more about the topic than a word used once.
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0.0:
            return vector
        return [component / norm for component in vector]


class SentenceTransformerEmbedder:
    """Optional dense encoder. Requires the model to be present locally."""

    name = "sentence_transformers"

    def __init__(self, model_name: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=sentence_transformers requires the extra "
                "dependency. Install it with: uv add sentence-transformers"
            ) from error
        # Recorded so index_signature can tell two same-dimension models apart.
        # Without it the signature carried an empty slot, and switching between
        # two 1024-d models passed the guard while every query was encoded by one
        # model and matched against vectors built by another.
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dimensions = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: Sequence[str]) -> list:
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(component) for component in vector] for vector in vectors]


class OnnxEmbedder:
    """Optional ONNX sentence encoder, run torch-free on whatever this box has.

    The point of this provider over a static distilled table (e.g. model2vec) is
    CONTEXT: this is a real transformer, so self-attention still disambiguates a
    word by its neighbours -- a static token-average cannot, which matters for
    Korean where 조사/어미 carry meaning. int8 quantisation keeps that attention
    while cutting the model to a quarter of its float size, so it stays usable on
    CPU. It is NOT the default: it runs a model, which the build forbids for the
    default path, so it lives here in the optional slot and the zero-download
    `hashing` provider remains the default.

    The Xenova ONNX repos ship a variant with the Sentence-Transformers pooling
    and normalisation baked into the graph, so the output is already the dense
    sentence vector -- there is no client-side pooling to get wrong. The model is
    fetched once into the local Hugging Face cache; inference touches no network.

    PRECISION SELECTS THE ASSET, AND THE ASSET IS A FLEET DECISION. int8 (the
    default) is a CPU format; fp16 is what a GPU execution provider wants. They
    are different vector spaces, so the precision is part of `index_signature`:
    a fleet cannot have the hub build fp16 vectors while a spoke encodes its
    queries in int8. `pipeline/runtime.py` picks the execution provider to match,
    and deliberately stays on the CPU for int8 even when a GPU is present --
    measured, int8 on a GPU EP was slower than one CPU thread.

    The provider NAME stays `onnx_int8` whatever the precision: it is the value
    `EMBEDDING_PROVIDER` carries and the value `index_meta.embedding_provider`
    reports, and renaming it would invalidate every existing index for a cosmetic
    reason. The precision travels as its own signature slot instead, appended only
    when it is not int8 -- the same trick the Kiwi slot uses, so an int8 index
    keeps its exact prior signature and does not rebuild.
    """

    name = "onnx_int8"
    # Pooled + L2-normalised sentence-embedding graphs. Same graph, same pooling,
    # different weight precision.
    _ASSETS = {
        "int8": "onnx/sentence_transformers_int8.onnx",
        "fp16": "onnx/sentence_transformers_fp16.onnx",
    }
    _MAX_TOKENS = 512  # a 1200-char chunk fits well under this; bounds CPU cost.
    # How many tokenised batches may sit ahead of the session. Tier A of the
    # hybrid design: the CPU tokenises batch N+1 while the device runs batch N,
    # which costs nothing in determinism because the work is still ordered.
    _PREFETCH = 2

    def __init__(self, model_name: str, allow_download: bool = False, precision: str = "int8"):
        try:
            import numpy as np
            from huggingface_hub import hf_hub_download
            from huggingface_hub.errors import LocalEntryNotFoundError
            from tokenizers import Tokenizer

            if importlib.util.find_spec("onnxruntime") is None:
                raise ImportError("onnxruntime")
        except ImportError as error:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=onnx_int8 needs the onnx-embed extra. "
                "Install it with: uv sync --extra onnx-embed"
            ) from error

        self._np = np
        self.model_name = model_name
        if precision not in self._ASSETS:
            raise ValueError(
                f"EMBEDDING_PRECISION must be one of {sorted(self._ASSETS)}, got {precision!r}"
            )
        self.precision = precision

        def _fetch(filename: str) -> str:
            # The read path is strictly offline (invariant 7): try the local HF
            # cache first on BOTH paths, and branch only on a genuine cache miss.
            # The build path (allow_download=True) fetches once; the read path
            # raises with an actionable message instead of touching the network.
            # A non-miss failure (corrupt cache, permissions) surfaces as itself
            # rather than being masked as "not cached".
            try:
                return hf_hub_download(model_name, filename, local_files_only=True)
            except LocalEntryNotFoundError:
                if not allow_download:
                    raise RuntimeError(
                        f"onnx_int8 model {model_name!r} is not in the local Hugging "
                        f"Face cache. The model is fetched once during `make build`; "
                        f"the read path never downloads (invariant 7). Run `make build` "
                        f"online once to warm the cache, then retry."
                    ) from None
                return hf_hub_download(model_name, filename)

        onnx_path = _fetch(self._ASSETS[precision])
        self._tokenizer = Tokenizer.from_file(_fetch("tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=self._MAX_TOKENS)
        self._tokenizer.enable_padding()

        # Where it runs is runtime.py's decision, not this class's. On the CPU it
        # stays single-threaded for the same reason as before: a threaded matmul's
        # accumulation order is not fixed, so pinning threads keeps the vectors
        # bit-identical across rebuilds on this machine -- what index_signature
        # assumes. A GPU EP gets its own determinism options there.
        self.profile = runtime.detect(precision)
        self._onnx_path = onnx_path
        self._cpu_session = None
        # auto (default) = device alone, with CPU/device overlap (Tier A).
        # hybrid = the CPU also encodes batches (Tier B). See _encode_hybrid.
        self._devices = os.environ.get("ENCODE_DEVICES", "auto").strip().lower()
        self._session = runtime.make_session(onnx_path, self.profile, threads=1)
        # What the session ACTUALLY got, which is not always what was asked for:
        # make_session degrades to the CPU rather than failing the build.
        self.provider = self._session.get_providers()[0]
        if precision == "fp16" and self.provider == runtime.CPU_EP:
            # The misconfiguration that costs the most and shows the least: the
            # GPU asset running on the CPU. MEASURED on this box: query encoding
            # goes 12.7 ms (int8) -> 203.4 ms (fp16), 16x, for identical results.
            # It happens whenever a fleet adopts fp16 for its GPU hub and a
            # CPU-only spoke inherits the setting -- the spoke still works, just
            # 16x slower on every query, with nothing in the output to say why.
            print(
                "[embed] WARNING: EMBEDDING_PRECISION=fp16 but this machine is running on "
                "the CPU. fp16 is the GPU asset; on a CPU it is ~16x slower than int8 for "
                "the same vectors. Use int8 here, or check `make gpu-probe`.",
                file=sys.stderr,
            )
        self._batch = max(1, int(self.profile.batch))
        self._input_names = {node.name for node in self._session.get_inputs()}
        self._output_name = self._session.get_outputs()[0].name
        self.dimensions = len(self.encode(["차원 확인"])[0])

    def _tokenize(self, batch) -> dict:
        """CPU half of a batch. Split out so it can overlap the device half."""
        np = self._np
        encoded = self._tokenizer.encode_batch(list(batch))
        feeds = {
            "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
        }
        return {name: value for name, value in feeds.items() if name in self._input_names}

    def _run(self, feeds, session=None) -> list:
        # The session is a parameter, not state: Tier B runs two of them at once
        # from two threads, and swapping `self._session` around a call would make
        # the two workers race for which device ran which batch.
        np = self._np
        output = (session or self._session).run([self._output_name], feeds)[0]
        # Pooled variant returns [batch, dim]; fall back to CLS if a token-level
        # [batch, seq, dim] graph is ever substituted.
        matrix = output if output.ndim == 2 else output[:, 0, :]
        # fp16 graphs return float16; normalise in float32 or the division
        # underflows and the unit vectors come back subtly wrong.
        matrix = matrix.astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (matrix / norms).astype(float).tolist()

    def _run_with_backoff(self, feeds, session=None) -> list:
        """Run, halving the batch on an allocation failure instead of dying.

        A shared-memory laptop GPU can be out of room for a batch of 16 while a
        batch of 8 fits, and the operator cannot know that in advance.

        Splitting is NOT numerically free, and the message says so: the tokenizer
        pads to the batch's longest sequence, so the halves pad differently than
        the whole would have (measured at cosine ~0.99 for an extreme pair). The
        trade is deliberate -- a slightly different vector for one batch beats a
        failed build -- but it means an OOM-hit rebuild is not byte-identical to a
        clean one, and that is worth knowing when a rebuild "changes" for no
        apparent reason.
        """
        try:
            return self._run(feeds, session)
        except Exception as error:  # noqa: BLE001 - allocation failures are EP-specific types
            rows = len(next(iter(feeds.values())))
            if rows <= 1 or not _is_memory_error(error):
                raise
            half = rows // 2
            print(
                f"[embed] {type(error).__name__} at batch {rows}; retrying at {half}. "
                f"NOTE: the split pads differently, so these vectors differ slightly "
                f"from a clean run's.",
                file=sys.stderr,
            )
            first = {name: value[:half] for name, value in feeds.items()}
            second = {name: value[half:] for name, value in feeds.items()}
            return self._run_with_backoff(first, session) + self._run_with_backoff(second, session)

    def _encode_batch(self, batch) -> list:
        return self._run_with_backoff(self._tokenize(batch))

    def _encode_pipelined(self, batches) -> list:
        """Tier A: tokenise ahead of the device, bounded by `_PREFETCH`.

        One session, one consumer, so the order and the arithmetic are exactly
        those of the sequential path -- the only thing that changes is that the
        device stops waiting for the tokenizer. An unbounded prefetch (e.g.
        `ThreadPoolExecutor.map`) would tokenise all 12k passages up front and
        hold them in memory, which is why this walks the iterator by hand.
        """
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor

        vectors: list = []
        remaining = iter(batches)
        with ThreadPoolExecutor(max_workers=1) as pool:
            queue: deque = deque()
            for _ in range(self._PREFETCH):
                batch = next(remaining, None)
                if batch is None:
                    break
                queue.append(pool.submit(self._tokenize, batch))
            while queue:
                feeds = queue.popleft().result()
                batch = next(remaining, None)
                if batch is not None:
                    queue.append(pool.submit(self._tokenize, batch))
                vectors.extend(self._run_with_backoff(feeds))
        return vectors

    def _encode_workers(self, n_batches: int) -> int:
        override = os.environ.get("ENCODE_WORKERS")
        if override:
            try:
                return max(1, min(int(override), n_batches))
            except ValueError:
                pass
        cores = os.cpu_count() or 1
        # int8 GEMM is memory-bandwidth-bound, so throughput saturates near the
        # physical core count; leave 2 cores free and cap at 8. Determinism is
        # unaffected -- every Run still uses intra_op=1, so each chunk's vector is
        # bit-identical to the single-threaded path; only independent batches run
        # concurrently, and results are re-assembled in order.
        return max(1, min(cores - 2, 8, n_batches))

    # ---------------- Tier B: the device and the CPU share the work ----------
    def _hybrid_session(self):
        """A second session, on the CPU, running THE SAME ASSET as the device.

        The same asset is not a detail: int8 and fp16 are different vector
        spaces, so letting the CPU half encode int8 while the device half encodes
        fp16 would fill one index with vectors that do not share a geometry --
        every cosine between the two halves would be wrong, and nothing would
        raise. The cost is that the CPU half runs the fp16 graph, which it does
        through inserted casts and therefore slower than its native int8. That is
        the honest arithmetic of Tier B, and the reason it is opt-in: it pays only
        where the device is barely faster than the CPU (an integrated GPU), not
        where it is several times faster.
        """
        if self._cpu_session is None:
            self._cpu_session = runtime.make_session(
                self._onnx_path, runtime.DeviceProfile(provider=runtime.CPU_EP), threads=1
            )
        return self._cpu_session

    def _hybrid_split(self, sample) -> tuple:
        """Integer (device, cpu) batch shares. Measured once, then RECORDED.

        Measuring is a timing, and a timing is not reproducible -- so the ratio is
        persisted in the device profile and reused. Bit-stability under Tier B is
        therefore "same machine, same recorded split", which is the same shape of
        promise as before, just with one more thing written down. `ENCODE_SPLIT=g:c`
        skips the measurement entirely and is the deterministic path.
        """
        override = os.environ.get("ENCODE_SPLIT", "").strip()
        if override:
            device_share, _, cpu_share = override.partition(":")
            return max(1, int(device_share)), max(0, int(cpu_share))

        from fractions import Fraction
        from time import perf_counter

        paths = get_paths()
        profile_path = paths.processed / "device_profile.json"
        recorded = runtime.load_profile(profile_path) or {}
        split = (recorded.get("measurements") or {}).get("encode_split")
        if split:
            return int(split[0]), int(split[1])

        def rate(session, feeds) -> float:
            self._run_on(session, feeds)  # warm up; first run compiles/allocates
            start = perf_counter()
            self._run_on(session, feeds)
            elapsed = perf_counter() - start
            return 1.0 / elapsed if elapsed > 0 else 0.0

        feeds = self._tokenize(sample)
        device_rate = rate(self._session, feeds)
        cpu_rate = rate(self._hybrid_session(), feeds)
        if cpu_rate <= 0 or device_rate <= 0:
            return 1, 0
        fraction = Fraction(device_rate / cpu_rate).limit_denominator(4)
        device_share, cpu_share = max(1, fraction.numerator), max(1, fraction.denominator)
        print(
            f"[embed] hybrid split measured: device {device_rate:.2f}/s vs cpu {cpu_rate:.2f}/s "
            f"-> {device_share}:{cpu_share}"
            + ("  (device is >3x the cpu; hybrid buys little here)" if device_rate > 3 * cpu_rate else ""),
            file=sys.stderr,
        )
        runtime.save_profile(profile_path, self.profile, {"encode_split": [device_share, cpu_share]})
        return device_share, cpu_share

    def _run_on(self, session, feeds) -> list:
        return self._run_with_backoff(feeds, session)

    def _encode_hybrid(self, batches) -> list:
        """Run batches on the device and the CPU concurrently, by a FIXED pattern.

        Not work-stealing on purpose. A stealing queue rebalances beautifully and
        assigns batch N to whichever device is free, which changes run to run --
        and with it the low bits of every vector that happened to land elsewhere.
        A fixed repeating pattern keeps the assignment a function of the batch
        index, so a rerun with the same split reproduces the same bytes.
        """
        from concurrent.futures import ThreadPoolExecutor

        device_share, cpu_share = self._hybrid_split(batches[0])
        if cpu_share <= 0:
            return self._encode_pipelined(batches)
        pattern = [0] * device_share + [1] * cpu_share
        assignment = [pattern[index % len(pattern)] for index in range(len(batches))]
        results: list = [None] * len(batches)

        def worker(device: int, session) -> None:
            for index, batch in enumerate(batches):
                if assignment[index] != device:
                    continue
                results[index] = self._run_on(session, self._tokenize(batch))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, 0, self._session),
                pool.submit(worker, 1, self._hybrid_session()),
            ]
            for future in futures:
                future.result()
        return [vector for batch_result in results for vector in batch_result]

    def encode(self, texts: Sequence[str]) -> list:
        texts = list(texts)
        if not texts:
            return []
        batches = [texts[i : i + self._batch] for i in range(0, len(texts), self._batch)]
        if self.provider != runtime.CPU_EP and self._devices == "hybrid" and len(batches) > 1:
            return self._encode_hybrid(batches)
        if self.provider != runtime.CPU_EP:
            # One device, one queue. Running N concurrent sessions against a
            # single GPU serialises inside the driver anyway and multiplies the
            # resident model, so the parallelism that pays here is the CPU/device
            # overlap, not more sessions.
            return self._encode_pipelined(batches)
        workers = self._encode_workers(len(batches))
        if workers <= 1:
            return [vector for batch in batches for vector in self._encode_batch(batch)]
        # onnxruntime releases the GIL during run() and Run() is thread-safe, so
        # independent batches encode on separate cores while each stays single-
        # threaded (intra_op=1). ThreadPoolExecutor.map preserves batch order, so
        # the assembled result is bit-identical to the sequential path (verified by
        # the ENCODE_WORKERS=1 vs N equality check).
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._encode_batch, batches))
        return [vector for batch_result in results for vector in batch_result]


@cache
def _load_embedder(provider: str, model: str, dim: int, allow_download: bool, precision: str):
    """Construct the embedder once per (provider, model, dim, allow_download, precision).

    Cached for the process, so the read path does not rebuild the ONNX session on
    every query. Keyed on primitives (not the Settings object) so the key is
    explicit and stable.
    """
    if provider == "hashing":
        return HashingEmbedder(dim)
    if provider == "sentence_transformers":
        # NOTE: this provider still downloads on a cold cache at construction
        # (SentenceTransformer(model_name)); the read-path offline guarantee is
        # currently restored for onnx_int8 only. Gating it is a follow-up.
        return SentenceTransformerEmbedder(model)
    if provider == "onnx_int8":
        return OnnxEmbedder(model, allow_download=allow_download, precision=precision)
    raise ValueError(
        "EMBEDDING_PROVIDER must be 'hashing', 'sentence_transformers' or "
        f"'onnx_int8', got {provider!r}"
    )


def resolve_precision(settings: Settings) -> str:
    """The asset this fleet uses. `auto` means "fp16 if this box has a GPU for it".

    `auto` is offered for a single-machine setup and is deliberately NOT the
    default: in a hub/spoke fleet it would let each node pick its own vector
    space, and the first sync would ship an index whose vectors no spoke can
    match. The default (int8) keeps every node in the space it is in today.
    """
    requested = (settings.embedding_precision or "int8").strip().lower()
    if requested != "auto":
        return requested
    return runtime.precision_for(runtime.detect("fp16").provider)


def cached_asset_path(settings: Settings | None = None) -> str | None:
    """Local path of the embedder asset, or None if it is not cached yet.

    For `make gpu-probe`, which reports per-EP graph support and must never
    download to do it.
    """
    settings = settings or get_settings()
    if settings.embedding_provider != "onnx_int8":
        return None
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        return hf_hub_download(
            settings.embedding_model,
            OnnxEmbedder._ASSETS[resolve_precision(settings)],
            local_files_only=True,
        )
    except (LocalEntryNotFoundError, KeyError, OSError):
        return None


def get_embedder(settings: Settings, allow_download: bool = False):
    """Resolve the configured embedder (cached, once per process).

    `allow_download` is True only on the build path (`build_index`); the read path
    leaves it False so a missing onnx_int8 model raises rather than silently
    touching the network (invariant 7).
    """
    return _load_embedder(
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_dim,
        allow_download,
        resolve_precision(settings),
    )


def serialize_vector(vector: Sequence[float]) -> bytes:
    """Pack a float list into the compact BLOB layout sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


# --------------------------------------------------------------------------
# SQLite plumbing
# --------------------------------------------------------------------------
def connect_index(index_path, read_only: bool = False) -> sqlite3.Connection:
    """Open the serving index with sqlite-vec loaded."""
    if read_only:
        connection = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(str(index_path))
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    return connection


def open_lake(paths: Paths) -> duckdb.DuckDBPyConnection:
    """Attach the DuckLake catalog read-side."""
    connection = duckdb.connect()
    connection.execute("INSTALL ducklake")
    connection.execute("LOAD ducklake")
    connection.execute(f"ATTACH 'ducklake:{paths.ducklake_catalog}' AS lake")
    return connection


def _create_schema(connection: sqlite3.Connection, dimensions: int) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS chunks_fts;
        CREATE TABLE chunks (
            chunk_id       TEXT PRIMARY KEY,
            doc_id         TEXT NOT NULL,
            rel_path       TEXT NOT NULL,
            collection     TEXT NOT NULL,
            title          TEXT,
            doc_type       TEXT,
            heading        TEXT,
            chunk_index    INTEGER NOT NULL,
            content        TEXT NOT NULL,
            char_start     INTEGER,
            char_end       INTEGER,
            token_estimate INTEGER,
            content_sha256 TEXT
        );
        CREATE INDEX chunks_doc_id ON chunks (doc_id);
        CREATE INDEX chunks_collection ON chunks (collection);
        CREATE TABLE IF NOT EXISTS index_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    connection.execute("DROP TABLE IF EXISTS chunks_vec")
    connection.execute(
        f"CREATE VIRTUAL TABLE chunks_vec USING vec0("
        f"chunk_id TEXT PRIMARY KEY, embedding float[{dimensions}])"
    )
    connection.execute(
        f"CREATE VIRTUAL TABLE chunks_fts USING fts5("
        f"chunk_id UNINDEXED, title, heading, content, tokenize = \"{FTS_TOKENIZE}\")"
    )


ROOT_COLLECTION = "_root"


def collection_of(rel_path: str) -> str:
    """The scoping bucket for a document: its first folder under the inbox.

    `sto/열매_증권신고서.txt` -> `sto`; a file at the inbox root -> `_root`. This
    is the folder-as-collection convention (the Obsidian/vector-namespace pattern):
    organise by moving files between folders, and scope a search to one folder,
    without splitting the single serving index.
    """
    normalised = (rel_path or "").replace("\\", "/").lstrip("/")
    head, sep, _ = normalised.partition("/")
    return head if sep and head else ROOT_COLLECTION


def build_index(paths: Paths | None = None, settings: Settings | None = None) -> dict:
    """Rebuild the retrieval tables from gold.chunks. Idempotent by construction."""
    paths = paths or get_paths()
    settings = settings or get_settings()
    paths.ensure()

    lake = open_lake(paths)
    try:
        rows = lake.execute(
            """
            SELECT chunk_id, doc_id, rel_path, title, doc_type, heading, chunk_index,
                   content, embed_text, char_start, char_end, token_estimate, content_sha256
            FROM lake.gold.chunks
            ORDER BY doc_id, chunk_index
            """
        ).fetchall()
    finally:
        lake.close()

    if not rows:
        raise RuntimeError(
            "lake.gold.chunks is empty. Run the ingest and transform steps first "
            "(make ingest && make transform), or check that data/inbox/documents has input."
        )

    # Collapse identical chunk content for the retrieval surfaces, PER COLLECTION.
    # The same boilerplate clause (예: 제1조(목적)) is copied verbatim into dozens of
    # DART filings; a RAG index must hold each distinct passage once, or N identical
    # hits fill every top-K and bury the distinctive articles. The key is
    # (collection, content), NOT content alone: a clause that also exists in another
    # collection must stay reachable when a search is scoped to THIS collection, or
    # folder-as-collection scoping silently loses content. `chunks` still holds
    # every row, so provenance and the gold<->index parity stay intact; only
    # chunks_vec and chunks_fts are collapsed, and to the SAME representative set,
    # so the two retrieval halves still agree. The representative is the first row
    # per key under the query's ORDER BY (doc_id, chunk_index), so the choice is
    # deterministic and the build stays idempotent.
    representatives = []
    seen_keys = set()
    for row in rows:
        key = (collection_of(row[2]), row[7])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        representatives.append(row)

    embedder = get_embedder(settings, allow_download=True)
    # `row[8]` is embed_text, not content: that distinction is what the cache
    # keys on (see pipeline/vector_cache.py). Dimensions come from the embedder
    # rather than from the first vector, because the cache needs them BEFORE
    # anything is encoded -- and because an empty representative set used to
    # IndexError here.
    dimensions = int(embedder.dimensions)
    embeddings, cache_stats = encode_with_cache(
        embedder, [row[8] for row in representatives], paths.vector_cache, dimensions
    )
    print(
        f"[index] vectors: {cache_stats['hits']} reused, {cache_stats['encoded']} encoded "
        f"(cache {cache_stats['cache']}, {cache_stats.get('pruned', 0)} pruned)",
        file=sys.stderr,
    )
    if embeddings and len(embeddings[0]) != dimensions:
        raise RuntimeError(
            f"embedder {embedder.name} declares {dimensions} dimensions but produced "
            f"{len(embeddings[0])}. The index signature would then lie about the vector width."
        )

    connection = connect_index(paths.index_sqlite)
    try:
        with connection:
            _create_schema(connection, dimensions)
            connection.executemany(
                """
                INSERT INTO chunks (chunk_id, doc_id, rel_path, collection, title, doc_type,
                                    heading, chunk_index, content, char_start, char_end,
                                    token_estimate, content_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row[0], row[1], row[2], collection_of(row[2]), row[3], row[4],
                        row[5], row[6], row[7], row[9], row[10], row[11], row[12],
                    )
                    for row in rows
                ],
            )
            connection.executemany(
                "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                [
                    (row[0], serialize_vector(vector))
                    for row, vector in zip(representatives, embeddings, strict=True)
                ],
            )
            # The FTS copy is bigram-expanded, so `chunks_fts.content` is NOT
            # human-readable for CJK. Read text from `chunks`, never from here.
            connection.executemany(
                "INSERT INTO chunks_fts (chunk_id, title, heading, content) VALUES (?, ?, ?, ?)",
                [
                    (row[0], expand_cjk(row[3] or ""), expand_cjk(row[5] or ""), expand_cjk(row[7]))
                    for row in representatives
                ],
            )
            connection.executemany(
                "INSERT INTO index_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [
                    ("embedding_provider", embedder.name),
                    ("embedding_dim", str(dimensions)),
                    ("chunk_count", str(len(rows))),
                    ("node_role", settings.node_role),
                    ("index_signature", index_signature(embedder, dimensions)),
                ],
            )
    finally:
        connection.close()

    return {
        "chunks": len(rows),
        "indexed": len(representatives),
        "dimensions": dimensions,
        "provider": embedder.name,
    }


# --------------------------------------------------------------------------
# Query: reciprocal rank fusion over sqlite-vec + FTS5
# --------------------------------------------------------------------------
def build_fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    CJK runs are expanded to the same character n-grams `expand_cjk` wrote into
    the index, then every term is quoted so punctuation in the user's question
    can never be read as FTS5 operator syntax. Build side and query side must
    stay in step: if they diverge the keyword half silently returns nothing.
    """
    terms: list = []
    for token in tokenize(text):
        if CJK_RUN.fullmatch(token):
            if len(token) <= FTS_NGRAM_SIZE:
                terms.append(token)
            else:
                terms.extend(
                    token[i : i + FTS_NGRAM_SIZE]
                    for i in range(len(token) - FTS_NGRAM_SIZE + 1)
                )
        else:
            terms.append(token)

    # Additive noun/root lemmas, symmetric with the write side (expand_cjk). A
    # lemma is a whole-noun token, so it OR-matches the bare-noun form the write
    # side indexed, bridging the josa (사업자의 -> 사업자). No-op unless KIWI_MORPH.
    terms.extend(kiwi_lemmas(text))

    if not terms:
        return ""
    unique = list(dict.fromkeys(terms))
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in unique)


def index_signature(embedder, dimensions: int) -> str:
    """Identity of everything that must agree between build time and query time.

    Carries the retrieval logic version alongside the provider, model and
    dimension. Changing the tokenizer or the n-gram widths without rebuilding
    leaves an index whose keyword half matches nothing; comparing this string is
    what turns that into a loud error instead of empty results.
    """
    if not hasattr(embedder, "model_name"):
        raise TypeError(
            f"embedder {type(embedder).__name__} does not declare `model_name`. "
            "Every provider must, or index_signature cannot tell two models of "
            "the same dimension apart and the staleness guard becomes decorative."
        )
    model = embedder.model_name or ""
    from pipeline.doctypes import profile_signature

    parts = (
        "v4",
        profile_signature(),
        embedder.name,
        model,
        str(dimensions),
        f"cjk{'-'.join(str(size) for size in CJK_NGRAM_SIZES)}",
        f"fts{FTS_NGRAM_SIZE}",
        FTS_TOKENIZE,
    )
    # Appended only when Kiwi is on, so a Kiwi-off index keeps its exact prior
    # signature (no spurious rebuild) while a Kiwi index can never be queried off.
    kiwi = _kiwi_signature()
    if kiwi:
        parts = parts + (kiwi,)
    # Same trick for the model precision: int8 is the historical asset and appends
    # nothing, so every index built before this existed still matches. fp16 is a
    # DIFFERENT VECTOR SPACE -- an fp16-built index queried by an int8 encoder
    # returns plausible nonsense rather than an error, which is exactly what this
    # slot turns into a loud failure. Note it records the asset, NOT the execution
    # provider: a GPU-built index must stay queryable on a CPU-only spoke.
    precision = getattr(embedder, "precision", "") or ""
    if precision and precision != "int8":
        parts = parts + (precision,)
    # And the encode batch, for the same reason as the precision: it is not a
    # speed knob. The tokenizer pads to the longest sequence in the batch, so
    # changing the batch changes the vectors (measured: cosine 0.9918 for one
    # passage alone vs batched beside a long one). Appended only when it differs
    # from the default, so no existing index is invalidated by this slot existing.
    batch = getattr(embedder, "_batch", runtime.ENCODE_BATCH_DEFAULT)
    if batch != runtime.ENCODE_BATCH_DEFAULT:
        parts = parts + (f"batch{batch}",)
    return "|".join(parts)


def assert_index_current(connection: sqlite3.Connection, embedder, dimensions: int) -> None:
    """Refuse to query an index built by different retrieval logic."""
    row = connection.execute(
        "SELECT value FROM index_meta WHERE key = 'index_signature'"
    ).fetchone()
    expected = index_signature(embedder, dimensions)
    actual = row[0] if row else None
    if actual != expected:
        raise ValueError(
            "data/serving/index.sqlite was built by different retrieval settings and "
            "would return wrong results.\n"
            f"  index was built with: {actual or '(no signature: pre-upgrade index)'}\n"
            f"  current settings are: {expected}\n"
            "Rebuild it with `make build`."
        )


def read_index_meta(paths: Paths | None = None, connection: sqlite3.Connection | None = None) -> dict:
    """The index's own build identity (index_meta rows), for a provenance footer.

    Read-only and cheap: lets a caller report WHICH index answered (signature,
    embedder, node role, sizes) without making the answer's correctness depend on
    it. Returns {} when there is no index yet.
    """
    owns = connection is None
    if connection is None:
        paths = paths or get_paths()
        if not paths.index_sqlite.exists():
            return {}
        connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        return {row[0]: row[1] for row in connection.execute("SELECT key, value FROM index_meta")}
    finally:
        if owns:
            connection.close()


HYBRID_SEARCH_SQL = """
WITH vector_hits AS (
    SELECT
        chunk_id,
        distance,
        ROW_NUMBER() OVER (ORDER BY distance) AS rank
    FROM chunks_vec
    WHERE embedding MATCH :query_vector
      AND k = :candidates
),
keyword_hits AS (
    SELECT
        chunk_id,
        ROW_NUMBER() OVER (ORDER BY bm25(chunks_fts)) AS rank
    FROM chunks_fts
    WHERE chunks_fts MATCH :query_text
    ORDER BY bm25(chunks_fts)
    LIMIT :candidates
),
candidate_ids AS (
    SELECT chunk_id FROM vector_hits
    UNION
    SELECT chunk_id FROM keyword_hits
),
fused AS (
    SELECT
        candidate_ids.chunk_id AS chunk_id,
        vector_hits.rank AS vector_rank,
        vector_hits.distance AS vector_distance,
        keyword_hits.rank AS keyword_rank,
        -- Reciprocal rank fusion: sum of 1/(k + rank) over the rankings a
        -- document appears in. Rank-based, so the two incomparable score
        -- scales (bm25, cosine distance) never have to be normalised.
        -- Weighted RRF. The two arms are not equally trustworthy here: the
        -- keyword half matches statute vocabulary exactly, while the vector half
        -- is a bag of character n-grams. Measured on 45 Korean queries, weighting
        -- the vector arm at 0.3 lifts MRR@10 from 0.7063 to 0.7251 and takes
        -- particle-glued queries from 0.9048 to 1.0000. Do NOT set it to 0:
        -- dropping the arm entirely breaks exact-identifier hit@1.
        COALESCE(:vector_weight / (:rrf_k + vector_hits.rank), 0.0)
            + COALESCE(:keyword_weight / (:rrf_k + keyword_hits.rank), 0.0) AS rrf_score
    FROM candidate_ids
    LEFT JOIN vector_hits ON vector_hits.chunk_id = candidate_ids.chunk_id
    LEFT JOIN keyword_hits ON keyword_hits.chunk_id = candidate_ids.chunk_id
)
SELECT
    chunks.chunk_id,
    chunks.doc_id,
    chunks.rel_path,
    chunks.collection,
    chunks.title,
    chunks.heading,
    chunks.doc_type,
    chunks.chunk_index,
    chunks.content,
    fused.vector_rank,
    -- Raw L2 distance from the vector arm, surfaced so a caller can see how far
    -- away a "hit" actually is. There is no threshold applied: sqlite-vec's
    -- `k = :candidates` returns exactly that many rows however distant they are,
    -- so a query about something absent from the corpus still fills the list.
    fused.vector_distance,
    fused.keyword_rank,
    fused.rrf_score
FROM fused
JOIN chunks ON chunks.chunk_id = fused.chunk_id
-- Collection scoping is applied AFTER fusion on the over-fetched candidate set
-- (the caller raises `candidates` when a collection is set), not inside the vec0
-- KNN, which has no collection column. NULL means every collection.
WHERE (:collection IS NULL OR chunks.collection = :collection)
  -- Smoke fixtures share the serving index (the smoke test needs them there) but
  -- must not surface in a real answer; hidden by default on the read path via a
  -- doc_id denylist -- no schema column, so no rebuild. :include_fixtures re-admits
  -- them for the fixture-dependent smoke assertions.
  AND (:include_fixtures OR chunks.doc_id NOT IN (SELECT value FROM json_each(:fixture_ids)))
ORDER BY fused.rrf_score DESC, chunks.chunk_id
LIMIT :limit
"""


@lru_cache(maxsize=1)
def _fixture_doc_ids() -> frozenset:
    """doc_ids of the smoke fixtures (pipeline/fixtures/). They share the serving
    index, so the read path hides them from a real answer by default. Derived
    locally via the same document_id() the build uses -- no schema column, so no
    rebuild, and it stays in sync if a fixture is added or renamed."""
    from pipeline.chunking import (  # lazy: avoid an import cycle
        SUPPORTED_SUFFIXES,
        document_id,
    )

    fixtures_dir = Path(__file__).parent / "fixtures"
    return frozenset(
        document_id(path.name)
        for path in fixtures_dir.glob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _fixture_ids_json() -> str:
    return json.dumps(sorted(_fixture_doc_ids()))


def hybrid_search(
    query: str,
    limit: int = 5,
    candidates: int = 40,
    connection: sqlite3.Connection | None = None,
    paths: Paths | None = None,
    settings: Settings | None = None,
    expand: bool | None = None,
    collection: str | None = None,
    include_fixtures: bool = False,
    rerank: bool | None = None,
) -> list:
    """Vector + keyword retrieval fused with RRF.

    When alias expansion is on, the query is first rewritten into its domain
    synonyms and every variant is retrieved separately, then the ranked lists are
    fused. That is what bridges 스테이블코인 to 자산연동형, which share no
    characters and so are invisible to both halves of the index.

    `rerank` overrides `settings.rerank_enabled`: None follows the setting, and an
    explicit True/False forces the cross-encoder rerank stage on/off (the eval
    harness needs a rerank-free `fused` arm alongside a `reranked` arm in the same
    RERANK=1 run, so it passes rerank=False/True explicitly).
    """
    settings = settings or get_settings()
    use_expansion = settings.alias_expansion if expand is None else expand
    variants = expand_query(query) if use_expansion else [query]
    use_rerank = settings.rerank_enabled if rerank is None else rerank

    if use_rerank:
        # Retrieve a deeper pool (retrieval order), then reorder it with the
        # cross-encoder. Kept out of build_rag: pipeline.reranker owns the model.
        from pipeline.reranker import rerank as _rerank

        pool = multi_hybrid_search(
            variants,
            limit=settings.rerank_candidates,
            candidates=max(candidates, settings.rerank_candidates),
            connection=connection,
            paths=paths,
            settings=settings,
            collection=collection,
            include_fixtures=include_fixtures,
        )
        return _rerank(query, variants, pool, settings, limit)

    return multi_hybrid_search(
        variants,
        limit=limit,
        candidates=candidates,
        connection=connection,
        paths=paths,
        settings=settings,
        collection=collection,
        include_fixtures=include_fixtures,
    )


def _search_once(
    query: str,
    limit: int,
    candidates: int,
    connection: sqlite3.Connection,
    settings: Settings,
    collection: str | None = None,
    include_fixtures: bool = False,
) -> list:
    """One retrieval: the weighted RRF of the vector and keyword arms."""
    embedder = get_embedder(settings)
    query_vector = embedder.encode([query])[0]
    assert_index_current(connection, embedder, len(query_vector))

    fts_query = build_fts_query(query)
    if not fts_query:
        # Nothing tokenizable: quote the raw query rather than handing FTS5
        # an empty MATCH, which is a syntax error.
        fts_query = '"' + query.replace('"', '""') + '"'

    # An all-zero query vector carries no information, but sqlite-vec ranks
    # by L2 distance, so it would return the least-populated chunks at
    # distance 0.0 and inject that noise into the fusion at the same weight
    # as the top keyword hit. `k = 0` returns no rows, which is the honest
    # answer: for this query there is no vector evidence.
    has_vector_signal = any(query_vector)

    rows = connection.execute(
        HYBRID_SEARCH_SQL,
        {
            "query_vector": serialize_vector(query_vector),
            "query_text": fts_query,
            "candidates": candidates if has_vector_signal else 0,
            "rrf_k": settings.rrf_k,
            "vector_weight": settings.vector_weight,
            "keyword_weight": settings.keyword_weight,
            "collection": collection,
            "include_fixtures": 1 if include_fixtures else 0,
            "fixture_ids": _fixture_ids_json(),
            "limit": limit,
        },
    ).fetchall()
    return [dict(row) for row in rows]


# When a collection filter is set, the KNN over-fetches globally and the filter
# is applied afterwards, so the candidate depth is raised to keep enough
# in-collection rows in the window. Ample for this project's scale; the correct
# scale-up is a collection column on the vec0/fts tables for pre-filtering.
COLLECTION_CANDIDATES = 300


def multi_hybrid_search(
    queries: Sequence[str],
    limit: int = 5,
    candidates: int = 40,
    connection: sqlite3.Connection | None = None,
    paths: Paths | None = None,
    settings: Settings | None = None,
    collection: str | None = None,
    include_fixtures: bool = False,
) -> list:
    """Retrieve each query variant separately, then fuse the ranked lists.

    Fusing SEPARATE retrievals beats OR-ing every alias into one FTS query
    (measured nDCG@10 0.7455 against 0.5953): an OR dilutes the one
    discriminative term among generic ones, whereas a variant that matches
    nothing simply contributes an empty list.

    The fused score is divided by the number of variants. It is a per-query
    constant so it cannot reorder anything, but without it a multi-variant query
    scores far higher than a single-variant one purely because more arms
    contributed, which would make any future relevance threshold unsettable.
    """
    if not queries:
        raise ValueError("multi_hybrid_search needs at least one query")

    paths = paths or get_paths()
    settings = settings or get_settings()

    owns_connection = connection is None
    if connection is None:
        if not paths.index_sqlite.exists():
            raise FileNotFoundError(
                f"{paths.index_sqlite} does not exist. Build it with `make build`."
            )
        connection = connect_index(paths.index_sqlite, read_only=True)

    depth = max(candidates, COLLECTION_CANDIDATES) if collection else candidates
    try:
        fused: dict = {}
        best: dict = {}
        for variant in queries:
            rows = _search_once(variant, depth, depth, connection, settings, collection=collection, include_fixtures=include_fixtures)
            for rank, row in enumerate(rows, start=1):
                chunk_id = row["chunk_id"]
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (settings.rrf_k + rank)
                # Keep the row from the variant that ranked it best, so the
                # reported per-arm ranks describe a real retrieval.
                if chunk_id not in best:
                    best[chunk_id] = row

        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:limit]
        results = []
        for chunk_id, score in ordered:
            row = dict(best[chunk_id])
            row["rrf_score"] = score / len(queries)
            row["query_variants"] = len(queries)
            results.append(row)
        return results
    finally:
        if owns_connection:
            connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build or query the RAG half of the index.")
    parser.add_argument("--query", help="Run a hybrid search instead of rebuilding.")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    if args.query:
        for hit in hybrid_search(args.query, limit=args.limit):
            print(
                f"{hit['rrf_score']:.5f}  vec={hit['vector_rank']}  fts={hit['keyword_rank']}  "
                f"{hit['rel_path']} :: {hit['heading']}"
            )
        return 0

    summary = build_index()
    print(
        "[build_rag] indexed {chunks} chunk(s), {dimensions}-dim, provider={provider}".format(
            **summary
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
