"""Render the corpus graph as a standalone, offline HTML page.

Reads the same nodes and edges the agent traverses — `gold.entities` /
`gold.relations` from DuckLake, or the materialised `nodes` / `edges` tables in a
serving-index SQLite file — and injects them into `template.html`, a
self-contained Canvas visualisation. No third-party library, no build step, no
network: the template inlines all CSS and JS, so the output opens straight from
`file://` and keeps the read path offline (invariant #7). The front-end asset
lives beside this script, mirroring `tools/hitl/` (server.py + input.css).

  make graph                 # -> data/serving/graph.html  (reads lake.gold)
  make graph SOURCE=index    # read the built serving index instead
  python tools/viz/graph.py [--source lake|index] [--out PATH] [--include-fixtures]

For a LIVE view (auto-refresh + multi-database picker) run the server instead:
  make graph-serve           # tools/viz/server.py, which reuses the loaders here.

Smoke fixtures (pipeline/fixtures/: the platform's own design docs and .py files)
share the index but are HIDDEN by default here, exactly as `hybrid_search` hides
them — so the graph shows the real document corpus (bills, statutes, filings), not
the platform's own scaffolding. Pass --include-fixtures / ?fixtures=1 to keep them.

Source selection (`--source`):
  auto  (default) serving index if it is built and non-empty, else lake.gold
  lake            always read lake.gold.entities / lake.gold.relations
  index           always read the serving index nodes / edges tables

Reading `lake.gold` works before `make build` has materialised the serving
index, so a fresh checkout can render the graph after `make transform` alone.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Optional

from pipeline import Paths, get_paths
from pipeline.build_rag import _fixture_doc_ids, connect_index, open_lake

TEMPLATE_PATH = Path(__file__).with_name("template.html")
PLACEHOLDER = "/*RAWDATA*/"


# ---------------------------------------------------------------- loaders

def read_lake_graph(paths: Paths, include_fixtures: bool = False) -> dict:
    """Graph rows straight from the gold layer (always current, no index needed)."""
    lake = open_lake(paths)
    try:
        node_rows = lake.execute(
            """
            SELECT entity_id, kind, label, in_degree, out_degree
            FROM lake.gold.entities
            ORDER BY in_degree + out_degree DESC, entity_id
            """
        ).fetchall()
        edge_rows = lake.execute(
            """
            SELECT source_entity, target_entity, relation, weight
            FROM lake.gold.relations
            ORDER BY weight DESC, source_entity
            """
        ).fetchall()
    finally:
        lake.close()
    if not node_rows:
        raise RuntimeError("lake.gold.entities is empty. Run the transform first (make transform).")
    return _drop_fixtures(_shape(node_rows, edge_rows), include_fixtures)


def read_sqlite_graph(sqlite_path: Path, include_fixtures: bool = False) -> dict:
    """Graph rows from a serving-index SQLite file's nodes/edges tables."""
    connection = connect_index(Path(sqlite_path), read_only=True)
    try:
        node_rows = connection.execute(
            "SELECT node_id, kind, label, in_degree, out_degree FROM nodes"
            " ORDER BY in_degree + out_degree DESC, node_id"
        ).fetchall()
        edge_rows = connection.execute(
            "SELECT source_id, target_id, relation, weight FROM edges ORDER BY weight DESC, source_id"
        ).fetchall()
    finally:
        connection.close()
    if not node_rows:
        raise RuntimeError(f"{sqlite_path} has no graph rows. Build it (make build) or use --source lake.")
    return _drop_fixtures(_shape(node_rows, edge_rows), include_fixtures)


def _shape(node_rows, edge_rows) -> dict:
    nodes = [
        {"id": r[0], "kind": r[1], "label": r[2] or r[0], "ind": int(r[3] or 0), "outd": int(r[4] or 0)}
        for r in node_rows
    ]
    edges = [
        {"source": r[0], "target": r[1], "relation": r[2], "weight": float(r[3])}
        for r in edge_rows
    ]
    return {"nodes": nodes, "edges": edges}


