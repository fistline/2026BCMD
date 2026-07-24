"""Sample fixture: entry point of the dependency chain.

service_api -> search_core -> vector_store. Editing vector_store must surface
this module as affected, two hops away.
"""

from search_core import hybrid_search


def handle_query(request, table, embedder):
    """Answer one retrieval request end to end."""
    query_text = request["q"]
    query_vector = embedder(query_text)
    hits = hybrid_search(query_vector, query_text, table, limit=request.get("limit", 5))
    return {"query": query_text, "hits": [identifier for identifier, _score in hits]}
