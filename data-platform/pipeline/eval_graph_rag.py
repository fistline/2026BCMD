"""Regression floor for the RELATED section of graph_rag_search (make ask).

`make eval` protects the direct arms; this protects the graph arm's promise:
that a graph-connected document's relevant article surfaces in `related` when
the direct top hits missed it. The judgments in eval_graph_rag_queries.json
are anchored by doc-id regex + heading regex — never chunk_id, never rank —
because chunk ids are positional and related ordering legitimately moves when
the relation-weight ontology is tuned. Membership is the contract.

Scoring per expectation:
  hit       a related row matches (doc regex, heading regex)
  absorbed  the expected doc became a SEED doc — the related section excludes
            seeds by design, so this is not a miss; excluded from the mean
  skipped   the doc regex matches nothing in the index (different corpus)
  miss      the doc is indexed, not a seed, and no related row matched

    uv run python -m pipeline.eval_graph_rag                    report
    uv run python -m pipeline.eval_graph_rag --record-baseline  set the floor
    uv run python -m pipeline.eval_graph_rag --assert-baseline  gate
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pipeline import Paths, get_paths
from pipeline.build_rag import connect_index, read_index_meta
from pipeline.graph_rag import graph_rag_search

QUERIES_PATH = Path(__file__).parent / "eval_graph_rag_queries.json"
BASELINE_PATH = Path(__file__).parent / "eval_graph_rag_baseline.json"
# related_recall may only drop this much below the recorded floor before the
# assert fails. Membership of a handful of judgments is coarse, so one lost
# expectation moves the mean a lot; the tolerance stays below one expectation's
# worth to make any real loss loud.
TOLERANCE = 0.05


def load_queries() -> list:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]


def evaluate(paths: Paths | None = None) -> dict | None:
    paths = paths or get_paths()
    if not paths.index_sqlite.exists():
        print("[eval_graph_rag] no index; run `make build` first")
        return None

    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        doc_ids = sorted(
            row[0] for row in connection.execute("SELECT DISTINCT doc_id FROM chunks")
        )

        per_query = []
        hits = misses = absorbed = skipped = 0
        for judgment in load_queries():
            # depth=2 with a widened doc cap, deliberately: when statute
            # documents enter the corpus (the statute doctype profile), the
            # shared statute stops being a co-citation pivot and becomes a
            # depth-1 document itself, pushing sibling bills to depth 2. The
            # judgments assert MEMBERSHIP of the sibling's article, and this
            # setting keeps that membership reachable in both corpus shapes
            # instead of failing the gate on a corpus migration.
            payload = graph_rag_search(
                judgment["query"], limit=5, depth=2, max_related_docs=8,
                connection=connection, paths=paths,
            )
            seeds = set(payload["seed_docs"])
            rows = payload["related"]
            outcome = []
            for expectation in judgment["expect"]:
                doc_re = re.compile(expectation["doc"])
                heading_re = re.compile(expectation["heading"])
                indexed = [doc for doc in doc_ids if doc_re.search(doc)]
                if not indexed:
                    outcome.append({"doc": expectation["doc"], "status": "skipped"})
                    skipped += 1
                    continue
                if any(doc_re.search(seed) for seed in seeds):
                    outcome.append({"doc": expectation["doc"], "status": "absorbed"})
                    absorbed += 1
                    continue
                matched = any(
                    doc_re.search(row["doc_id"]) and heading_re.search(row["heading"] or "")
                    for row in rows
                )
                outcome.append(
                    {"doc": expectation["doc"], "status": "hit" if matched else "miss"}
                )
                if matched:
                    hits += 1
                else:
                    misses += 1
            per_query.append({"id": judgment["id"], "query": judgment["query"], "outcome": outcome})
    finally:
        connection.close()

    scored = hits + misses
    result = {
        "related_recall": round(hits / scored, 4) if scored else None,
        "hits": hits,
        "misses": misses,
        "absorbed": absorbed,
        "skipped": skipped,
        "scored": scored,
        "per_query": per_query,
        "index_signature": read_index_meta(paths).get("index_signature"),
    }
    return result


def render(result: dict) -> None:
    print(
        f"[eval_graph_rag] related_recall={result['related_recall']}  "
        f"(hit {result['hits']} / miss {result['misses']} / "
        f"absorbed {result['absorbed']} / skipped {result['skipped']})"
    )
    for entry in result["per_query"]:
        statuses = ", ".join(f"{o['status']}" for o in entry["outcome"])
        print(f"  {entry['id']}  [{statuses}]  {entry['query']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record-baseline", action="store_true")
    parser.add_argument("--assert-baseline", action="store_true")
    args = parser.parse_args(argv)

    result = evaluate()
    if result is None:
        # No index is a setup gap, not a regression; the build gate catches it.
        return 0
    render(result)

    if args.record_baseline:
        baseline = {
            "related_recall": result["related_recall"],
            "scored": result["scored"],
            "index_signature": result["index_signature"],
        }
        BASELINE_PATH.write_text(
            json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"[eval_graph_rag] baseline recorded -> {BASELINE_PATH.name}")
        return 0

    if args.assert_baseline:
        if not BASELINE_PATH.exists():
            print("[eval_graph_rag] no baseline recorded; run --record-baseline first")
            return 1
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        floor = baseline.get("related_recall")
        current = result["related_recall"]
        if floor is None or current is None:
            # An unscorable run (different corpus) cannot regress the floor.
            print("[eval_graph_rag] not scorable against this corpus; skipping gate")
            return 0
        if current < floor - TOLERANCE:
            print(
                f"[eval_graph_rag] FAIL related_recall {current} dropped below "
                f"baseline {floor} - {TOLERANCE}"
            )
            return 1
        print(f"[eval_graph_rag] OK related_recall {current} >= {floor} - {TOLERANCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
