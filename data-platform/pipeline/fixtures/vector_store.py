"""Sample fixture: leaf module of the dependency chain.

service_api -> search_core -> vector_store. Nothing imports below this file, so
it is the natural blast-radius root for a code-impact question.
"""

import math


def knn_lookup(query_vector, table, limit=10):
    """Return the `limit` closest rows in `table` by cosine distance."""
    scored = []
    for row in table:
        scored.append((cosine_distance(query_vector, row["embedding"]), row))
    scored.sort(key=lambda pair: pair[0])
    return [row for _distance, row in scored[:limit]]


def cosine_distance(left, right):
    """Cosine distance between two equal-length sequences of floats."""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 1.0
    return 1.0 - (dot / (left_norm * right_norm))
