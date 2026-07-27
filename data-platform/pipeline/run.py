"""Orchestrator: ingest -> transform -> index -> sync.

This is the single entry point `make build` calls, and the same function the
watcher calls when a file lands in the inbox. Each stage is independently
runnable (`--only transform`) and every stage is idempotent, so an interrupted
build is fixed by running it again rather than by cleaning anything out.

Stage map:
  ingest     promote data/inbox -> data/raw (immutable), then Meltano EL into
             DuckLake `lake.raw.*`
  transform  SQLMesh bronze -> silver -> gold, with blocking audits
  index      gold -> data/serving/index.sqlite (vectors + FTS + graph)
  sync       push/pull the data plane; a no-op stub when DATA_REMOTE=local_only

This module owns the DuckLake catalog: it is created here, once, with a DATA_PATH
stored RELATIVE to the project root (so a checkout rename does not strand it).
Meltano's loader and SQLMesh both attach without declaring a data path, so there
is exactly one place that decides where Parquet files live, and every attach runs
with cwd = project root so the relative path resolves identically everywhere.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import duckdb

from pipeline import Paths, Settings, get_paths, get_settings
from pipeline.watcher import promote_inbox, seed_inbox

STAGES = ("ingest", "transform", "index", "sync")


def _log(stage: str, message: str) -> None:
    print(f"[{stage}] {message}", flush=True)


def _console_script(name: str) -> str:
    """Resolve a console script next to the running interpreter.

    Both Meltano and SQLMesh ship console scripts rather than `__main__`
    modules, so `python -m <name>` does not work. Looking beside sys.executable
    keeps the pipeline inside the project venv without requiring it be active.
    """
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        raise SystemExit(f"could not find the `{name}` executable; run `uv sync` first")
    return resolved


def _child_env(paths: Paths, settings: Settings) -> dict:
    """Environment shared by every subprocess.

    PLATFORM_PROCESSED_DIR is absolute on purpose: SQLMesh resolves its DuckLake
    catalog path against it, and a relative value would resolve differently
    depending on which directory the command was launched from.
    """
    env = dict(os.environ)
    env["PLATFORM_PROCESSED_DIR"] = str(paths.processed)
    env["PLATFORM_NODE_ROLE"] = settings.node_role
    env["SQLMESH_GATEWAY"] = settings.sqlmesh_gateway
    return env


def _run(command: Sequence[str], env: dict, stage: str, cwd=None) -> None:
    _log(stage, "$ " + " ".join(command))
    result = subprocess.run(list(command), env=env, cwd=str(cwd or get_paths().root), check=False)
    if result.returncode != 0:
        raise SystemExit(f"[{stage}] command failed with exit code {result.returncode}")


def ensure_ducklake(paths: Paths) -> None:
    """Create the DuckLake catalog and its raw schema. Safe to call repeatedly.

    Declaring DATA_PATH exactly once, here, is what keeps the loader and SQLMesh
    from disagreeing about it. DuckLake stores the data path inside the catalog
    and rejects a later attach that declares a different one.

    DATA_PATH is stored RELATIVE to the project root, not absolute, so renaming or
    moving the checkout does not strand the catalog on a path that no longer
    exists (the failure the earlier `platform` -> `data_platform` rename caused).
    Every process that attaches this catalog runs with cwd = project root (see
    `_run`'s cwd and the Makefile), so the relative path resolves the same
    everywhere. If the data dir is pointed outside the project via an absolute
    PLATFORM_DATA_DIR, it stays absolute, because there is no relative form then.
    """
    paths.ensure()
    try:
        data_path = paths.ducklake_data.relative_to(paths.root).as_posix()
    except ValueError:
        data_path = str(paths.ducklake_data)
    connection = duckdb.connect()
    try:
        connection.execute("INSTALL ducklake")
        connection.execute("LOAD ducklake")
        connection.execute(
            f"ATTACH IF NOT EXISTS 'ducklake:{paths.ducklake_catalog}' AS lake "
            f"(DATA_PATH '{data_path}/')"
        )
        connection.execute("CREATE SCHEMA IF NOT EXISTS lake.raw")
    finally:
        connection.close()


def stage_ingest(paths: Paths, settings: Settings, seed: bool = True) -> None:
    """Promote the inbox into the immutable raw zone, then run Meltano EL."""
    if seed:
        seeded = seed_inbox(paths)
        if seeded:
            _log("ingest", f"seeded {seeded} tracked file(s) into {paths.inbox}")

    summary = promote_inbox(paths, verbose=False)
    _log(
        "ingest",
        "raw zone: promoted={promoted} revised={revised} unchanged={unchanged} "
        "skipped={skipped} rejected={rejected}".format(**summary),
    )

    document_count = sum(1 for _ in paths.raw_documents.rglob("*") if _.is_file())
    if document_count == 0:
        raise SystemExit(
            f"[ingest] {paths.raw_documents} is empty. Drop a .md or .py file into "
            f"{paths.inbox} and run again."
        )

    ensure_ducklake(paths)
    _run(
        [
            _console_script("meltano"),
            "--environment", settings.node_role,
            "run", "el-inbox",
        ],
        env=_child_env(paths, settings),
        stage="ingest",
    )


def stage_transform(paths: Paths, settings: Settings) -> None:
    """SQLMesh bronze -> silver -> gold. Blocking audits gate the promotion."""
    ensure_ducklake(paths)
    env = _child_env(paths, settings)
    base = [_console_script("sqlmesh"), "-p", "transform", "--gateway", settings.sqlmesh_gateway]

    # Step 1 applies any model-definition changes and, on a fresh checkout,
    # creates the `prod` environment. It must come first: `--restate-model`
    # fails with "Environment 'prod' must exist for this operation" when there
    # is no state yet.
    _run(base + ["plan", "--auto-apply", "--no-prompts"], env=env, stage="transform")

    # Step 2 restates the bronze models, which clears their intervals and
    # re-executes them together with everything downstream. That is what makes
    # a build always rebuild gold from whatever just landed in `lake.raw.*` and
    # always re-run the audits.
    #
    # Neither alternative does that. Step 1 alone is a no-op once no model
    # definition has changed, and `run --ignore-cron` only fills *missing*
    # intervals, so a second build inside the same cron window would silently
    # skip both the refresh and the quality gate.
    restate: list = []
    for model in ("bronze.documents", "bronze.chunks", "bronze.relations"):
        restate.extend(["--restate-model", model])
    _run(
        base + ["plan", "--auto-apply", "--no-prompts"] + restate,
        env=env,
        stage="transform",
    )


def stage_index(paths: Paths, settings: Settings) -> None:
    """Build the single-file serving index: vectors, keywords, and graph."""
    from pipeline.build_graph import build_graph
    from pipeline.build_rag import build_index
    from pipeline.export import export_chunks

    rag = build_index(paths=paths, settings=settings)
    _log(
        "index",
        "rag: {chunks} chunk(s), {dimensions}-dim, provider={provider}".format(**rag),
    )
    graph = build_graph(paths=paths)
    _log("index", "graph: {nodes} node(s), {edges} edge(s)".format(**graph))

    size_mb = paths.index_sqlite.stat().st_size / (1024 * 1024)
    _log("index", f"wrote {paths.index_sqlite} ({size_mb:.2f} MiB)")

    # Both the index and this export are projections of the same gold table, so
    # they are rebuilt together and cannot drift apart.
    exported = export_chunks(paths=paths)
    _log("index", "export: {documents} document(s), {chunks} chunk(s) -> {directory}".format(**exported))


def stage_sync(paths: Paths, settings: Settings) -> None:
    """Data-plane sync. DATA_REMOTE=local_only makes this an explicit no-op."""
    remote = settings.data_remote.strip()
    if remote == "local_only":
        _log(
            "sync",
            "DATA_REMOTE=local_only: data/ stays on this disk. "
            "Set DATA_REMOTE to an s3:// URI in .env to enable sync.",
        )
        return

    if not remote.startswith("s3://"):
        raise SystemExit(
            f"[sync] DATA_REMOTE must be 'local_only' or an s3:// URI, got {remote!r}"
        )

    if shutil.which("dvc") is None:
        raise SystemExit(
            "[sync] DATA_REMOTE is set to an S3 URI but `dvc` is not installed. "
            "Install it with: uv add dvc[s3]"
        )

    env = _child_env(paths, settings)
    _run(["dvc", "remote", "add", "--default", "--force", "origin", remote], env=env, stage="sync")
    _run(["dvc", "push"], env=env, stage="sync")


def build(
    paths: Paths | None = None,
    settings: Settings | None = None,
    only: Sequence[str] | None = None,
    seed: bool = True,
) -> int:
    """Run the pipeline end to end (or just the named stages)."""
    paths = (paths or get_paths()).ensure()
    settings = settings or get_settings()
    selected = tuple(only) if only else STAGES

    for stage in selected:
        if stage not in STAGES:
            raise SystemExit(f"unknown stage {stage!r}; choose from {', '.join(STAGES)}")

    started = time.monotonic()
    _log("run", f"node_role={settings.node_role} gateway={settings.sqlmesh_gateway} stages={','.join(selected)}")

    if "ingest" in selected:
        stage_ingest(paths, settings, seed=seed)
    if "transform" in selected:
        stage_transform(paths, settings)
    if "index" in selected:
        stage_index(paths, settings)
    if "sync" in selected:
        stage_sync(paths, settings)

    _log("run", f"completed in {time.monotonic() - started:.1f}s")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the local-first data pipeline.")
    parser.add_argument(
        "--only",
        nargs="+",
        choices=STAGES,
        help="Run only the named stages, in the given order.",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Do not copy the tracked seeds (source/ + fixtures) into the inbox.",
    )
    args = parser.parse_args(argv)
    return build(only=args.only, seed=not args.no_seed)


if __name__ == "__main__":
    raise SystemExit(main())
