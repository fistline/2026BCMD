"""Build the graph half of the serving index and traverse it.

Reads `lake.gold.entities` and `lake.gold.relations` out of DuckLake into two
tables in the same SQLite file the vectors live in:

  nodes  one row per entity (document, module, symbol)
  edges  one directed, weighted, typed row per relation

The point is multi-hop reachability. "A depends on B, B uses C, therefore a
change to C affects A" is not answerable with a join of fixed depth, because the
depth is a property of the data rather than of the question. A recursive CTE
walks until it runs out of edges or hits the depth cap.

Two guards matter in that recursion:
  * the visited-path check stops a cycle from looping forever, and
  * the depth cap bounds the blast radius on a densely connected graph.
Neither is optional: without them a single cyclic import chain hangs the agent.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Optional, Sequence

from pipeline import Paths, get_paths
from pipeline.build_rag import connect_index, open_lake

DIRECTIONS = {
    # direction -> (column matched against the frontier, column stepped to)
    "downstream": ("source_id", "target_id"),
    "upstream": ("target_id", "source_id"),
}

MAX_DEPTH_LIMIT = 12


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS nodes;
        CREATE TABLE nodes (
            node_id    TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,
            label      TEXT,
            doc_id     TEXT,
            rel_path   TEXT,
            in_degree  INTEGER NOT NULL DEFAULT 0,
            out_degree INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE edges (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation  TEXT NOT NULL,
            weight    REAL NOT NULL,
            doc_id    TEXT,
            rel_path  TEXT,
            evidence  TEXT,
            PRIMARY KEY (source_id, target_id, relation)
        );
        CREATE INDEX edges_source ON edges (source_id);
        CREATE INDEX edges_target ON edges (target_id);
        CREATE INDEX nodes_kind ON nodes (kind);
        """
    )


def build_graph(paths: Optional[Paths] = None) -> dict:
    """Rebuild nodes and edges from the gold layer. Idempotent by construction."""
    paths = paths or get_paths()
    paths.ensure()

    lake = open_lake(paths)
    try:
        node_rows = lake.execute(
            """
            SELECT entity_id, kind, label, doc_id, rel_path, in_degree, out_degree
            FROM lake.gold.entities
            ORDER BY entity_id
            """
        ).fetchall()
        edge_rows = lake.execute(
            """
            SELECT source_entity, target_entity, relation, weight, doc_id, rel_path, evidence
            FROM lake.gold.relations
            ORDER BY source_entity, target_entity, relation
            """
        ).fetchall()
    finally:
        lake.close()

    if not node_rows:
        raise RuntimeError(
            "lake.gold.entities is empty. Run the ingest and transform steps first "
            "(make ingest && make transform)."
        )

    connection = connect_index(paths.index_sqlite)
    try:
        with connection:
            _create_schema(connection)
            connection.executemany(
                "INSERT INTO nodes (node_id, kind, label, doc_id, rel_path, in_degree, out_degree)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                node_rows,
            )
            # INSERT OR IGNORE: gold.relations is unique per (source, relation,
            # target) but two documents can assert the same edge, and the graph
            # only needs it once.
            connection.executemany(
                "INSERT OR IGNORE INTO edges"
                " (source_id, target_id, relation, weight, doc_id, rel_path, evidence)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                edge_rows,
            )
            connection.executemany(
                "INSERT INTO index_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [("node_count", str(len(node_rows))), ("edge_count", str(len(edge_rows)))],
            )
    finally:
        connection.close()

    return {"nodes": len(node_rows), "edges": len(edge_rows)}


