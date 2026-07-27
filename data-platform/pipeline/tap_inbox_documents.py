"""Meltano SDK extractor for the immutable `data/raw/documents` landing zone.

FIRST_SOURCE = inbox_documents. `pipeline/watcher.py` promotes files from
`data/inbox/documents/` into `data/raw/documents/`; this tap only ever reads
from `data/raw` and never writes there, which keeps the raw zone immutable.

Three Singer streams are emitted, mirroring the gold layer the index needs:
  documents  - one record per source file, shape preserved
  chunks     - retrievable units produced by `pipeline.chunking`
  relations  - directed edges asserted by the document

Registered in meltano.yml as `tap-inbox-documents` with
`executable: tap-inbox-documents`, which resolves to the console script declared
in pyproject.toml (`pipeline.tap_inbox_documents:TapInboxDocuments.cli`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from singer_sdk import Stream, Tap
from singer_sdk import typing as th

from pipeline.chunking import SUPPORTED_SUFFIXES, parse_document

DEFAULT_RAW_DIR = "data/raw/documents"


def _project_root() -> Path:
    """Meltano runs plugins from the project root and exports it explicitly."""
    root = os.environ.get("MELTANO_PROJECT_ROOT")
    return Path(root) if root else Path.cwd()


def _resolve(raw_dir: str) -> Path:
    path = Path(raw_dir)
    return path if path.is_absolute() else (_project_root() / path)


class _InboxStreamBase(Stream):
    """Shared file walk so the three streams stay consistent with each other.

    Deliberately full-table rather than incremental. The raw zone is small,
    immutable and local, so re-reading all of it is cheap; and an incremental
    bookmark here would be decorative at best, because `ingested_at` is stamped
    at read time and would therefore always clear its own high-water mark. The
    loader truncates and reloads each stream to match.
    """

    @property
    def raw_dir(self) -> Path:
        return _resolve(self.config.get("raw_dir", DEFAULT_RAW_DIR))

    @property
    def suffixes(self) -> frozenset:
        configured = self.config.get("file_extensions")
        if not configured:
            return SUPPORTED_SUFFIXES
        return frozenset(
            suffix if suffix.startswith(".") else f".{suffix}" for suffix in configured
        )

    def parsed_documents(self) -> Iterable:
        root = self.raw_dir
        if not root.exists():
            self.logger.warning("raw_dir %s does not exist; emitting no records", root)
            return
        ingested_at = datetime.now(timezone.utc).isoformat()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in self.suffixes:
                continue
            rel_path = path.relative_to(root).as_posix()
            yield parse_document(rel_path, path.read_bytes()), path, ingested_at


class DocumentsStream(_InboxStreamBase):
    name = "documents"
    primary_keys = ("doc_id",)
    schema = th.PropertiesList(
        th.Property("doc_id", th.StringType, required=True),
        th.Property("rel_path", th.StringType, required=True),
        th.Property("doc_type", th.StringType, required=True),
        th.Property("title", th.StringType),
        th.Property("content", th.StringType),
        th.Property("content_sha256", th.StringType, required=True),
        th.Property("content_fingerprint", th.StringType, required=True),
        th.Property("byte_size", th.IntegerType),
        th.Property("source_modified_at", th.DateTimeType),
        th.Property("ingested_at", th.DateTimeType),
    ).to_dict()

    def get_records(self, context: dict | None) -> Iterable[dict]:
        for parsed, path, ingested_at in self.parsed_documents():
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            yield {
                "doc_id": parsed.doc_id,
                "rel_path": parsed.rel_path,
                "doc_type": parsed.doc_type,
                "title": parsed.title,
                "content": parsed.content,
                "content_sha256": parsed.content_sha256,
                "content_fingerprint": parsed.content_fingerprint,
                "byte_size": parsed.byte_size,
                "source_modified_at": modified.isoformat(),
                "ingested_at": ingested_at,
            }


class ChunksStream(_InboxStreamBase):
    name = "chunks"
    primary_keys = ("chunk_id",)
    schema = th.PropertiesList(
        th.Property("chunk_id", th.StringType, required=True),
        th.Property("doc_id", th.StringType, required=True),
        th.Property("rel_path", th.StringType),
        th.Property("title", th.StringType),
        th.Property("doc_type", th.StringType),
        th.Property("chunk_index", th.IntegerType),
        th.Property("heading", th.StringType),
        th.Property("content", th.StringType),
        th.Property("char_start", th.IntegerType),
        th.Property("char_end", th.IntegerType),
        th.Property("token_estimate", th.IntegerType),
        th.Property("content_sha256", th.StringType),
        th.Property("ingested_at", th.DateTimeType),
    ).to_dict()

    def get_records(self, context: dict | None) -> Iterable[dict]:
        for parsed, _path, ingested_at in self.parsed_documents():
            for chunk in parsed.chunks:
                yield {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "rel_path": parsed.rel_path,
                    "title": parsed.title,
                    "doc_type": parsed.doc_type,
                    "chunk_index": chunk.chunk_index,
                    "heading": chunk.heading,
                    "content": chunk.content,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "token_estimate": chunk.token_estimate,
                    "content_sha256": parsed.content_sha256,
                    "ingested_at": ingested_at,
                }


class RelationsStream(_InboxStreamBase):
    name = "relations"
    primary_keys = ("relation_id",)
    schema = th.PropertiesList(
        th.Property("relation_id", th.StringType, required=True),
        th.Property("doc_id", th.StringType, required=True),
        th.Property("rel_path", th.StringType),
        th.Property("source_entity", th.StringType, required=True),
        th.Property("source_kind", th.StringType),
        th.Property("relation", th.StringType, required=True),
        th.Property("target_entity", th.StringType, required=True),
        th.Property("target_kind", th.StringType),
        th.Property("evidence", th.StringType),
        th.Property("ingested_at", th.DateTimeType),
    ).to_dict()

    def get_records(self, context: dict | None) -> Iterable[dict]:
        for parsed, _path, ingested_at in self.parsed_documents():
            for relation in parsed.relations:
                yield {
                    "relation_id": relation.relation_id,
                    "doc_id": relation.doc_id,
                    "rel_path": parsed.rel_path,
                    "source_entity": relation.source_entity,
                    "source_kind": relation.source_kind,
                    "relation": relation.relation,
                    "target_entity": relation.target_entity,
                    "target_kind": relation.target_kind,
                    "evidence": relation.evidence,
                    "ingested_at": ingested_at,
                }


class TapInboxDocuments(Tap):
    """Singer tap over the immutable raw document zone."""

    name = "tap-inbox-documents"

    config_jsonschema = th.PropertiesList(
        th.Property(
            "raw_dir",
            th.StringType,
            default=DEFAULT_RAW_DIR,
            description="Immutable raw zone to read. Relative paths resolve against the Meltano project root.",
        ),
        th.Property(
            "file_extensions",
            th.ArrayType(th.StringType),
            description="File suffixes to ingest. Defaults to the suffixes pipeline.chunking can parse.",
        ),
    ).to_dict()

    def discover_streams(self) -> list:
        return [
            DocumentsStream(tap=self),
            ChunksStream(tap=self),
            RelationsStream(tap=self),
        ]


if __name__ == "__main__":
    TapInboxDocuments.cli()
