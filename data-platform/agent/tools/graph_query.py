"""Agent tool: graph_query.

Walk the dependency graph built from document declarations and code imports.

Call this for reachability questions, not content questions. "What breaks if I
change X" is a graph question; "what does X say about retries" is a
`hybrid_search` question. The distinction matters because impact is transitive:
if A imports B and B imports C, then C's blast radius includes A, and no amount
of keyword or vector search over the text of C will reveal that.

direction='upstream'   -> what depends on this (blast radius / impact)
direction='downstream' -> what this depends on (prerequisites)

For the connected documents' PASSAGES rather than their names — content plus
reachability in one call — use `agent.tools.graph_rag`, whose `related` section
walks these same edges and returns text with the edge as provenance.

CLI:
    uv run python -m agent.tools.graph_query vector_store --direction upstream
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from pipeline import get_paths, no_index_message
from pipeline.build_graph import DIRECTIONS, graph_query, resolve_node
from pipeline.build_rag import connect_index


def graph_query_tool(
    node: str,
    direction: str = "upstream",
    max_depth: int = 3,
    relations: Sequence[str] | None = None,
    limit: int = 50,
) -> dict:
    """Traverse the graph from one node and report what is reachable.

    Args:
        node: Node id to start from, for example `vector_store` or a doc slug.
            If it is not an exact id, the closest candidates are resolved first.
        direction: 'upstream' for impact, 'downstream' for prerequisites.
        max_depth: Hop limit. Depth 1 is direct neighbours only.
        relations: Restrict the walk to these edge types, for example
            ['imports', 'depends_on'].
        limit: Maximum nodes returned.

    Returns:
        A dict with the resolved start node, the candidates considered, and the
        reachable nodes with their minimum `depth` and the relations used.
    """
    paths = get_paths()
    if not paths.index_sqlite.exists():
        raise FileNotFoundError(no_index_message(paths.index_sqlite))

    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        candidates = resolve_node(node, connection=connection)
        exact = connection.execute(
            "SELECT node_id FROM nodes WHERE node_id = ?", (node,)
        ).fetchone()
        start = node if exact else (candidates[0]["node_id"] if candidates else node)

        hits = graph_query(
            start,
            direction=direction,
            max_depth=max_depth,
            relations=relations,
            limit=limit,
            connection=connection,
        )
    finally:
        connection.close()

    return {
        "requested": node,
        "start_node": start,
        "resolved_from_candidates": not bool(exact),
        "candidates": [item["node_id"] for item in candidates],
        "direction": direction,
        "max_depth": max_depth,
        "relations": list(relations) if relations else None,
        "result_count": len(hits),
        "results": hits,
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("node", help="Node id (or a name close to one) to start from.")
    parser.add_argument("--direction", default="upstream", choices=sorted(DIRECTIONS))
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--relations", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    payload = graph_query_tool(
        args.node,
        direction=args.direction,
        max_depth=args.max_depth,
        relations=args.relations,
        limit=args.limit,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
