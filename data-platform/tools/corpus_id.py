"""Which documents was this measured on? A short, deterministic answer.

A number in MEASUREMENTS.md is only comparable to another number if both were taken
over the same documents, and until now no row said which. That is not a theoretical
gap: `M:cold-rebuild` (1433.9 s over 12 643 vectors) was compared against a fresh
1948.2 s over 19 808 vectors in this repo, and the comparison was only sound because
whoever ran it happened to know the two shared a document set. Nothing recorded it.

The identity material already exists -- `source/CORPUS_MANIFEST.tsv` carries a
sha256 per file, which is exactly what Croissant recommends for a versioned dataset
and what DVC's lock file does for a pipeline stage. This just folds those hashes
into one short id so a row can name it.

WHAT IT COVERS: the document SET -- which files, and their contents. Two corpora
with the same id hold the same documents.

WHAT IT DOES NOT COVER: how those documents were cut up or encoded. Chunk size,
the embedder, the batch and the token cap all change the index without changing the
corpus, and they have their own identity in `index_signature`. A row whose value
depends on the shape should say so in its own text (most already state a chunk or
vector count).

    uv run python tools/corpus_id.py        # or plain python3; reads one TSV
    make corpus-id
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "source" / "CORPUS_MANIFEST.tsv"

# Short enough to sit in a table cell, long enough that a collision would be news.
DIGITS = 12
PREFIX = "c:"

# A row recorded before this tool existed cannot have its corpus recovered -- the
# documents may since have changed. Saying so is honest; guessing is a fabricated
# provenance, which is worse than a blank.
UNKNOWN = "unknown"


def corpus_id(manifest: Path = MANIFEST, verify: bool = True) -> str:
    """`c:` + the first DIGITS hex of sha256 over the sorted (path, sha256) pairs.

    Only the identity columns feed the hash. `bytes` is derivable from the content
    and `source` is provenance prose, so editing either must not change the id --
    otherwise fixing a URL typo would silently orphan every measurement.

    VERIFIED AGAINST THE FILES before an id is issued, because a manifest is a
    DECLARATION and this one was wrong within the week: it described 48 of 70 files
    while claiming to describe all of them (3d31ee3), and five rows credited a
    publisher that never published those documents (87c1ecd). An id minted from a
    manifest that does not match the bytes on disk would be a fabricated provenance
    wearing a checksum, which is worse than no id at all. Pass verify=False only
    where the files are legitimately absent (a spoke that received `data/` but not
    `source/`), and understand that the answer is then the declared corpus, not the
    real one.
    """
    rows = []
    with manifest.open(encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        try:
            collection = header.index("collection")
            filename = header.index("filename")
            sha = header.index("sha256")
        except ValueError as error:
            raise ValueError(f"{manifest} is missing an identity column: {error}") from error
        for line in handle:
            if not line.strip():
                continue
            cells = line.rstrip("\n").split("\t")
            if len(cells) <= max(collection, filename, sha):
                continue
            rows.append(f"{cells[collection]}/{cells[filename]}\t{cells[sha]}")

    if not rows:
        raise ValueError(f"{manifest} lists no documents")

    if verify:
        disagreements = []
        for row in rows:
            relative, declared = row.split("\t")
            path = manifest.parent / relative
            if not path.is_file():
                disagreements.append(f"{relative}: declared but not on disk")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if not actual.startswith(declared):
                disagreements.append(f"{relative}: declared {declared}, file is {actual[:len(declared)]}")
        if disagreements:
            raise ValueError(
                f"the manifest does not describe the files ({len(disagreements)} disagreement(s)); "
                f"no id issued: " + "; ".join(disagreements[:4])
            )

    running = hashlib.sha256("\n".join(sorted(rows)).encode("utf-8"))
    return PREFIX + running.hexdigest()[:DIGITS]


def main() -> int:
    if not MANIFEST.exists():
        print(f"FAIL: no manifest at {MANIFEST}")
        return 1
    try:
        identifier = corpus_id()
    except ValueError as error:
        print(f"FAIL: {error}")
        return 1
    count = sum(1 for line in MANIFEST.read_text(encoding="utf-8").split("\n")[1:] if line.strip())
    print(identifier)
    print(f"  {count} document(s) in {MANIFEST.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