def _drop_fixtures(graph: dict, include_fixtures: bool) -> dict:
    """Hide the smoke-test scaffolding the same way hybrid_search does.

    Two things go: the fixture DOC ids (pipeline/fixtures/), and every code entity
    (kind symbol/module) — code is never legal corpus, so it has no place in the
    default document view. Real documents and statutes are always kept, even when
    isolated (a filing with no extracted edges is still real corpus). Pass
    include_fixtures to keep the full graph (code-impact view)."""
    if include_fixtures:
        return graph
    deny = _fixture_doc_ids()
    keep = [
        n for n in graph["nodes"]
        if n["id"] not in deny and n["kind"] not in ("symbol", "module")
    ]
    keep_ids = {n["id"] for n in keep}
    edges = [e for e in graph["edges"] if e["source"] in keep_ids and e["target"] in keep_ids]
    return {"nodes": keep, "edges": edges}


# ---------------------------------------------------------------- change signatures (for live refresh)

def sqlite_signature(sqlite_path: Path) -> str:
    """Cheap change token for a SQLite graph db: mtime + size, no DB open."""
    p = Path(sqlite_path)
    if not p.exists():
        return "absent"
    st = p.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def lake_signature(paths: Paths) -> str:
    """Change token for the gold layer: row counts + latest ingest stamp."""
    lake = open_lake(paths)
    try:
        row = lake.execute(
            "SELECT COUNT(*), COALESCE(MAX(ingested_at)::VARCHAR, '') FROM lake.gold.relations"
        ).fetchone()
        n = lake.execute("SELECT COUNT(*) FROM lake.gold.entities").fetchone()[0]
    finally:
        lake.close()
    return f"{n}:{row[0]}:{row[1]}"


# ---------------------------------------------------------------- index discovery / selection

def index_has_graph(sqlite_path: Path) -> bool:
    p = Path(sqlite_path)
    if not p.exists():
        return False
    try:
        connection = connect_index(p, read_only=True)
        try:
            tables = {
                r[0] for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"nodes", "edges"} <= tables:
                return False
            return connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def load_graph(paths: Paths, source: str = "auto", include_fixtures: bool = False) -> dict:
    if source == "index":
        return read_sqlite_graph(paths.index_sqlite, include_fixtures)
    if source == "lake":
        return read_lake_graph(paths, include_fixtures)
    # auto: prefer the freshest source. gold is re-derived on every transform, so it
    # tracks the corpus; the serving index is a snapshot that can lag until rebuilt.
    if paths.ducklake_catalog.exists():
        return read_lake_graph(paths, include_fixtures)
    return read_sqlite_graph(paths.index_sqlite, include_fixtures)


# ---------------------------------------------------------------- static render

def render_html(graph: dict) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise RuntimeError(f"{TEMPLATE_PATH} is missing the {PLACEHOLDER} data placeholder.")
    # json.dumps produces no '*/' sequence, so it cannot terminate the JS comment early.
    return template.replace(PLACEHOLDER, json.dumps(graph, ensure_ascii=False), 1)


def build_viz(paths: Optional[Paths] = None, source: str = "auto",
              out: Optional[Path] = None, include_fixtures: bool = False) -> dict:
    """Render the graph HTML. Idempotent: same inputs, same file."""
    paths = paths or get_paths()
    paths.ensure()
    graph = load_graph(paths, source, include_fixtures)
    out = out or (paths.serving / "graph.html")
    out.write_text(render_html(graph), encoding="utf-8")
    return {"path": out, "nodes": len(graph["nodes"]), "edges": len(graph["edges"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render the corpus graph to a standalone offline HTML page.")
    parser.add_argument("--source", default="auto", choices=("auto", "lake", "index"))
    parser.add_argument("--out", type=Path, default=None, help="Output HTML path (default data/serving/graph.html).")
    parser.add_argument("--include-fixtures", action="store_true",
                        help="Include pipeline/fixtures scaffolding (hidden by default, like hybrid_search).")
    args = parser.parse_args(argv)

    summary = build_viz(source=args.source, out=args.out, include_fixtures=args.include_fixtures)
    print(f"[viz.graph] {summary['nodes']} node(s), {summary['edges']} edge(s) -> {summary['path']}")
    print(f"[viz.graph] open it: file://{summary['path'].resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
