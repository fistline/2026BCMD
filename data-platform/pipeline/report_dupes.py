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
import re
import unicodedata
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

# ---- Fuzzy-twin detector parameters -------------------------------------------
# These MUST match transform/models/silver/document_twins.sql (the live detector,
# when landed). This module is the detector's measurement harness: same gates,
# same normalisation, same threshold, computed in Python against source/ files so
# a regression is visible without touching lake state.
#   w=10 char shingles on the n2 norm; measured twin floor 0.910, worst
#   cross-format different-doc 0.610 -> T=0.85 sits inside a +0.30 margin.
#   Hard gates run BEFORE the threshold: format-different (the load-bearing one:
#   a 정정/개정/다른서류 arrives in the SAME format and must never reach Jaccard),
#   then length-ratio < 0.15 (a true rendition is ~length-identical).
TWIN_SHINGLE_W = 10
TWIN_JACCARD_T = 0.85
TWIN_LEN_RATIO = 0.15

# Recorded separation, measured 2026-07-27 on the 10 curated twin sets and the
# cross-format non-twin pairs that clear the length gate. `--controls` asserts
# these, which is the drift guard: shingles come out of the EXTRACTORS, so a
# pypdf or hwpkit upgrade can move the distribution T sits in, and T is a
# committed literal that will not move with it.
#
# Pinning the measurement rather than the extractor VERSION is deliberate. A
# version pin fires on every harmless patch bump and says nothing about whether
# the numbers actually moved; the separation is what T depends on. Versions are
# printed as provenance so a real drift can be attributed. Same discipline as
# eval_baseline.json: re-record only alongside a measurement that justifies it.
TWIN_POS_FLOOR = 0.910  # lowest Jaccard among true twins
TWIN_NEG_CEILING = 0.610  # highest Jaccard among cross-format different documents
TWIN_SEPARATION_TOLERANCE = 0.02


def _norm_n2(text: str) -> str:
    """NFC, then strip whitespace the way the SQL detector does.

    flags=re.ASCII on purpose: DuckDB's regexp_replace is RE2, whose \\s is the
    ASCII class. Python's default unicode \\s would ALSO eat U+3000/U+00A0 and
    the two sides would disagree on any document that uses ideographic spaces --
    which Korean filings do. Harness and detector must compute the same string,
    and the smoke test asserts that byte-for-byte.
    """
    return re.sub(r"\s", "", unicodedata.normalize("NFC", text), flags=re.ASCII)


def _shingles(norm: str) -> set:
    """Set of w-char shingles; empty for a string shorter than one shingle."""
    w = TWIN_SHINGLE_W
    return {norm[i : i + w] for i in range(len(norm) - w + 1)} if len(norm) >= w else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def twin_model_body() -> str:
    """The document_twins model SQL with its MODEL () header removed, or None.

    Returned so a harness can execute the LIVE model instead of a copy of it --
    a copy is exactly what stops catching regressions. The header ends at the
    first line that is only `);`; the description string CONTAINS the two
    characters `);`, so a plain substring split lands inside it.
    """
    model = get_paths().root / "transform" / "models" / "silver" / "document_twins.sql"
    if not model.exists():
        return None
    parts = re.split(r"^\);\s*$", model.read_text(encoding="utf-8"), maxsplit=1, flags=re.M)
    return parts[1] if len(parts) == 2 else None


def _extractor_provenance() -> str:
    """Versions the recorded separation was measured against, for attribution."""
    from importlib.metadata import version

    parts = []
    for package in ("hwpkit", "pypdf", "duckdb"):
        try:
            parts.append(f"{package} {version(package)}")
        except Exception:
            parts.append(f"{package} unknown")
    return ", ".join(parts)


def _twin_gates(suffix_a: str, suffix_b: str, len_a: int, len_b: int):
    """Why a pair is excluded before Jaccard, or None if it reaches the score."""
    if suffix_a == suffix_b:
        return "same-format"
    if len_a == 0 or len_b == 0:
        return "empty"
    if abs(len_a - len_b) / max(len_a, len_b) >= TWIN_LEN_RATIO:
        return "length-ratio"
    return None


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


