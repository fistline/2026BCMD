"""Agent tool: graph_rag.

Hybrid retrieval plus graph-related context in one call. The `results` section
is exactly what `hybrid_search` returns; the `related` section adds passages
from documents the dependency graph connects to the seed documents — the bill
that delegates to the same statute, the module that imports what you found —
each row carrying the edges that justify it. The two sections are not fused:
direct hits answer the question, related rows widen it, and the caller can see
which is which.

Call this instead of `hybrid_search` when the answer may live in a connected
document the query's own vocabulary cannot reach. Call `hybrid_search` when
only the direct answer matters, and `graph_query` for pure reachability.

CLI:
    uv run python -m agent.tools.graph_rag "예치금 분리보관 의무" --depth 1
"""

from __future__ import annotations

import argparse
import json

from pipeline import get_paths
from pipeline.build_rag import read_index_meta
from pipeline.graph_rag import DEPTH_CEILING, graph_rag_search

SNIPPET_CHARS = 400


def _snippet(row: dict, full_content: bool) -> dict:
    content = row["content"]
    row = dict(row)
    row["content"] = content if full_content else content[:SNIPPET_CHARS]
    row["truncated"] = (not full_content) and len(content) > SNIPPET_CHARS
    return row


def graph_rag_tool(
    query: str,
    limit: int = 5,
    depth: int | None = None,
    seed_limit: int = 5,
    relations: list | None = None,
    max_related_docs: int = 5,
    full_content: bool = False,
    collection: str | None = None,
    include_fixtures: bool = False,
) -> dict:
    """Run hybrid retrieval with graph-related context.

    Args:
        query: Natural-language question, or an exact identifier.
        limit: Number of direct passages to return.
        depth: 0 = no graph section, 1 = direct edges + co-citation (default
            from GRAPH_DEPTH), 2 = one more hop over direct document edges.
        seed_limit: How many top hits choose the seed documents.
        relations: Restrict connecting edges to these types (default: all
            except `mentions`).
        max_related_docs: Cap on related documents.
        full_content: Return whole chunks instead of leading snippets.
        collection: Scope the DIRECT search to one collection. Related
            discovery always crosses collections — that is what the graph is
            for — and each related row carries its own `collection`.

    Returns:
        A dict with `results` (identical to hybrid_search), `related` (each row
        carrying `connection.links` — the actual edges), `seed_docs`, and the
        same provenance footer the other tools report.
    """
    payload = graph_rag_search(
        query,
        limit=limit,
        depth=depth,
        seed_limit=seed_limit,
        relations=relations,
        max_related_docs=max_related_docs,
        collection=collection,
        include_fixtures=include_fixtures,
    )

    paths = get_paths()
    meta = read_index_meta(paths)

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    return {
        "query": query,
        "index": str(paths.index_sqlite),
        "collection": collection,
        "seed_docs": payload["seed_docs"],
        "expansion": payload["expansion"],
        "result_count": len(payload["results"]),
        "results": [
            {
                "chunk_id": hit["chunk_id"],
                "doc_id": hit["doc_id"],
                "rel_path": hit["rel_path"],
                "collection": hit["collection"],
                "title": hit["title"],
                "heading": hit["heading"],
                "doc_type": hit["doc_type"],
                **_snippet(hit, full_content),
                "vector_rank": hit["vector_rank"],
                "keyword_rank": hit["keyword_rank"],
                "rrf_score": round(hit["rrf_score"], 6),
            }
            for hit in payload["results"]
        ],
        "related_count": len(payload["related"]),
        "related": [
            {
                "chunk_id": row["chunk_id"],
                "doc_id": row["doc_id"],
                "rel_path": row["rel_path"],
                "collection": row["collection"],
                "title": row["title"],
                "heading": row["heading"],
                **_snippet(row, full_content),
                "selection": row["selection"],
                "connection": row["connection"],
            }
            for row in payload["related"]
        ],
        "provenance": {
            "index_signature": meta.get("index_signature"),
            "embedding_provider": meta.get("embedding_provider"),
            "node_role": meta.get("node_role"),
            "chunk_count": _int(meta.get("chunk_count")),
            "node_count": _int(meta.get("node_count")),
            "edge_count": _int(meta.get("edge_count")),
        },
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query", help="Question or exact identifier to search for.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        choices=range(0, DEPTH_CEILING + 1),
        help="0 = plain hybrid search, 1 = direct + co-citation, 2 = one more doc hop.",
    )
    parser.add_argument("--seed-limit", type=int, default=5)
    parser.add_argument("--relations", nargs="*", default=None, help="Restrict connecting edge types.")
    parser.add_argument("--max-related-docs", type=int, default=5)
    parser.add_argument("--full-content", action="store_true")
    parser.add_argument("--collection", default=None, help="Scope the direct search to one collection.")
    parser.add_argument("--include-fixtures", action="store_true", help="Include smoke fixtures (hidden from real answers by default).")
    args = parser.parse_args(argv)

    payload = graph_rag_tool(
        args.query,
        limit=args.limit,
        depth=args.depth,
        seed_limit=args.seed_limit,
        relations=args.relations,
        max_related_docs=args.max_related_docs,
        full_content=args.full_content,
        collection=args.collection,
        include_fixtures=args.include_fixtures,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
