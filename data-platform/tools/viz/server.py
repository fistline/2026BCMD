"""Live corpus-graph viewer. Stdlib only; no framework, no network beyond localhost.

Serves the same self-contained Canvas page as `tools/viz/graph.py`, but wired to
live endpoints instead of inlined data, so it adds three things a static file
cannot:

  * opens in the browser on start (an operator command, like tools/hitl),
  * a source picker when more than one graph database is present, and
  * auto-refresh: the page polls a cheap change token and re-renders whenever the
    selected graph db changes (e.g. after `make build` / while `make watch` runs).

It is an OPERATOR tool, off the build path (invariant #7 covers the read path,
not a local viewer). Data loaders are reused from `graph.py` so the static and
live views can never disagree about how a graph is read.

Run:
    make graph-serve                       # -> opens http://127.0.0.1:8777/
    uv run python tools/viz/server.py [--port 8777] [--no-open]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from pipeline import get_paths

# Load the sibling loaders by path so this works whether or not tools/ is a package.
_spec = importlib.util.spec_from_file_location("viz_graph", Path(__file__).with_name("graph.py"))
vg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vg)

PATHS = get_paths()
DB_LOCK = threading.Lock()  # duckdb connections are not thread-safe; serialise DB reads.


# ---------------------------------------------------------------- sources

def discover_sources() -> list:
    """Every graph database on offer: the live gold layer plus any serving *.sqlite."""
    sources = []
    if PATHS.ducklake_catalog.exists():
        sources.append({"id": "lake", "label": "lake.gold (라이브)", "type": "lake"})

    index_default = None
    serving = PATHS.serving
    if serving.exists():
        for p in sorted(serving.glob("*.sqlite")):
            if vg.index_has_graph(p):
                sid = f"index:{p.name}"
                label = f"{p.name} (빌드됨)"
                sources.append({"id": sid, "label": label, "type": "index", "path": str(p)})
                if p.name == "index.sqlite":
                    index_default = sid

    # Default to the freshest source: the live gold layer tracks the corpus, while
    # the built index can lag until rebuilt. The picker still exposes both.
    default_id = "lake" if PATHS.ducklake_catalog.exists() else index_default
    if default_id is None and sources:
        default_id = sources[0]["id"]
    for s in sources:
        s["default"] = s["id"] == default_id
    return sources


def _sqlite_path(source_id: str) -> Path:
    return PATHS.serving / source_id.split("index:", 1)[1]


def load_source(source_id: str, include_fixtures: bool = False) -> dict:
    with DB_LOCK:
        if source_id == "lake":
            return vg.read_lake_graph(PATHS, include_fixtures)
        if source_id.startswith("index:"):
            return vg.read_sqlite_graph(_sqlite_path(source_id), include_fixtures)
    raise ValueError(f"unknown source {source_id!r}")


def source_signature(source_id: str) -> str:
    with DB_LOCK:
        if source_id == "lake":
            return vg.lake_signature(PATHS)
        if source_id.startswith("index:"):
            return vg.sqlite_signature(_sqlite_path(source_id))
    raise ValueError(f"unknown source {source_id!r}")


# ---------------------------------------------------------------- page

BOOT = '<script>window.__VIZ_BOOT__ = {mode:"live", endpoint:"/api"};</script>\n'


def build_page() -> str:
    template = vg.TEMPLATE_PATH.read_text(encoding="utf-8")
    # Live mode reads data from /api, so the inlined placeholder becomes an unused null.
    page = template.replace(vg.PLACEHOLDER, "null", 1)
    return page.replace("<body>\n", "<body>\n" + BOOT, 1)


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "vizgraph/1.0"

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def do_GET(self) -> None:
        route = urlparse(self.path)
        path, query = route.path, parse_qs(route.query)
        try:
            if path in ("/", "/index.html"):
                self._send(self.server.page.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/sources":
                self._json(discover_sources())
            elif path == "/api/graph":
                fixtures = query.get("fixtures", ["0"])[0] in ("1", "true", "yes")
                self._json(load_source(query.get("source", ["lake"])[0], fixtures))
            elif path == "/api/version":
                self._json({"version": source_signature(query.get("source", ["lake"])[0])})
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # a broken source must not take the server down
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, *args) -> None:  # keep the console quiet; startup line is enough
        return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Live corpus-graph viewer in the browser.")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser automatically.")
    args = parser.parse_args(argv)

    PATHS.ensure()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.page = build_page()

    url = f"http://127.0.0.1:{args.port}/"
    sources = discover_sources()
    print(f"[viz.server] {len(sources)} graph db(s): {', '.join(s['id'] for s in sources) or 'none — run make build'}")
    print(f"[viz.server] serving {url}  (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[viz.server] stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
