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

from pipeline import get_paths
from pipeline.build_rag import open_lake
from pipeline.chunking import FINGERPRINT_MIN_CHARS


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


if __name__ == "__main__":
    raise SystemExit(main())
