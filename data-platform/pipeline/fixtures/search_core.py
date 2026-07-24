"""Sample fixture: middle module of the dependency chain.

Imports vector_store and is imported by service_api, so a change here has a
two-hop blast radius that graph_query is expected to find.
"""

from vector_store import knn_lookup


def reciprocal_rank_fusion(rankings, k=60):
    """Fuse several ranked id lists into one score per id."""
    scores = {}
    for ranking in rankings:
        for position, identifier in enumerate(ranking, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def hybrid_search(query_vector, query_text, table, limit=10):
    """Fuse a vector ranking and a keyword ranking into one result list."""
    vector_ranking = [row["id"] for row in knn_lookup(query_vector, table, limit=limit)]
    keyword_ranking = [row["id"] for row in table if query_text in row["text"]]
    fused = reciprocal_rank_fusion([vector_ranking, keyword_ranking])
    return fused[:limit]
