"""Agent tool: hybrid_search.

Retrieve passages from the local corpus using vector search (sqlite-vec) and
keyword search (FTS5) fused with reciprocal rank fusion.

Call this for any question about what the documents or the code actually say.
Prefer it over grep: grep finds a literal string, this also finds the passage
that describes the same idea in different words. Prefer it over a pure vector
store: an exact identifier such as `hybrid_search` is a token FTS5 matches
exactly and an embedding only approximates. When the answer may sit in a
GRAPH-CONNECTED document the query's wording cannot reach (the bill delegating
to the same statute), use `agent.tools.graph_rag` instead — its `results`
section is exactly this search, plus a `related` section with edge provenance.

CLI:
    uv run python -m agent.tools.hybrid_search "how does promotion work" --limit 5
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

from pipeline import get_paths, get_settings
from pipeline.build_rag import hybrid_search, read_index_meta

SNIPPET_CHARS = 400


def hybrid_search_tool(
    query: str,
    limit: int = 5,
    candidates: int = 40,
    full_content: bool = False,
    collection: Optional[str] = None,
    include_fixtures: bool = False,
) -> dict:
    """Run a fused vector+keyword search over the serving index.

    Args:
        query: Natural-language question, or an exact identifier.
        limit: Number of passages to return.
        candidates: Depth of each ranking before fusion. Raising it helps when a
            passage is mid-ranked by both halves rather than top-ranked by one.
        full_content: Return whole chunks instead of leading snippets.
        collection: Restrict the search to one collection (the document's first
            inbox folder). None searches every collection.

    Returns:
        A dict with `query` and `results`. Each result carries `rel_path`,
        `collection`, `heading`, `content`, the per-ranking positions
        (`vector_rank`, `keyword_rank`, either of which may be null when only one
        half matched) and the fused `rrf_score`.
    """
    paths = get_paths()
    settings = get_settings()
    hits = hybrid_search(
        query,
        limit=limit,
        candidates=candidates,
        paths=paths,
        settings=settings,
        collection=collection,
        include_fixtures=include_fixtures,
    )

    results = []
    for hit in hits:
        content = hit["content"]
        results.append(
            {
                "chunk_id": hit["chunk_id"],
                "doc_id": hit["doc_id"],
                "rel_path": hit["rel_path"],
                "collection": hit["collection"],
                "title": hit["title"],
                "heading": hit["heading"],
                "doc_type": hit["doc_type"],
                "content": content if full_content else content[:SNIPPET_CHARS],
                "truncated": (not full_content) and len(content) > SNIPPET_CHARS,
                "vector_rank": hit["vector_rank"],
                "keyword_rank": hit["keyword_rank"],
                "rrf_score": round(hit["rrf_score"], 6),
            }
        )

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
        "result_count": len(results),
        "results": results,
        # Which index answered, so a caller can judge staleness/trust without the
        # answer's correctness depending on it. All from index_meta (build-time);
        # no per-hit tier or confidence, which would be fabricated signal.
        "provenance": {
            "index_signature": meta.get("index_signature"),
            "embedding_provider": meta.get("embedding_provider"),
            "node_role": meta.get("node_role"),
            "chunk_count": _int(meta.get("chunk_count")),
            "node_count": _int(meta.get("node_count")),
            "edge_count": _int(meta.get("edge_count")),
        },
    }


def list_collections() -> dict:
    """Report every collection in the index with its chunk and document counts."""
    from pipeline.build_rag import connect_index

    paths = get_paths()
    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        rows = connection.execute(
            "SELECT collection, COUNT(*) AS chunks, COUNT(DISTINCT doc_id) AS documents "
            "FROM chunks GROUP BY collection ORDER BY documents DESC, collection"
        ).fetchall()
    finally:
        connection.close()
    return {"collections": [dict(row) for row in rows]}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query", nargs="?", help="Question or exact identifier to search for.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--candidates", type=int, default=40)
    parser.add_argument("--full-content", action="store_true")
    parser.add_argument("--collection", default=None, help="Restrict to one collection (inbox folder).")
    parser.add_argument("--include-fixtures", action="store_true", help="Include smoke fixtures (hidden from real answers by default).")
    parser.add_argument("--list-collections", action="store_true", help="List collections with counts, then exit.")
    args = parser.parse_args(argv)

    if args.list_collections:
        print(json.dumps(list_collections(), indent=2, ensure_ascii=False))
        return 0
    if not args.query:
        parser.error("a query is required unless --list-collections is given")

    payload = hybrid_search_tool(
        args.query,
        limit=args.limit,
        candidates=args.candidates,
        full_content=args.full_content,
        collection=args.collection,
        include_fixtures=args.include_fixtures,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
