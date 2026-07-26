"""Report the content-duplicate renditions that silver.documents collapses.

Read-only visibility for the cross-format dedup. Today (an HWP-only corpus) it
prints nothing, because no two documents share a content fingerprint. Its purpose
is the moment PDF parsing is enabled: run it against a corpus that contains an
HWP and its PDF twin and it shows whether the twin actually merged.

A silent EMPTY report after PDFs are added is the failure signal -- it means the
two renditions produced different fingerprints (page furniture, OCR drift) and
were double-indexed rather than collapsed. That is the gate described in
AGENTS.md: strengthen `normalize_for_fingerprint` or escalate to MinHash before
the PDF renditions can be trusted.

The query reads bronze.documents (pre-dedup) so it can SEE the collision that
silver then resolves; silver keeps only one row per fingerprint, so querying it
would always look clean.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline import get_paths
from pipeline.build_rag import open_lake
from pipeline.chunking import (
    FINGERPRINT_MIN_CHARS,
    FORMAT_PRIORITY,
    normalize_for_fingerprint,
)
from pipeline.extract import ExtractionError, extract_text

# The originals a PDF could be a rendition OF, highest authority first. Shared
# with seed_inbox rather than restated, so the twin the seeder keeps is always
# the twin this tool measures against.
TWIN_ORIGINALS = tuple(f".{suffix}" for suffix in FORMAT_PRIORITY if suffix != "pdf")
TWIN_FORMATS = frozenset(TWIN_ORIGINALS) | {".pdf"}


def main() -> int:
    paths = get_paths()
    lake = open_lake(paths)
    try:
        rows = lake.execute(
            """
            SELECT content_fingerprint,
                   COUNT(*) AS n,
                   LIST(rel_path ORDER BY rel_path) AS paths
            FROM lake.bronze.documents
            WHERE LENGTH(TRIM(content)) >= ?
            GROUP BY content_fingerprint
            HAVING COUNT(*) > 1
            ORDER BY n DESC, content_fingerprint
            """,
            [FINGERPRINT_MIN_CHARS],
        ).fetchall()
    finally:
        lake.close()

    if not rows:
        print(
            "[dupes] no content-duplicate renditions above the "
            f"{FINGERPRINT_MIN_CHARS}-char floor. "
            "If PDF twins are present and expected to merge, this empty result "
            "means the fingerprints diverged -- see AGENTS.md."
        )
        return 0

    collapsed = sum(int(n) - 1 for _fp, n, _paths in rows)
    print(f"[dupes] {len(rows)} content group(s), {collapsed} rendition(s) collapsed by silver.documents:")
    for fingerprint, n, paths_list in rows:
        print(f"  {fingerprint[:12]}  x{n}  {list(paths_list)}")
    return 0


def _twin_pairs(root) -> list:
    """(stem, original, derived) for every same-name pair of formats in source/.

    Pairs an authoritative rendition with a derived one by FILE STEM, which is
    how the corpus actually stores twins: `01_법안.hwp` beside `01_법안.pdf`.
    """
    by_stem: dict = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in TWIN_FORMATS:
            by_stem.setdefault(str(path.with_suffix("")), {})[path.suffix.lower()] = path

    pairs: list = []
    for stem, formats in sorted(by_stem.items()):
        original = next((formats[s] for s in TWIN_ORIGINALS if s in formats), None)
        derived = formats.get(".pdf")
        if original is not None and derived is not None:
            pairs.append((Path(stem).name, original, derived))
    return pairs


def _first_divergence(left: str, right: str) -> str:
    """Where two normalised texts first differ, with a little context each side."""
    limit = min(len(left), len(right))
    cut = next((i for i in range(limit) if left[i] != right[i]), limit)
    return (
        f"      first differs at char {cut} of {len(left)}/{len(right)}\n"
        f"        original: {left[max(0, cut - 30):cut + 50]!r}\n"
        f"        derived : {right[max(0, cut - 30):cut + 50]!r}"
    )


def report_twins() -> int:
    """The measurement AGENTS.md requires before `.pdf` joins BINARY_SUFFIXES.

    `make dupes` reads bronze.documents, so it can only see formats the tap
    already ingests -- it cannot answer "would a PDF collapse onto its HWP twin"
    until PDF is switched on, by which point the corpus is already double-indexed
    if the answer is no. This mode closes that circle: it extracts both twins
    straight from `source/` and compares the fingerprints, touching no pipeline
    state at all.

    The measured answer on this corpus is 0 of 10, and it is not a bug to fix by
    normalising harder: hwpkit and pypdf agree on a bill's characters and
    disagree on their order, because each linearises the cover-page 의안번호 table
    at a different point. Sorting the characters to sidestep that reached 7 of 10
    and was REJECTED -- an order-insensitive key makes any anagram a duplicate,
    which is too weak for a key that decides what gets indexed.

    So the curated corpus is protected upstream instead, by seeding one rendition
    per twin set (`watcher._superseded_renditions`), and this stays a diagnostic:
    it reports the fingerprint verdict AND which rendition the seeder keeps. Exit
    is non-zero only when a twin cannot be read at all, which is a real fault.
    """
    root = get_paths().root / "source"
    if not root.is_dir():
        print(f"[twins] no {root} directory; nothing to measure")
        return 0

    pairs = _twin_pairs(root)
    if not pairs:
        print("[twins] no original/PDF twin pairs found in source/")
        return 0

    matched: list = []
    diverged: list = []
    unreadable: list = []
    for stem, original, derived in pairs:
        try:
            left = normalize_for_fingerprint(extract_text(original.name, original.read_bytes()))
            right = normalize_for_fingerprint(extract_text(derived.name, derived.read_bytes()))
        except ExtractionError as error:
            unreadable.append((stem, error))
            continue
        (matched if left == right else diverged).append((stem, left, right))

    total = len(pairs)
    print(f"[twins] {len(matched)}/{total} pair(s) share a fingerprint after normalisation")
    for stem, original, _derived in pairs:
        print(f"  seeded: {original.name}")
    for stem, _left, _right in matched:
        print(f"  MATCH     {stem}")
    for stem, left, right in diverged:
        print(f"  DIVERGED  {stem}")
        print(_first_divergence(left, right))
    for stem, error in unreadable:
        print(f"  UNREADABLE {stem}: {error}")

    if unreadable:
        print(f"\n[twins] FAIL: {len(unreadable)} twin(s) could not be extracted at all.")
        return 1
    if diverged:
        print(
            f"\n[twins] {len(diverged)} pair(s) do NOT share a fingerprint. That is "
            "EXPECTED here and is why seed_inbox seeds one rendition per twin set: "
            "the derived PDFs above never enter the pipeline, so they cannot be "
            "double-indexed. Content dedup still guards twins dropped straight into "
            "the inbox, where no curation is known -- check those with `make dupes`."
        )
        return 0
    print("\n[twins] every PDF also collapses onto its original by content.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--twins",
        action="store_true",
        help="Measure source/ twin pairs directly instead of reading the lake.",
    )
    arguments = parser.parse_args()
    raise SystemExit(report_twins() if arguments.twins else main())
