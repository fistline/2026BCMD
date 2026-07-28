"""Retrieval evaluation: turn the next tuning question into a measurement.

Every retrieval number this project has claimed — the vector arm at 0.3, the
alias table, the rejection of morphological tokenization — came from eval sets
built outside the repo and thrown away. That made each one unreproducible and
the next change unmeasurable. This is the smallest thing that fixes that.

It is a REGRESSION FLOOR, not a benchmark. Fifteen judgments over one Korean
bill corpus say nothing in absolute terms; what they do is tell you whether the
change you just made helped or quietly broke a category.

Three arms are reported separately, because the fused number hides the trade:
a change can lift the keyword arm and lose more in fusion. Morphological
tokenization did exactly that under the OLD regime (hashing embedder, vector_weight
0.3): +0.078 keyword R@10 but -0.022 fused MRR. Re-measured on the current
onnx_int8 / vector_weight 1.0 regime, ADDITIVE Kiwi noun lemmas (KIWI_MORPH=1,
`uv sync --extra kiwi`; lemmas appended beside the char-bigram backbone, never
replacing it) instead lift BOTH — keyword +0.039, fused 0.654->0.692, cross_bill
0.833->1.000, synonym_gap +0.056 — while holding vocabulary_match at 1.000. So it
ships as an opt-in (default stays model-free), NOT a rejection. Caveat: it is a
general keyword-arm gain, not a particle_glue fix — that kind regressed -0.056
(common lemmas like 신고/자산 broaden the match), so per-kind still rules.

    make eval                 report
    make eval-baseline        record the current numbers as the floor
    uv run python -m pipeline.eval_retrieval --assert-baseline

Skips cleanly when the judgments do not match the indexed corpus, so a fresh
clone with only the bundled fixtures still has a green build.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pipeline import Paths, Settings, get_paths, get_settings
from pipeline.build_rag import (
    build_fts_query,
    connect_index,
    get_embedder,
    hybrid_search,
    resolve_precision,
    serialize_vector,
)

QUERY_FILE = Path(__file__).with_name("eval_queries.json")
BASELINE_FILE = Path(__file__).with_name("eval_baseline.json")
# The opt-in reranker floor lives in a SEPARATE file so `make eval-baseline`
# (rerank off) and `make eval-rerank-baseline` (RERANK=1) never overwrite each
# other. Selected by the run's rerank_enabled, so the default gate is untouched.
BASELINE_RERANK_FILE = Path(__file__).with_name("eval_rerank_baseline.json")


def _baseline_file(result: dict) -> Path:
    """The floor this run must clear: one file per (rerank arm, embedding asset).

    The asset axis is the same argument as the rerank axis. int8 and fp16 are
    different vector spaces, so their numbers are not comparable and must not
    overwrite each other -- and a fleet evaluating fp16 needs its own recorded
    floor before it can gate on it. int8 (the default) keeps the historical
    filenames, so nothing that exists today is renamed or re-recorded.
    """
    base = BASELINE_RERANK_FILE if result.get("settings", {}).get("rerank_enabled") else BASELINE_FILE
    precision = (result.get("settings", {}) or {}).get("embedding_precision", "int8")
    if precision in ("", "int8"):
        return base
    return base.with_name(f"{base.stem}.{precision}{base.suffix}")
DEPTH = 10
# Below this share of judgments resolvable against the index, the corpus is not
# the one these judgments describe and the numbers would be noise.
MIN_RESOLVABLE = 0.6


def load_queries(path: Path | None = None) -> list:
    payload = json.loads((path or QUERY_FILE).read_text(encoding="utf-8"))
    return payload["queries"]


def _matching_chunk_ids(connection, patterns: list) -> set:
    """Resolve heading regexes to chunk ids against the CURRENT index.

    Heading-anchored on purpose: chunk ids are positional, so any change to
    sectioning re-points them while every id still resolves.
    """
    if not patterns:
        return set()
    rows = connection.execute("SELECT chunk_id, heading FROM chunks").fetchall()
    compiled = [re.compile(re.escape(pattern)) for pattern in patterns]
    return {
        row["chunk_id"]
        for row in rows
        if row["heading"] and any(pattern.search(row["heading"]) for pattern in compiled)
    }


def _fixture_chunk_ids(connection) -> set:
    """Chunk ids of smoke fixtures, hidden from the served read path.

    The single arms query chunks_vec/chunks_fts directly, with no doc_id column
    and no fixture predicate; without this they credit fixture chunks the fused
    product path (build_rag HYBRID_SEARCH_SQL) hides, inflating the arm metrics
    against a retriever users never get.
    """
    from pipeline.build_rag import _fixture_doc_ids

    fixture_docs = _fixture_doc_ids()
    if not fixture_docs:
        return set()
    rows = connection.execute("SELECT chunk_id, doc_id FROM chunks").fetchall()
    return {row["chunk_id"] for row in rows if row["doc_id"] in fixture_docs}


def _vector_only(query: str, connection, settings: Settings, depth: int) -> list:
    embedder = get_embedder(settings)
    vector = embedder.encode([query])[0]
    if not any(vector):
        return []
    fixtures = _fixture_chunk_ids(connection)
    rows = connection.execute(
        "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (serialize_vector(vector), depth + len(fixtures)),  # over-fetch so DEPTH real rows survive
    ).fetchall()
    return [row["chunk_id"] for row in rows if row["chunk_id"] not in fixtures][:depth]


def _keyword_only(query: str, connection, depth: int) -> list:
    match = build_fts_query(query)
    if not match:
        return []
    fixtures = _fixture_chunk_ids(connection)
    rows = connection.execute(
        "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
        (match, depth + len(fixtures)),  # over-fetch so DEPTH real rows survive
    ).fetchall()
    return [row["chunk_id"] for row in rows if row["chunk_id"] not in fixtures][:depth]


def _score(ranked: list, relevant: set, depth: int = DEPTH) -> dict:
    """Hit-rate style metrics. Recall@k is 'at least one relevant in top k'.

    Chosen over |retrieved ∩ relevant| / |relevant| because several judgments
    have many relevant chunks across bills, under which Recall@1 would be capped
    by the label count and would measure the judgments rather than the retriever.
    """
    hits = [position for position, chunk_id in enumerate(ranked[:depth]) if chunk_id in relevant]
    first = hits[0] if hits else None
    return {
        "p_at_1": 1.0 if (ranked[:1] and ranked[0] in relevant) else 0.0,
        "r_at_5": 1.0 if any(position < 5 for position in hits) else 0.0,
        "r_at_10": 1.0 if hits else 0.0,
        "mrr_at_10": (1.0 / (first + 1)) if first is not None else 0.0,
    }


def _mean(values: list) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _source_sha() -> str:
    """The commit this eval ran against, so a recorded number is bound to a SHA.

    Empty string outside a git repo -- the eval must still run on a fresh clone.
    Stored in the result (and thus in eval_baseline.json), which is tracked, so git
    history becomes the append-only telemetry log without a second file to rot.
    """
    import subprocess

    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:  # noqa: BLE001 -- not a repo / no git: telemetry is best-effort
        return ""


def evaluate(paths: Paths | None = None, settings: Settings | None = None) -> dict | None:
    """Run all three arms. Returns None when the corpus does not match."""
    paths = paths or get_paths()
    settings = settings or get_settings()
    if not paths.index_sqlite.exists():
        print("[eval] no index; run `make build` first.", file=sys.stderr)
        return None

    queries = load_queries()
    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        graded = []
        unresolved = []
        for query in queries:
            relevant = _matching_chunk_ids(connection, query.get("relevant", []))
            if query.get("relevant") and not relevant:
                unresolved.append(query["id"])
                continue
            graded.append((query, relevant))

        answerable = [item for item in graded if item[1]]
        expected = [query for query in queries if query.get("relevant")]
        if expected and len(answerable) < MIN_RESOLVABLE * len(expected):
            print(
                f"[eval] SKIPPED: only {len(answerable)}/{len(expected)} judgments resolve "
                "against this index. These judgments describe the Korean bill corpus; "
                "load it, or write judgments for the corpus you have.",
                file=sys.stderr,
            )
            return None

        rerank_on = settings.rerank_enabled
        arms = {"vector": {}, "keyword": {}, "fused": {}}
        if rerank_on:
            arms["reranked"] = {}
        by_kind: dict = {}
        by_kind_reranked: dict = {}
        per_query: list = []
        for query, relevant in graded:
            kind = query.get("kind", "unlabelled")
            ranked = {
                "vector": _vector_only(query["query"], connection, settings, DEPTH),
                "keyword": _keyword_only(query["query"], connection, DEPTH),
                # rerank=False forces a rerank-FREE fused baseline even inside a
                # RERANK=1 run, so the reranked arm has a clean control to attribute
                # the cross-encoder's contribution against (not reranked-vs-reranked).
                "fused": [
                    hit["chunk_id"]
                    for hit in hybrid_search(
                        query["query"], limit=DEPTH, connection=connection,
                        settings=settings, rerank=False,
                    )
                ],
            }
            if rerank_on:
                ranked["reranked"] = [
                    hit["chunk_id"]
                    for hit in hybrid_search(
                        query["query"], limit=DEPTH, connection=connection,
                        settings=settings, rerank=True,
                    )
                ]
            if kind == "negative":
                # Nothing to score positively; record how many arms returned a
                # top hit at all, which is what a future abstention rule needs.
                by_kind.setdefault(kind, {"n": 0, "returned_rank1": 0})
                by_kind[kind]["n"] += 1
                returned = int(bool(ranked["fused"]))
                by_kind[kind]["returned_rank1"] += returned
                per_query.append({"id": query["id"], "kind": kind, "returned_rank1": returned})
                continue

            for arm, ids in ranked.items():
                scored = _score(ids, relevant)
                for metric, value in scored.items():
                    arms[arm].setdefault(metric, []).append(value)
                if arm == "fused":
                    bucket = by_kind.setdefault(kind, {})
                    for metric, value in scored.items():
                        bucket.setdefault(metric, []).append(value)
                    per_query.append({
                        "id": query["id"],
                        "kind": kind,
                        "p_at_1": scored["p_at_1"],
                        "mrr_at_10": scored["mrr_at_10"],
                    })
                elif arm == "reranked":
                    # Separate bucket: the fused by_kind (and its baseline/gate)
                    # is left untouched, so the default rerank-off path is
                    # unaffected; the reranked floor is measured on its own.
                    rbucket = by_kind_reranked.setdefault(kind, {})
                    for metric, value in scored.items():
                        rbucket.setdefault(metric, []).append(value)
    finally:
        connection.close()

    result = {
        "arms": {
            arm: {metric: _mean(values) for metric, values in metrics.items()}
            for arm, metrics in arms.items()
        },
        "by_kind": {
            kind: (
                bucket
                if "n" in bucket
                else {metric: _mean(values) for metric, values in bucket.items()}
            )
            for kind, bucket in by_kind.items()
        },
        "graded": len(graded),
        "chunk_count": chunk_count,
        "source_sha": _source_sha(),
        "per_query": per_query,
        "unresolved": unresolved,
        "settings": {
            "vector_weight": settings.vector_weight,
            "keyword_weight": settings.keyword_weight,
            "rrf_k": settings.rrf_k,
            "alias_expansion": settings.alias_expansion,
            "embedding_provider": settings.embedding_provider,
            # Which asset produced these vectors. Selects the baseline file, and
            # makes a recorded floor self-describing rather than implicitly int8.
            "embedding_precision": resolve_precision(settings),
            "rerank_enabled": settings.rerank_enabled,
            "rerank_model": settings.rerank_model if settings.rerank_enabled else None,
            "rerank_weight": settings.rerank_weight if settings.rerank_enabled else None,
        },
    }
    # Only present in a RERANK=1 run: keeps the default baseline shape unchanged.
    if by_kind_reranked:
        result["by_kind_reranked"] = {
            kind: {metric: _mean(values) for metric, values in bucket.items()}
            for kind, bucket in by_kind_reranked.items()
        }
    return result


def render(result: dict) -> None:
    print(f"graded {result['graded']} queries  {result['settings']}")
    if result["unresolved"]:
        print(f"  unresolved judgments: {', '.join(result['unresolved'])}")
    print()
    print(f"  {'arm':10} {'P@1':>7} {'R@5':>7} {'R@10':>7} {'MRR@10':>8}")
    for arm, metrics in result["arms"].items():
        print(
            f"  {arm:10} {metrics.get('p_at_1', 0):7.3f} {metrics.get('r_at_5', 0):7.3f} "
            f"{metrics.get('r_at_10', 0):7.3f} {metrics.get('mrr_at_10', 0):8.3f}"
        )
    print()
    print("  fused, by kind:")
    for kind, metrics in sorted(result["by_kind"].items()):
        if "n" in metrics:
            print(f"    {kind:18} {metrics['returned_rank1']}/{metrics['n']} returned a top hit")
            continue
        print(
            f"    {kind:18} P@1 {metrics.get('p_at_1', 0):.3f}  "
            f"R@5 {metrics.get('r_at_5', 0):.3f}  MRR@10 {metrics.get('mrr_at_10', 0):.3f}"
        )
    if result.get("by_kind_reranked"):
        print()
        print("  reranked, by kind (vs fused above = cross-encoder's contribution):")
        for kind, metrics in sorted(result["by_kind_reranked"].items()):
            print(
                f"    {kind:18} P@1 {metrics.get('p_at_1', 0):.3f}  "
                f"R@5 {metrics.get('r_at_5', 0):.3f}  MRR@10 {metrics.get('mrr_at_10', 0):.3f}"
            )


def build_kind(paths: Paths | None = None) -> str:
    """How the index under evaluation was built: canonical | incremental | unknown.

    A canonical index encoded every vector in one build. An incremental one reused
    cached vectors that were encoded beside different neighbours, and this graph is
    dynamically quantised, so those vectors differ -- measured, cosine 0.9904, which
    is enough to move R@10 by 0.15. A floor recorded from an incremental index
    therefore records a build history, and the next canonical build "regresses"
    against it for no reason anyone can see.
    """
    paths = paths or get_paths()
    if not paths.index_sqlite.exists():
        return "unknown"
    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        row = connection.execute(
            "SELECT value FROM index_meta WHERE key = 'build_kind'"
        ).fetchone()
    finally:
        connection.close()
    return row[0] if row else "unknown"


def compare(
    result: dict,
    tolerance: float = 0.02,
    per_kind_tolerance: float = 0.08,
    strict: bool = False,
) -> int:
    """Fail when a recorded ARM metric drops by more than `tolerance`, or a per-KIND
    metric by more than `per_kind_tolerance`.

    The per-kind floor exists because the mean hides a category collapse: reverting
    the arm weighting left the fused MRR bit-identical at 0.699 while particle_glue
    fell 0.833 -> 0.611 (AGENTS.md '## Measuring retrieval'). A per-kind bucket is
    small (n=2-4), so a single-query flip is a real signal; the looser tolerance
    catches that collapse without firing on a rounding wobble.
    """
    baseline_file = _baseline_file(result)
    record_cmd = (
        "make eval-rerank-baseline"
        if baseline_file.name.startswith(BASELINE_RERANK_FILE.stem)
        else "make eval-baseline"
    )
    if not baseline_file.exists():
        # SKIP, and under --assert-baseline that is a FAILURE, not a pass. A
        # baseline is only ever created deliberately (--record-baseline), so a
        # missing one means the gate is measuring nothing. This is not
        # hypothetical: the per-asset split means adopting fp16 selects
        # `eval_baseline.fp16.json`, which does not exist -- turning the blocking
        # retrieval floor into a no-op at exactly the moment the vector space
        # changes and the floor is most needed.
        print(f"[eval] SKIP: no baseline at {baseline_file}; record one with `{record_cmd}`.")
        return 1 if strict else 0

    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))

    # The floor is only comparable on the corpus the baseline was recorded on.
    # Adding documents (the platform's whole purpose) shifts every ranking, which
    # is a corpus change, not a retrieval regression. When the indexed corpus
    # differs, skip rather than false-alarm -- same spirit as the resolvability
    # skip in evaluate(). Re-record with `make eval-baseline` to adopt the new
    # corpus as the floor.
    base_count = baseline.get("chunk_count")
    current_count = result.get("chunk_count")
    if base_count is not None and current_count is not None and base_count != current_count:
        print(
            f"\n[eval] corpus changed since the baseline ({base_count} -> {current_count} "
            "chunks); the regression floor is not comparable and is skipped. Re-record "
            "with `make eval-baseline` if this corpus is the new normal.",
        )
        return 1 if strict else 0

    regressions = []
    for arm, metrics in baseline.get("arms", {}).items():
        for metric, previous in metrics.items():
            current = result["arms"].get(arm, {}).get(metric)
            if current is not None and current < previous - tolerance:
                regressions.append(f"{arm}.{metric} {previous:.3f} -> {current:.3f}")

    # Per-kind floor: a category can collapse while the mean holds. Buckets are
    # small, so treat a per-kind drop as a real signal, not noise -- and fail on it.
    kind_regressions = []
    for kind, metrics in baseline.get("by_kind", {}).items():
        if "n" in metrics:  # the negative kind has no positive metric to floor
            continue
        current_bucket = result.get("by_kind", {}).get(kind)
        if not current_bucket or "n" in current_bucket:
            continue  # kind absent from this run (corpus shifted) -- skip, don't false-alarm
        for metric, previous in metrics.items():
            current = current_bucket.get(metric)
            if current is not None and current < previous - per_kind_tolerance:
                kind_regressions.append(f"{kind}.{metric} {previous:.3f} -> {current:.3f}")

    # Reranked per-kind floor: present only when a RERANK=1 baseline was recorded
    # (via `make eval-rerank-baseline`). The default rerank-off run has no
    # by_kind_reranked, so both sides are empty and this is a no-op -- the opt-in
    # reranker is gated by `make eval-rerank` without touching the default path.
    for kind, metrics in baseline.get("by_kind_reranked", {}).items():
        current_bucket = result.get("by_kind_reranked", {}).get(kind)
        if not current_bucket:
            continue
        for metric, previous in metrics.items():
            current = current_bucket.get(metric)
            if current is not None and current < previous - per_kind_tolerance:
                kind_regressions.append(f"reranked:{kind}.{metric} {previous:.3f} -> {current:.3f}")

    base_sha = (baseline.get("source_sha") or "")[:8] or "(no sha)"
    if regressions or kind_regressions:
        if regressions:
            print(f"\n[eval] ARM REGRESSION vs baseline {base_sha} (tolerance {tolerance}):")
            for line in regressions:
                print(f"  - {line}")
        if kind_regressions:
            print(f"\n[eval] PER-KIND REGRESSION vs baseline {base_sha} (tolerance {per_kind_tolerance}):")
            for line in kind_regressions:
                print(f"  - {line}")
        return 1
    print(
        f"\n[eval] no regression vs baseline {base_sha} "
        f"(arm tolerance {tolerance}, per-kind {per_kind_tolerance})."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record-baseline", action="store_true", help="Write the current numbers as the floor.")
    parser.add_argument("--assert-baseline", action="store_true", help="Fail if a metric regressed.")
    parser.add_argument("--json", action="store_true", help="Emit the raw result.")
    args = parser.parse_args(argv)

    result = evaluate()
    if result is None:
        # A corpus mismatch is not a failure: a fresh clone has only fixtures.
        return 0

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)

    if args.record_baseline:
        kind = build_kind()
        if kind == "incremental":
            print(
                "[eval] REFUSING to record a baseline from an incrementally-built index.\n"
                "       Cached vectors were encoded beside different neighbours, and this graph\n"
                "       is dynamically quantised, so they differ from what a cold build produces\n"
                "       (measured: cosine 0.9904, enough to move R@10 by 0.15). A floor recorded\n"
                "       here would measure a build history.\n"
                "       Run `make index-canonical` first, then record.",
                file=sys.stderr,
            )
            return 1
        baseline_file = _baseline_file(result)
        baseline_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n[eval] baseline written to {baseline_file}")
        return 0

    if args.assert_baseline:
        # strict: a gate that cannot tell 'no regression' from 'nothing measured'
        # is not a gate.
        return compare(result, strict=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
