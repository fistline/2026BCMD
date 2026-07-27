"""Optional cross-encoder reranker (bge-reranker-v2-m3 ONNX int8), query-time.

Opt-in (`RERANK=1`), default OFF — the baseline (no reranker) is the M1-8GB
target; this is the heavier opt-in path (a second ~544 MB model resident).

It reorders the fused candidate pool by a cross-encoder relevance score, then
fuses that BACK with the retrieval rank via RRF, so a candidate the retriever
surfaced (e.g. only via an alias variant) is never fully demoted by a reranker
that does not know the domain synonym. Two safety properties the design review
demanded:

  * Alias preservation via VARIANT MAX-POOL — each candidate is scored against
    every query variant (original + alias expansions) and keeps its MAX score,
    so a chunk surfaced only by the 자산연동형 variant is scored high by THAT
    variant and stays near the top. (A cross-encoder fed only the original query
    would score 스테이블코인↔자산연동형 low and demote it — the exact
    `synonym_gap` regression this max-pool prevents.)
  * Determinism — single-thread inference by default (threaded matmul reduction
    is non-associative) and logit rounding before ranking, so the query-output
    order is stable run-to-run. `RERANK_THREADS` trades this for speed.

Offline like the embedder: the model is fetched once at warm/build time; the
read path RAISES rather than downloading (invariant 7). No new dependency —
reuses onnxruntime / tokenizers / huggingface_hub.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from functools import cache

from pipeline import runtime

RERANK_DEFAULT_MODEL = "onnx-community/bge-reranker-v2-m3-ONNX"
# Same asset rule as the embedder: int8 is the CPU format, fp16 is what a GPU
# execution provider wants. Unlike the embedder this precision is NOT a fleet
# decision -- no reranker output is persisted, so it never has to agree with
# anything on another machine. It still defaults to int8 so the shipped
# behaviour, and `eval-rerank`'s recorded floor, do not move on their own.
_ASSETS = {
    "int8": "onnx/model_int8.onnx",
    "fp16": "onnx/model_fp16.onnx",
}
_MAX_TOKENS = 512
_BATCH = 16   # mirror the embedder's cap so per-call batch memory stays bounded (8GB)
_ROUND = 4    # round CE logits before ranking -> ties break on chunk_id -> deterministic


class OnnxReranker:
    name = "bge-reranker-v2-m3-onnx-int8"

    def __init__(
        self,
        model_name: str,
        allow_download: bool = False,
        threads: int = 1,
        precision: str = "int8",
    ):
        try:
            import numpy as np
            from huggingface_hub import hf_hub_download
            from huggingface_hub.errors import LocalEntryNotFoundError
            from tokenizers import Tokenizer

            if importlib.util.find_spec("onnxruntime") is None:
                raise ImportError("onnxruntime")
        except ImportError as error:
            raise RuntimeError(
                "RERANK=1 needs the onnx-embed extra (onnxruntime, tokenizers, "
                "huggingface_hub). Install it with: uv sync --extra onnx-embed"
            ) from error

        self._np = np
        self.model_name = model_name
        if precision not in _ASSETS:
            raise ValueError(f"RERANK_PRECISION must be one of {sorted(_ASSETS)}, got {precision!r}")
        self.precision = precision

        def _fetch(filename: str) -> str:
            # Same offline contract as OnnxInt8Embedder: local cache first on both
            # paths; the read path (allow_download=False) raises on a miss instead
            # of touching the network. A non-miss error surfaces as itself.
            try:
                return hf_hub_download(model_name, filename, local_files_only=True)
            except LocalEntryNotFoundError:
                if not allow_download:
                    raise RuntimeError(
                        f"reranker model {model_name!r} is not in the local Hugging "
                        f"Face cache. It is fetched once by `make warm-rerank` (or a "
                        f"build with RERANK=1); the read path never downloads "
                        f"(invariant 7). Warm it online once, then retry."
                    ) from None
                return hf_hub_download(model_name, filename)

        self._tokenizer = Tokenizer.from_file(_fetch("tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        self._tokenizer.enable_padding()

        # Single-thread by default: a threaded matmul's accumulation order is not
        # fixed, so pinning threads keeps the logits -- and thus the ranking --
        # stable run-to-run, the same reason the embedder pins them.
        # runtime.detect() keeps this on the CPU unless the fp16 asset is asked
        # for, so `RERANK=1` alone never silently moves onto a GPU.
        self.profile = runtime.detect(precision)
        self._session = runtime.make_session(_fetch(_ASSETS[precision]), self.profile, threads=threads)
        self.provider = self._session.get_providers()[0]
        self._input_names = {node.name for node in self._session.get_inputs()}

    def _score_batch(self, pairs) -> list:
        np = self._np
        encoded = self._tokenizer.encode_batch(list(pairs))
        feeds = {
            "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encoded], dtype=np.int64),
        }
        feeds = {name: value for name, value in feeds.items() if name in self._input_names}
        logits = self._session.run(None, feeds)[0]
        return [float(x) for x in logits.reshape(-1)]

    def score(self, pairs: Sequence) -> list:
        """Cross-encoder logit per (query, doc) pair, sub-batched for memory."""
        pairs = list(pairs)
        out: list = []
        for start in range(0, len(pairs), _BATCH):
            out.extend(self._score_batch(pairs[start : start + _BATCH]))
        return out


@cache
def _load_reranker(model: str, allow_download: bool, threads: int, precision: str):
    """One reranker per (model, allow_download, threads, precision) for the process."""
    return OnnxReranker(
        model, allow_download=allow_download, threads=threads, precision=precision
    )


def resolve_precision(settings) -> str:
    """`auto` means "fp16 if this box has an execution provider that wants it".

    Safe to resolve per node here, unlike the embedder's precision: a reranker
    score is computed and discarded within one query.
    """
    requested = (settings.rerank_precision or "int8").strip().lower()
    if requested != "auto":
        return requested
    return runtime.precision_for(runtime.detect("fp16").provider)


def get_reranker(settings, allow_download: bool = False):
    return _load_reranker(
        settings.rerank_model,
        allow_download,
        settings.rerank_threads,
        resolve_precision(settings),
    )


def rerank(query, variants, rows, settings, limit, allow_download: bool = False) -> list:
    """Reorder the fused candidate `rows` (retrieval order) with the cross-encoder,
    fused back with the retrieval rank via RRF. Returns the top `limit`, each row
    annotated with `ce_score`, `ce_rank`, `retrieval_rank`, `rerank_score`,
    `reranked`.

    `final = w_retr/(k + retr_rank) + 1/(k + ce_rank)` — the cross-encoder term is
    the DOMINANT signal (that is the point of reranking); the retrieval rank is a
    small PRIOR (`rerank_weight`, default 0.15) that only breaks near-ties in the
    retriever's favour. An equal-weight RRF would be symmetric and flatten the CE
    signal to a near-no-op whenever CE and retrieval disagree — which is exactly
    the case reranking exists to resolve. Alias-surfaced chunks are protected not
    by the retrieval prior but by the variant MAX-POOL: they earn a high CE score
    from the variant that surfaced them, so a CE-dominant order keeps them high.
    """
    rows = list(rows)
    if not rows:
        return rows
    reranker = get_reranker(settings, allow_download=allow_download)

    contents = [row["content"] for row in rows]
    n = len(rows)
    best = [float("-inf")] * n
    for variant in variants:
        scores = reranker.score([(variant, content) for content in contents])
        for i, score in enumerate(scores):
            if score > best[i]:
                best[i] = score
    ce = [round(score, _ROUND) for score in best]

    # ce_rank: 1-based over (ce desc, chunk_id) so equal-ish logits order stably.
    order = sorted(range(n), key=lambda i: (-ce[i], rows[i]["chunk_id"]))
    ce_rank = [0] * n
    for rank, i in enumerate(order, start=1):
        ce_rank[i] = rank

    k = settings.rrf_k
    w_retr = settings.rerank_weight  # retrieval PRIOR weight; CE weight is fixed at 1.0
    fused: list = []
    for i, row in enumerate(rows):
        retrieval_rank = i + 1  # position in the fused pool
        final = w_retr / (k + retrieval_rank) + 1.0 / (k + ce_rank[i])
        out = dict(row)
        out["ce_score"] = ce[i]
        out["ce_rank"] = ce_rank[i]
        out["retrieval_rank"] = retrieval_rank
        out["rerank_score"] = final
        out["reranked"] = True
        fused.append(out)

    fused.sort(key=lambda row: (-row["rerank_score"], row["chunk_id"]))
    return fused[:limit]
