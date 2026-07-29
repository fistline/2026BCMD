"""Shared paths and settings for the local-first data platform.

Everything that resolves a filesystem location lives here so the control plane
(this package) and the data plane (`data/`) never disagree about where bytes go.

Nothing in this module reads the network. `.env` is loaded from the project root
if present; it is git-ignored and holds the only machine-specific values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Paths:
    """Every path the platform touches, rooted at the data plane."""

    root: Path
    data: Path
    inbox: Path
    raw: Path
    raw_documents: Path
    raw_revisions: Path
    raw_manifest: Path
    processed: Path
    serving: Path
    landing_db: Path
    ducklake_catalog: Path
    ducklake_data: Path
    sqlmesh_state: Path
    index_sqlite: Path
    # Build cache, not a serving artefact: `make sync` ships data/serving to the
    # spokes and a spoke has no use for it. Removed by `make clean` (which wipes
    # data/processed) but NOT by `make clean-index`, so dropping the index to
    # rebuild it stays cheap.
    vector_cache: Path
    fixtures: Path
    source: Path

    def ensure(self) -> Paths:
        """Create the data-plane directories. Safe to call repeatedly."""
        for directory in (
            self.data,
            self.inbox,
            self.raw,
            self.raw_documents,
            self.raw_revisions,
            self.processed,
            self.serving,
            self.ducklake_data,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> Paths:
    """Resolve the data-plane layout, honouring PLATFORM_DATA_DIR."""
    data_dir = Path(os.environ.get("PLATFORM_DATA_DIR", "data"))
    data = data_dir if data_dir.is_absolute() else PROJECT_ROOT / data_dir
    processed = data / "processed"
    raw = data / "raw"
    return Paths(
        root=PROJECT_ROOT,
        data=data,
        inbox=data / "inbox" / "documents",
        raw=raw,
        raw_documents=raw / "documents",
        raw_revisions=raw / "_revisions",
        raw_manifest=raw / "_manifest.jsonl",
        processed=processed,
        serving=data / "serving",
        landing_db=processed / "landing.duckdb",
        ducklake_catalog=processed / "catalog.ducklake",
        ducklake_data=processed / "ducklake",
        sqlmesh_state=processed / "sqlmesh_state.duckdb",
        index_sqlite=data / "serving" / "index.sqlite",
        vector_cache=processed / "vector_cache.sqlite",
        fixtures=PROJECT_ROOT / "pipeline" / "fixtures",
        source=PROJECT_ROOT / "source",
    )


@dataclass(frozen=True)
class Settings:
    """Node identity and knobs. Spoke and hub differ only through these."""

    node_role: str
    sqlmesh_gateway: str
    data_remote: str
    embedding_provider: str
    embedding_dim: int
    embedding_model: str
    # Which ONNX asset the fleet uses: int8 (CPU format, the default and the
    # historical behaviour) or fp16 (what a GPU execution provider wants). A
    # FLEET-wide setting, not a per-node one -- see resolve_precision().
    embedding_precision: str
    rrf_k: int
    vector_weight: float
    keyword_weight: float
    alias_expansion: bool
    # How many chunks ONE (doc_id, heading) may occupy in a fused answer.
    section_cap: int
    graph_depth: int
    # Optional cross-encoder reranker (RERANK=1). Default OFF: the baseline
    # (no reranker) is the M1-8GB target; this is the heavier opt-in path.
    rerank_enabled: bool
    rerank_model: str
    rerank_candidates: int
    rerank_weight: float
    rerank_threads: int
    # int8 (default, CPU) | fp16 (GPU) | auto. Unlike the embedder's precision
    # this is machine-local: no reranker output is persisted or synced.
    rerank_precision: str


def get_settings() -> Settings:
    node_role = os.environ.get("PLATFORM_NODE_ROLE", "spoke").strip().lower()
    if node_role not in {"spoke", "hub"}:
        raise ValueError(f"PLATFORM_NODE_ROLE must be 'spoke' or 'hub', got {node_role!r}")
    return Settings(
        node_role=node_role,
        sqlmesh_gateway=os.environ.get("SQLMESH_GATEWAY", node_role),
        data_remote=os.environ.get("DATA_REMOTE", "local_only"),
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "hashing").strip().lower(),
        # 1024, not 256: Korean character n-grams produce a far larger feature
        # vocabulary than English words, and hash collisions at 256 destroy the
        # signal. Measured knee of the curve on Korean legal retrieval.
        embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
        embedding_model=os.environ.get(
            "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
        ),
        embedding_precision=_embedding_precision(),
        rrf_k=int(os.environ.get("RRF_K", "60")),
        # 1.0, not the 0.3 this shipped with. That 0.3 was measured under the
        # HASHING embedder on a 45-query judgment set that no longer exists, and it
        # survived the move to onnx_int8 unexamined. Measured now: 0.3 → fused
        # MRR@10 0.750, 1.0 → 0.801 [M:arm-weighting]. Every recorded floor was
        # taken at 1.0 through a git-IGNORED .env while this said 0.3.
        #
        # Who that bit, precisely: NOT a bare clone -- with no .env the provider
        # falls back to `hashing` and index_signature refuses the index loudly,
        # which is that guard working. It bit the node that sets the embedder
        # correctly and inherits the fusion weight, which is what a spoke does.
        # That node missed the floor by 0.051 fused MRR@10 with nothing to say so:
        # index_signature, chunk_count and judgment_sha are all blind here, because
        # a fusion weight moves no vector and touches no judgment.
        vector_weight=float(os.environ.get("VECTOR_WEIGHT", "1.0")),
        keyword_weight=float(os.environ.get("KEYWORD_WEIGHT", "1.0")),
        alias_expansion=os.environ.get("ALIAS_EXPANSION", "1").strip() not in {"0", "false", "no"},
        # One long article windows into several chunks and every one of them
        # matches, so without a cap a single 조문 fills the answer with itself:
        # measured 29 of 140 top-10 slots on the eval set, 6 of 10 on one query.
        # Query-time only -- it moves no vector, so it is not in index_signature.
        section_cap=_section_cap(),
        graph_depth=_graph_depth(),
        # Reranker: opt-in, default off. Candidates 16, because that is the number
        # that fits ONE comparable batch. This model is dynamically quantised, so
        # the int8 scale is computed over the batch: the old default of 20 against
        # a batch cap of 16 scored candidates 1-16 and 17-20 under DIFFERENT scales
        # and then sorted them into one order [M:rerank-batch]. Measured, 16 in one
        # batch reproduces the recorded floor exactly, per-kind. The count is
        # therefore not a pure recall knob any more -- it moves every score.
        # Single-thread keeps the ranking deterministic.
        rerank_enabled=os.environ.get("RERANK", "0").strip() not in {"", "0", "false", "no"},
        rerank_model=os.environ.get("RERANK_MODEL", "onnx-community/bge-reranker-v2-m3-ONNX"),
        rerank_candidates=int(os.environ.get("RERANK_CANDIDATES", "16")),
        # Retrieval PRIOR weight in the rerank fusion (CE weight is fixed at 1.0):
        # lower = more cross-encoder-dominant. It was 0.15, which made the CE the
        # PRIMARY signal, and that stopped paying the moment SECTION_CAP lifted the
        # fused arm: at 0.15 the reranked arm scored BELOW plain fusion.
        #
        # 3.0 is the measured peak of a swept curve, not the edge of one
        # [M:rerank-weight]: MRR@10 climbs 0.758 -> 0.926 from 0.15 to 3.0, sits flat
        # at 4.0 (0.925), and by 8.0 has converged to the fused number exactly, which
        # is what `w/(k + retr_rank) + 1/(k + ce_rank)` must do as w grows. So the
        # cross-encoder earns its keep as a TIE-BREAKER and loses it as a primary
        # ranker. Re-sweep with `make eval-rerank` after anything that moves fusion.
        rerank_weight=float(os.environ.get("RERANK_WEIGHT", "3.0")),
        rerank_threads=int(os.environ.get("RERANK_THREADS", "1")),
        rerank_precision=_precision("RERANK_PRECISION"),
    )


def _precision(variable: str) -> str:
    """int8 (default) | fp16 | auto, for EMBEDDING_PRECISION and RERANK_PRECISION.

    Validated on read, like GRAPH_DEPTH, so a typo fails on the first settings
    load rather than at model-construction time deep inside a build.
    """
    value = os.environ.get(variable, "int8").strip().lower()
    if value not in {"int8", "fp16", "auto"}:
        raise ValueError(f"{variable} must be int8, fp16 or auto; got {value!r}")
    return value


def _embedding_precision() -> str:
    return _precision("EMBEDDING_PRECISION")


def _section_cap() -> int:
    """SECTION_CAP: chunks one (doc_id, heading) may occupy in a fused answer.

    Validated on read like GRAPH_DEPTH. 0 is rejected rather than treated as
    "unlimited": a cap of zero would return nothing, and a knob whose off switch
    silently empties the result is worse than no knob. Set it high to disable.
    """
    raw = os.environ.get("SECTION_CAP", "1").strip()
    try:
        cap = int(raw)
    except ValueError as error:
        raise ValueError(f"SECTION_CAP must be a positive integer, got {raw!r}") from error
    if cap < 1:
        raise ValueError(f"SECTION_CAP must be at least 1, got {cap}")
    return cap


def _graph_depth() -> int:
    """GRAPH_DEPTH for graph_rag: 0 disables expansion, 2 is the ceiling.

    Validated here rather than at query time so a typo'd .env fails on the first
    read, not on the first query that happens to touch the graph arm.
    """
    raw = os.environ.get("GRAPH_DEPTH", "1").strip()
    try:
        depth = int(raw)
    except ValueError as error:
        raise ValueError(f"GRAPH_DEPTH must be an integer 0-2, got {raw!r}") from error
    if not 0 <= depth <= 2:
        raise ValueError(f"GRAPH_DEPTH must be between 0 and 2, got {depth}")
    return depth


__all__ = ["PROJECT_ROOT", "Paths", "Settings", "get_paths", "get_settings"]