def _traversal_sql(from_column: str, to_column: str, relation_filter: bool) -> str:
    """Recursive reachability walk.

    `visited` carries the path as a delimited string so a node already on the
    current path is never expanded again. Without it, any cycle in the edge set
    makes this query non-terminating.
    """
    clause = "AND edges.relation IN (SELECT value FROM json_each(:relations))" if relation_filter else ""
    return f"""
WITH RECURSIVE walk(node_id, depth, visited, total_weight, via_relation, parent_id) AS (
    SELECT
        :start_node,
        0,
        '/' || :start_node || '/',
        0.0,
        NULL,
        NULL

    UNION ALL

    SELECT
        edges.{to_column},
        walk.depth + 1,
        walk.visited || edges.{to_column} || '/',
        walk.total_weight + edges.weight,
        edges.relation,
        walk.node_id
    FROM walk
    JOIN edges ON edges.{from_column} = walk.node_id
    WHERE walk.depth < :max_depth
      AND INSTR(walk.visited, '/' || edges.{to_column} || '/') = 0
      {clause}
),
shortest AS (
    SELECT
        node_id,
        MIN(depth) AS depth,
        MAX(total_weight) AS total_weight
    FROM walk
    WHERE depth > 0
    GROUP BY node_id
)
SELECT
    shortest.node_id,
    shortest.depth,
    ROUND(shortest.total_weight, 4) AS total_weight,
    nodes.kind,
    nodes.label,
    nodes.rel_path,
    (
        SELECT GROUP_CONCAT(DISTINCT walk.via_relation)
        FROM walk
        WHERE walk.node_id = shortest.node_id AND walk.depth = shortest.depth
    ) AS via_relations
FROM shortest
LEFT JOIN nodes ON nodes.node_id = shortest.node_id
ORDER BY shortest.depth, shortest.total_weight DESC, shortest.node_id
LIMIT :limit
"""


def graph_query(
    start_node: str,
    direction: str = "downstream",
    max_depth: int = 3,
    relations: Optional[Sequence[str]] = None,
    limit: int = 50,
    connection: Optional[sqlite3.Connection] = None,
    paths: Optional[Paths] = None,
) -> list:
    """Walk the dependency graph outward from one node.

    direction='downstream' answers "what does this depend on".
    direction='upstream'   answers "what breaks if this changes" (impact).
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(DIRECTIONS)}, got {direction!r}")
    if not 1 <= max_depth <= MAX_DEPTH_LIMIT:
        raise ValueError(f"max_depth must be between 1 and {MAX_DEPTH_LIMIT}, got {max_depth}")

    paths = paths or get_paths()
    owns_connection = connection is None
    if connection is None:
        if not paths.index_sqlite.exists():
            raise FileNotFoundError(
                f"{paths.index_sqlite} does not exist. Build it with `make build`."
            )
        connection = connect_index(paths.index_sqlite, read_only=True)

    from_column, to_column = DIRECTIONS[direction]
    parameters = {
        "start_node": start_node,
        "max_depth": max_depth,
        "limit": limit,
    }
    if relations:
        parameters["relations"] = json.dumps(list(relations))

    try:
        rows = connection.execute(
            _traversal_sql(from_column, to_column, relation_filter=bool(relations)),
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_connection:
            connection.close()


def resolve_node(
    text: str,
    connection: Optional[sqlite3.Connection] = None,
    paths: Optional[Paths] = None,
    limit: int = 5,
) -> list:
    """Find candidate node ids for a human-typed name."""
    paths = paths or get_paths()
    owns_connection = connection is None
    if connection is None:
        connection = connect_index(paths.index_sqlite, read_only=True)
    needle = text.strip().lower()
    try:
        rows = connection.execute(
            """
            SELECT node_id, kind, label, rel_path, in_degree, out_degree
            FROM nodes
            WHERE LOWER(node_id) = :exact
               OR LOWER(node_id) LIKE '%' || :needle || '%'
               OR LOWER(COALESCE(label, '')) LIKE '%' || :needle || '%'
            ORDER BY (LOWER(node_id) = :exact) DESC, in_degree + out_degree DESC, node_id
            LIMIT :limit
            """,
            {"exact": needle, "needle": needle, "limit": limit},
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owns_connection:
            connection.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build or traverse the graph half of the index.")
    parser.add_argument("--start", help="Traverse from this node instead of rebuilding.")
    parser.add_argument("--direction", default="upstream", choices=sorted(DIRECTIONS))
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--relations", nargs="*", help="Restrict traversal to these edge types.")
    args = parser.parse_args(argv)

    if args.start:
        hits = graph_query(
            args.start,
            direction=args.direction,
            max_depth=args.max_depth,
            relations=args.relations,
        )
        for hit in hits:
            print(
                f"depth={hit['depth']}  weight={hit['total_weight']}  "
                f"{hit['node_id']} ({hit['kind']}) via {hit['via_relations']}"
            )
        if not hits:
            print(f"no nodes reachable {args.direction} from {args.start!r}")
        return 0

    summary = build_graph()
    print("[build_graph] {nodes} node(s), {edges} edge(s)".format(**summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
