"""Graph-reachability evaluation: make a change to the graph ONTOLOGY measurable.

Retrieval has `make eval`; the graph had only a smoke check, so a relation-weight
or node-kind change shipped unmeasured. This is the graph analog of eval_retrieval:
a judgment set anchored on node SLUGS + relation type (NEVER on transient
degree/rank -- the graph analog of heading-regex anchoring), scored with a
WEIGHT-AWARE metric so a change to the weight ontology (e.g. promoting the
dominant `delegates_to` off the ELSE 0.3 tier in gold/relations.sql) is observable.

Four metrics per judgment, over the graph_query result (ordered by depth, then
total_weight DESC):
  reach          fraction of expected nodes reached within max_depth (weight-free)
  p_at_1         is the top weight-ranked node an expected one
  mrr            reciprocal rank of the first expected node in the weight order
  path_strength  mean accumulated total_weight of the reached expected nodes --
                 the ABSOLUTE weight signal, so a weight change moves it even when
                 a start node's neighbours are all one relation (no reordering).

    make eval-graph            report
    make eval-graph-baseline   record the current numbers as the floor
    uv run python -m pipeline.eval_graph --assert-baseline

Skips cleanly when the graph does not match the judgments: the fixture judgments
resolve on any build (the smoke graph is always seeded), the legal ones only once
the bills are indexed, so a fresh clone still scores something.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from pipeline import Paths, get_paths
from pipeline.build_graph import graph_query
from pipeline.build_rag import connect_index
from pipeline.eval_retrieval import _mean, _source_sha

QUERY_FILE = Path(__file__).with_name("eval_graph_queries.json")
BASELINE_FILE = Path(__file__).with_name("eval_graph_baseline.json")
# Below this share of judgments whose start_node exists as a node, the graph is
# not the one these judgments describe; skip rather than score noise.
MIN_RESOLVABLE = 0.5
METRICS = ("reach", "p_at_1", "mrr", "path_strength")


def load_queries(path: Optional[Path] = None) -> list:
    return json.loads((path or QUERY_FILE).read_text(encoding="utf-8"))["queries"]


def _score(result: list, relevant: set) -> dict:
    """Weight-aware reachability metrics. `result` is graph_query output already
    ordered by (depth ASC, total_weight DESC)."""
    reached = [row["node_id"] for row in result]
    reached_set = set(reached)
    first_rank = next((i for i, node_id in enumerate(reached) if node_id in relevant), None)
    strengths = [row["total_weight"] for row in result if row["node_id"] in relevant]
    return {
        "reach": round(len(relevant & reached_set) / len(relevant), 4) if relevant else 0.0,
        "p_at_1": 1.0 if (reached and reached[0] in relevant) else 0.0,
        "mrr": round(1.0 / (first_rank + 1), 4) if first_rank is not None else 0.0,
        "path_strength": round(sum(strengths) / len(strengths), 4) if strengths else 0.0,
    }


def evaluate(paths: Optional[Paths] = None) -> Optional[dict]:
    paths = paths or get_paths()
    if not paths.index_sqlite.exists():
        print("[eval-graph] no index; run `make build` first.", file=sys.stderr)
        return None

    queries = load_queries()
    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        node_count = connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        present = {row[0] for row in connection.execute("SELECT node_id FROM nodes")}
        # The relation->weight ontology the graph was built with, recorded so a
        # weight change is legible in the baseline's git diff.
        relation_weights = {
            row[0]: row[1]
            for row in connection.execute("SELECT relation, MIN(weight) FROM edges GROUP BY relation")
        }

        resolvable = [q for q in queries if q["start_node"] in present]
        if queries and len(resolvable) < MIN_RESOLVABLE * len(queries):
            print(
                f"[eval-graph] SKIPPED: only {len(resolvable)}/{len(queries)} start nodes resolve "
                "against this graph. Build the corpus these judgments describe, or write judgments "
                "for the graph you have.",
                file=sys.stderr,
            )
            return None

        overall: dict = defaultdict(list)
        by_kind: dict = {}
        per_query = []
        for query in resolvable:
            result = graph_query(
                query["start_node"],
                direction=query.get("direction", "downstream"),
                max_depth=query.get("max_depth", 3),
                relations=query.get("relations"),
                connection=connection,
            )
            scored = _score(result, set(query.get("relevant", [])))
            kind = query.get("kind", "unlabelled")
            bucket = by_kind.setdefault(kind, defaultdict(list))
            for metric, value in scored.items():
                overall[metric].append(value)
                bucket[metric].append(value)
            per_query.append({"id": query["id"], "kind": kind, **scored})
    finally:
        connection.close()

    return {
        "metrics": {metric: _mean(values) for metric, values in overall.items()},
        "by_kind": {
            kind: {metric: _mean(values) for metric, values in bucket.items()}
            for kind, bucket in by_kind.items()
        },
        "graded": len(resolvable),
        "node_count": node_count,
        "edge_count": edge_count,
        "relation_weights": relation_weights,
        "source_sha": _source_sha(),
        "per_query": per_query,
        "unresolved": [q["id"] for q in queries if q["start_node"] not in present],
    }


def render(result: dict) -> None:
    print(f"graded {result['graded']} judgments  nodes={result['node_count']} edges={result['edge_count']}")
    if result["unresolved"]:
        print(f"  unresolved (start node absent): {', '.join(result['unresolved'])}")
    print(f"  weights: {result['relation_weights']}")
    print()
    print(f"  {'metric':14} {'value':>8}")
    for metric in METRICS:
        print(f"  {metric:14} {result['metrics'].get(metric, 0):8.3f}")
    print("\n  by kind:")
    for kind, metrics in sorted(result["by_kind"].items()):
        print(
            f"    {kind:20} reach {metrics.get('reach', 0):.3f}  p@1 {metrics.get('p_at_1', 0):.3f}  "
            f"mrr {metrics.get('mrr', 0):.3f}  strength {metrics.get('path_strength', 0):.3f}"
        )


def compare(result: dict, tolerance: float = 0.05) -> int:
    """Fail when a graph metric drops by more than `tolerance`, overall or per kind."""
    if not BASELINE_FILE.exists():
        print(f"[eval-graph] no baseline at {BASELINE_FILE}; record one with `make eval-graph-baseline`.")
        return 0

    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    # The floor is comparable only on the graph it was recorded on. node+edge count
    # is the fingerprint (the graph analog of chunk_count): a corpus change shifts
    # reachability and is not a regression. Skip rather than false-alarm.
    for key in ("node_count", "edge_count"):
        base, current = baseline.get(key), result.get(key)
        if base is not None and current is not None and base != current:
            print(
                f"\n[eval-graph] graph changed since the baseline ({key} {base} -> {current}); "
                "the floor is not comparable and is skipped. Re-record with `make eval-graph-baseline`."
            )
            return 0

    regressions = []
    for metric, previous in baseline.get("metrics", {}).items():
        current = result["metrics"].get(metric)
        if current is not None and current < previous - tolerance:
            regressions.append(f"{metric} {previous:.3f} -> {current:.3f}")
    for kind, metrics in baseline.get("by_kind", {}).items():
        current_bucket = result.get("by_kind", {}).get(kind, {})
        for metric, previous in metrics.items():
            current = current_bucket.get(metric)
            if current is not None and current < previous - tolerance:
                regressions.append(f"{kind}.{metric} {previous:.3f} -> {current:.3f}")

    base_sha = (baseline.get("source_sha") or "")[:8] or "(no sha)"
    if regressions:
        print(f"\n[eval-graph] REGRESSION vs baseline {base_sha} (tolerance {tolerance}):")
        for line in regressions:
            print(f"  - {line}")
        return 1
    print(f"\n[eval-graph] no regression vs baseline {base_sha} (tolerance {tolerance}).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record-baseline", action="store_true", help="Write the current numbers as the floor.")
    parser.add_argument("--assert-baseline", action="store_true", help="Fail if a metric regressed.")
    parser.add_argument("--json", action="store_true", help="Emit the raw result.")
    args = parser.parse_args(argv)

    result = evaluate()
    if result is None:
        return 0  # a graph mismatch is not a failure: a fresh clone has only fixtures.

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)

    if args.record_baseline:
        BASELINE_FILE.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n[eval-graph] baseline written to {BASELINE_FILE}")
        return 0
    if args.assert_baseline:
        return compare(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
