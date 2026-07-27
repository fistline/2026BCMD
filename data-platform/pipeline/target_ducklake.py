"""Meltano SDK loader that lands Singer streams directly in DuckLake.

Why this exists rather than an off-the-shelf loader
---------------------------------------------------
There is no Singer target for DuckLake on PyPI. The documented fallback is to
land via `target-duckdb` and let SQLMesh promote the result, but target-duckdb
0.8.0 loads batches with `COPY <tbl> FROM '<file>.csv'` and never disables CSV
header detection. Against DuckDB >= 1.x the sniffer classifies the first data
row as a header, so the first record of every stream is silently discarded
(reproduced here: 7 documents in, 6 landed). Silent row loss upstream of a RAG
index is not an acceptable failure mode, so this project ships its own loader
built on the documented Meltano SDK `BatchSink` API. See README "NOTE: loader".

Load semantics: full refresh per run. The tap always re-reads the whole
immutable raw zone, so each stream's table is dropped and recreated once per
process, then filled. That is what makes the pipeline idempotent: running it
twice produces exactly the same tables, never duplicates.

The DuckLake catalog itself is created and owned by `pipeline/run.py`; this
loader only attaches to it, so the DATA_PATH is declared in exactly one place.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from pathlib import Path

import duckdb
from singer_sdk import Sink, Target
from singer_sdk import typing as th
from singer_sdk.sinks import BatchSink

DEFAULT_CATALOG_PATH = "data/processed/catalog.ducklake"
DEFAULT_TARGET_SCHEMA = "raw"
CATALOG_ALIAS = "lake"

_CONNECTIONS: dict = {}
_INITIALISED_TABLES: set = set()
# One DuckDB connection may only be used by one thread at a time, and DuckLake
# refuses a second attach of a catalog already attached in the process. The SDK
# drains sinks concurrently, so both the attach and the writes are serialised.
_LOCK = threading.RLock()


def _project_root() -> Path:
    root = os.environ.get("MELTANO_PROJECT_ROOT")
    return Path(root) if root else Path.cwd()


def _resolve(path_value: str) -> Path:
    path = Path(path_value)
    return path.resolve() if path.is_absolute() else (_project_root() / path).resolve()


def _connect(catalog_path: Path) -> duckdb.DuckDBPyConnection:
    """One DuckLake connection per catalog, shared by every sink in the process."""
    key = str(catalog_path)
    with _LOCK:
        connection = _CONNECTIONS.get(key)
        if connection is not None:
            return connection

        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect()
        connection.execute("INSTALL ducklake")
        connection.execute("LOAD ducklake")
        # No DATA_PATH here on purpose: run.py creates the catalog and fixes the
        # data path once. Re-declaring it from a second process is how DuckLake
        # ends up rejecting an attach for a path mismatch.
        connection.execute(f"ATTACH 'ducklake:{catalog_path}' AS {CATALOG_ALIAS}")
        _CONNECTIONS[key] = connection
        atexit.register(connection.close)
        return connection


def duckdb_type(property_schema: dict) -> str:
    """Map one JSON Schema property to a DuckDB column type."""
    declared = property_schema.get("type")
    if isinstance(declared, str):
        declared = [declared]
    concrete = [entry for entry in (declared or []) if entry != "null"]
    kind = concrete[0] if concrete else "string"
    fmt = property_schema.get("format")

    if kind == "integer":
        return "BIGINT"
    if kind == "number":
        return "DOUBLE"
    if kind == "boolean":
        return "BOOLEAN"
    if kind == "string":
        if fmt == "date-time":
            return "TIMESTAMP"
        if fmt == "date":
            return "DATE"
        if fmt == "time":
            return "TIME"
        return "VARCHAR"
    # Objects and arrays are landed as JSON text; bronze can unpack them in SQL.
    return "VARCHAR"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class DuckLakeSink(BatchSink):
    """Writes one Singer stream into one DuckLake table."""

    max_size = 10000

    @property
    def catalog_path(self) -> Path:
        return _resolve(self.config.get("catalog_path", DEFAULT_CATALOG_PATH))

    @property
    def target_schema(self) -> str:
        return self.config.get("target_schema", DEFAULT_TARGET_SCHEMA)

    @property
    def table_name(self) -> str:
        return self.stream_name.split("-")[-1]

    @property
    def qualified_table(self) -> str:
        return ".".join(
            (_quote(CATALOG_ALIAS), _quote(self.target_schema), _quote(self.table_name))
        )

    @property
    def columns(self) -> list:
        """Column order, fixed by the Singer schema so INSERT stays positional."""
        return list(self.schema.get("properties", {}).keys())

    def _ensure_table(self, connection: duckdb.DuckDBPyConnection) -> None:
        """Drop and recreate this stream's table once per process (full refresh).

        Called under `_LOCK`, so two sinks draining at once cannot both decide
        the table is missing and race each other through DROP/CREATE.
        """
        key = (str(self.catalog_path), self.target_schema, self.table_name)
        if key in _INITIALISED_TABLES:
            return

        properties = self.schema.get("properties", {})
        column_ddl = ", ".join(
            f"{_quote(name)} {duckdb_type(properties[name])}" for name in self.columns
        )
        connection.execute(
            f"CREATE SCHEMA IF NOT EXISTS {_quote(CATALOG_ALIAS)}.{_quote(self.target_schema)}"
        )
        connection.execute(f"DROP TABLE IF EXISTS {self.qualified_table}")
        connection.execute(f"CREATE TABLE {self.qualified_table} ({column_ddl})")
        _INITIALISED_TABLES.add(key)
        self.logger.info("Prepared %s (%d columns)", self.qualified_table, len(self.columns))

    def _encode(self, value):
        """Objects and arrays become JSON text; everything else passes through."""
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return value

    def process_batch(self, context: dict) -> None:
        records = context.get("records") or []
        if not records:
            return

        connection = _connect(self.catalog_path)

        properties = self.schema.get("properties", {})
        column_list = ", ".join(_quote(name) for name in self.columns)
        # Explicit CAST per column: DuckDB will not implicitly coerce an ISO-8601
        # string parameter into a TIMESTAMP column, and a silent NULL there would
        # break the "latest batch" filter every silver model depends on.
        value_list = ", ".join(
            f"CAST(? AS {duckdb_type(properties[name])})" for name in self.columns
        )
        statement = f"INSERT INTO {self.qualified_table} ({column_list}) VALUES ({value_list})"
        rows = [[self._encode(record.get(name)) for name in self.columns] for record in records]

        with _LOCK:
            self._ensure_table(connection)
            connection.executemany(statement, rows)
        self.logger.info("Loaded %d row(s) into %s", len(rows), self.qualified_table)


class TargetDuckLake(Target):
    """Project-local Singer target for DuckLake. Not a Meltano Hub plugin."""

    name = "target-ducklake-local"
    default_sink_class = DuckLakeSink

    config_jsonschema = th.PropertiesList(
        th.Property(
            "catalog_path",
            th.StringType,
            default=DEFAULT_CATALOG_PATH,
            description="DuckLake catalog file. Relative paths resolve against the Meltano project root.",
        ),
        th.Property(
            "target_schema",
            th.StringType,
            default=DEFAULT_TARGET_SCHEMA,
            description="Schema inside the DuckLake catalog that raw streams land in.",
        ),
        th.Property(
            "add_record_metadata",
            th.BooleanType,
            default=False,
            description="Add Singer _sdc_* columns. Off by default: the tap already carries ingested_at.",
        ),
    ).to_dict()

    @property
    def max_parallelism(self) -> int:
        """Drain one sink at a time.

        A single DuckDB connection backs every stream, so parallel drains would
        contend on it for no benefit: the bottleneck here is disk, not CPU.
        """
        return 1

    def get_sink_class(self, stream_name: str) -> type:
        return DuckLakeSink


__all__ = ["DuckLakeSink", "Sink", "TargetDuckLake", "duckdb_type"]


if __name__ == "__main__":
    TargetDuckLake.cli()