def report_controls() -> int:
    """Negative-control harness for the fuzzy-twin detector (plan §5b-1).

    Every case here was measured to be correctly REJECTED before the detector
    was designed; this re-measures them from source/ so a change to the gates,
    the normalisation or the threshold that would start merging non-twins fails
    loudly. The positive control is the 10 curated hwp/pdf twin sets, which the
    detector must catch 10/10 -- they are exactly what the exact fingerprint
    was measured to miss (0/10).

    Prints 잔여 이중색인률 as a first-class number: of the known twin sets, how
    many would still double-index if they arrived through the inbox (no
    curation), i.e. after the fuzzy layer too.
    """
    root = get_paths().root / "source"
    if not root.is_dir():
        print(f"[controls] no {root} directory; nothing to measure")
        return 0

    def load(path: Path) -> tuple:
        # Binary formats go through the extractor; plain text is read as-is,
        # the same split parse_document makes.
        if path.suffix.lower() in TWIN_FORMATS:
            text = extract_text(path.name, path.read_bytes())
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        norm = _norm_n2(text)
        return norm, _shingles(norm)

    failures: list = []

    # -- positive: curated twin sets must all be caught by the fuzzy layer -----
    pairs = _twin_pairs(root)
    caught = 0
    positives: list = []
    print(f"[controls] positive: {len(pairs)} curated twin set(s)")
    for stem, original, derived in pairs:
        norm_a, sh_a = load(original)
        norm_b, sh_b = load(derived)
        gate = _twin_gates(original.suffix.lower(), derived.suffix.lower(), len(norm_a), len(norm_b))
        score = _jaccard(sh_a, sh_b)
        detected = gate is None and score >= TWIN_JACCARD_T
        caught += detected
        positives.append(score)
        print(f"  {'DETECT ' if detected else 'MISSED '} J={score:.3f} gate={gate or '-'}  {stem}")
        if not detected:
            failures.append(f"positive twin missed: {stem} (J={score:.3f}, gate={gate})")

    # -- negative 1: a 1-char correction (same format) must be format-gated ----
    # Synthesised, because that is what a 정정본 IS: the same file with one
    # character changed. Measured J 0.999 -- more similar than a real twin, which
    # is exactly why the FORMAT gate, not the threshold, has to carry this case.
    sample = root / "sto" / "증권신고서(투자계약증권)_열매컴퍼니_20250626000457.txt"
    if sample.exists():
        norm_a = _norm_n2(sample.read_text(encoding="utf-8", errors="replace"))
        norm_b = norm_a[:5000] + ("일" if norm_a[5000] != "일" else "이") + norm_a[5001:]
        gate = _twin_gates(".txt", ".txt", len(norm_a), len(norm_b))
        score = _jaccard(_shingles(norm_a), _shingles(norm_b))
        print(f"[controls] 정정본(합성 1글자): J={score:.3f} gate={gate} (기대: same-format)")
        if gate != "same-format":
            failures.append("correction pair was not format-gated")
    else:
        print("[controls] 정정본: sample filing absent -- skipped")

    # -- negative 2: cross-format DIFFERENT documents stay far below T ---------
    by_suffix: dict = {}
    for stem, original, derived in pairs:
        by_suffix.setdefault("orig", []).append((stem, original))
        by_suffix.setdefault("pdf", []).append((stem, derived))
    worst = 0.0
    worst_pair = "-"
    reached = 0
    for stem_a, path_a in by_suffix.get("orig", []):
        norm_a, sh_a = load(path_a)
        for stem_b, path_b in by_suffix.get("pdf", []):
            if stem_a == stem_b:
                continue
            norm_b, sh_b = load(path_b)
            if _twin_gates(path_a.suffix.lower(), path_b.suffix.lower(), len(norm_a), len(norm_b)):
                continue
            reached += 1
            score = _jaccard(sh_a, sh_b)
            if score > worst:
                worst, worst_pair = score, f"{stem_a} × {stem_b}"
            if score >= TWIN_JACCARD_T:
                failures.append(f"cross-format different docs would merge: {stem_a} × {stem_b} J={score:.3f}")
    print(
        f"[controls] cross-format 타문서: {reached} pair(s) reached Jaccard, "
        f"worst J={worst:.3f} ({worst_pair}) < T={TWIN_JACCARD_T}"
    )

    # -- negative 3: same-format different filings must be format-gated --------
    sto = root / "sto"
    filings = sorted(sto.glob("*.txt")) if sto.is_dir() else []
    if len(filings) >= 2:
        norm_a, sh_a = load(filings[0])
        norm_b, sh_b = load(filings[1])
        gate = _twin_gates(".txt", ".txt", len(norm_a), len(norm_b))
        score = _jaccard(sh_a, sh_b)
        print(f"[controls] 동종 타서류({filings[0].stem[:20]}… × {filings[1].stem[:20]}…): J={score:.3f} gate={gate}")
        if gate != "same-format":
            failures.append("same-format filings were not format-gated")

    # -- negative 4: 모법/시행령 (same format here) ----------------------------
    law = root / "norms" / "금융소비자 보호에 관한 법률.txt"
    decree = root / "norms" / "금융소비자 보호에 관한 법률 시행령.txt"
    if law.exists() and decree.exists():
        norm_a, sh_a = load(law)
        norm_b, sh_b = load(decree)
        gate = _twin_gates(".txt", ".txt", len(norm_a), len(norm_b))
        score = _jaccard(sh_a, sh_b)
        print(f"[controls] 모법/시행령: J={score:.3f} gate={gate} (본문이 달라 T 아래이기도 함)")
        if gate is None and score >= TWIN_JACCARD_T:
            failures.append("모법/시행령 would merge")

    # -- separation: the drift guard ------------------------------------------
    # Everything above is a pass/fail against T. This asserts the DISTRIBUTION T
    # sits in, which is what an extractor upgrade moves.
    if positives and worst_pair != "-":
        pos_floor = min(positives)
        tolerance = TWIN_SEPARATION_TOLERANCE
        print(
            f"\n[controls] separation: 양성 하한 {pos_floor:.3f} (기록 {TWIN_POS_FLOOR:.3f}) · "
            f"음성 상한 {worst:.3f} (기록 {TWIN_NEG_CEILING:.3f}) · "
            f"마진 {pos_floor - worst:+.3f} · T={TWIN_JACCARD_T}"
        )
        print(f"[controls] provenance: {_extractor_provenance()}")
        if pos_floor < TWIN_POS_FLOOR - tolerance:
            failures.append(
                f"twin floor drifted down: {pos_floor:.3f} < {TWIN_POS_FLOOR:.3f}-{tolerance}"
            )
        if worst > TWIN_NEG_CEILING + tolerance:
            failures.append(
                f"non-twin ceiling drifted up: {worst:.3f} > {TWIN_NEG_CEILING:.3f}+{tolerance}"
            )
        if not (worst < TWIN_JACCARD_T < pos_floor):
            failures.append(
                f"T={TWIN_JACCARD_T} no longer separates the measured populations "
                f"({worst:.3f} .. {pos_floor:.3f})"
            )

    # -- inbox label set: the LIVE model SQL on real extracted text ------------
    # Plan §5-1(b). Everything above scores in Python; this runs the MODEL, on
    # real corpus text, through the path the feature exists for -- a twin dropped
    # straight into the inbox under a mismatched name in a different collection,
    # which `_superseded_renditions` (name- and directory-based) cannot catch.
    # Lake-free on purpose: a synthetic bronze built from source/, so the gate
    # runs before the build in `make verify` and on a clone with no index.
    body = twin_model_body()
    if body is None:
        print("\n[controls] document_twins.sql absent; live-model inbox check skipped")
    elif pairs:
        import duckdb

        con = duckdb.connect(":memory:")
        con.execute("CREATE SCHEMA bronze")
        con.execute(
            "CREATE TABLE bronze.documents ("
            "doc_id VARCHAR, rel_path VARCHAR, doc_type VARCHAR, content VARCHAR,"
            "content_sha256 VARCHAR, ingested_at TIMESTAMP)"
        )
        expected = {}
        for index, (stem, original, derived) in enumerate(pairs):
            origin_id = f"orig-{index:02d}-{original.suffix[1:]}"
            # Mismatched name AND a different collection, which is the whole
            # point: nothing links this to its original except the content.
            drop_id = f"inbox-{index:02d}-pdf"
            expected[drop_id] = origin_id
            for doc_id, rel_path, path in (
                (origin_id, f"bills/{stem}{original.suffix}", original),
                (drop_id, f"sto/무관한이름_{index:02d}.pdf", derived),
            ):
                con.execute(
                    "INSERT INTO bronze.documents VALUES (?, ?, ?, ?, ?, TIMESTAMP '2026-01-01')",
                    [doc_id, rel_path, path.suffix[1:].lower(),
                     extract_text(path.name, path.read_bytes()), doc_id],
                )
        marked = {row[0]: row[1] for row in con.execute(body).fetchall()}
        print(
            f"\n[controls] live model on inbox drops: {len(marked)}/{len(expected)} marked "
            "(이름 불일치 · collection 교차 · 실제 추출 텍스트)"
        )
        if marked != expected:
            missed = sorted(set(expected) - set(marked))
            wrong = sorted(k for k in marked if expected.get(k) != marked[k])
            failures.append(
                f"live model marking mismatch -- missed={missed[:3]} miswinner={wrong[:3]}"
            )

    # -- first-class number ----------------------------------------------------
    residual = len(pairs) - caught
    print(
        f"\n[controls] 잔여 이중색인률(인박스 직투 가정): {residual}/{len(pairs)} "
        f"-- 큐레이션 시딩까지 겹치면 0/{len(pairs)} (bronze에 쌍둥이 미존재)"
    )

    if failures:
        print("\n[controls] FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("[controls] OK: positives all caught, negatives all rejected.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--twins",
        action="store_true",
        help="Measure source/ twin pairs directly instead of reading the lake.",
    )
    parser.add_argument(
        "--controls",
        action="store_true",
        help="Run the fuzzy-twin detector's positive/negative control set.",
    )
    arguments = parser.parse_args()
    if arguments.controls:
        raise SystemExit(report_controls())
    raise SystemExit(report_twins() if arguments.twins else main())
