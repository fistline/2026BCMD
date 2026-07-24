"""Schema-validated JSON export of the gold layer, one file per document.

`data/processed/chunks/<doc_id>.json` is a first-class pipeline artefact: it has
a declared schema, a standard location under the regenerable part of the data
plane, and it is rebuilt on every build so it can never drift from what is
served.

Direction matters. This is derived FROM `lake.gold.chunks`, it is not an input
TO the index. Both the JSON and `data/serving/index.sqlite` are projections of
the same gold table, so they cannot disagree, and every blocking SQLMesh audit
still gates everything downstream of it. Feeding hand-made JSON into the indexer
instead would route around bronze, silver, gold and all nine audit sets.

Nothing here writes a timestamp. An export that embedded the run time would
differ on every build, which would make the data plane permanently dirty and
would defeat the reproducibility check.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from typing import Optional

from pydantic import BaseModel, Field

from pipeline import Paths, get_paths
from pipeline.build_rag import open_lake

SCHEMA_VERSION = "1"


class ChunkOut(BaseModel):
    """One retrievable unit, exactly as the index holds it."""

    chunk_id: str
    doc_id: str
    chunk_index: int
    heading: str = ""
    content: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    token_estimate: Optional[int] = None
    content_sha256: Optional[str] = None


class DocumentChunks(BaseModel):
    """Every chunk of one document, plus the provenance needed to trace it."""

    schema_version: str = Field(default=SCHEMA_VERSION)
    doc_id: str
    rel_path: str
    doc_type: str
    title: Optional[str] = None
    chunk_count: int
    # Typed on purpose. A bare `list` would make model_validate_json return
    # plain dicts, so a malformed or truncated export would load without
    # complaint and the schema would be decorative.
    chunks: list[ChunkOut]


def export_chunks(paths: Optional[Paths] = None) -> dict:
    """Rewrite data/processed/chunks/ from gold. Idempotent by construction.

    The directory is emptied first, so a document removed from the raw zone
    leaves no ghost file behind and the export stays a pure function of gold.
    """
    paths = (paths or get_paths()).ensure()
    target = paths.processed / "chunks"

    lake = open_lake(paths)
    try:
        rows = lake.execute(
            """
            SELECT doc_id, rel_path, doc_type, title, chunk_id, chunk_index,
                   heading, content, char_start, char_end, token_estimate, content_sha256
            FROM lake.gold.chunks
            ORDER BY doc_id, chunk_index
            """
        ).fetchall()
    finally:
        lake.close()

    if not rows:
        raise RuntimeError(
            "lake.gold.chunks is empty; run the ingest and transform stages first."
        )

    documents: dict = {}
    for row in rows:
        doc_id = row[0]
        entry = documents.setdefault(
            doc_id,
            {"doc_id": doc_id, "rel_path": row[1], "doc_type": row[2], "title": row[3], "chunks": []},
        )
        entry["chunks"].append(
            ChunkOut(
                chunk_id=row[4],
                doc_id=doc_id,
                chunk_index=row[5],
                heading=row[6] or "",
                content=row[7],
                char_start=row[8],
                char_end=row[9],
                token_estimate=row[10],
                content_sha256=row[11],
            )
        )

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    for doc_id in sorted(documents):
        entry = documents[doc_id]
        document = DocumentChunks(
            doc_id=entry["doc_id"],
            rel_path=entry["rel_path"],
            doc_type=entry["doc_type"],
            title=entry["title"],
            chunk_count=len(entry["chunks"]),
            chunks=entry["chunks"],
        )
        payload = json.dumps(
            document.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
        )
        # Atomic write: a reader never sees a half-written export, and an
        # interrupted build leaves the previous file intact.
        temporary = target / f".{doc_id}.json.tmp"
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, target / f"{doc_id}.json")

    return {
        "documents": len(documents),
        "chunks": len(rows),
        "directory": str(target),
    }


def load_document(doc_id: str, paths: Optional[Paths] = None) -> DocumentChunks:
    """Read one exported document back, validating it against the schema."""
    paths = paths or get_paths()
    path = paths.processed / "chunks" / f"{doc_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist. Build it with `make build`.")
    return DocumentChunks.model_validate_json(path.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--doc-id", help="Print one exported document instead of rebuilding.")
    args = parser.parse_args(argv)

    if args.doc_id:
        document = load_document(args.doc_id)
        print(json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0

    summary = export_chunks()
    print(
        "[export] {documents} document(s), {chunks} chunk(s) -> {directory}".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
