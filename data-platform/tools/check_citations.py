"""A tuning value must cite the measurement it came from, and the measurement must
say which documents it was taken over.

Three rules, and each exists because the thing it checks failed here:

  1. EVERY `[M:id]` RESOLVES. The citation convention is load-bearing -- comments,
     .env.example and AGENTS.md all defer to it instead of restating figures -- and
     nothing checked that the id existed. A dangling one reads exactly like a real
     one.

  2. EVERY ROW NAMES ITS CORPUS. `M:cold-rebuild` (1433.9 s over 12 643 vectors)
     was compared against a fresh 1948.2 s over 19 808 vectors, and that was only
     sound because whoever ran it knew the two shared a document set. Nothing
     recorded it. `unknown` is a legal answer for rows that predate the column: a
     guess would be a fabricated provenance, which is worse than a blank.

  3. EVERY RESULT-AFFECTING KNOB CITES. RERANK_WEIGHT sat at 0.15 with a paragraph
     of reasoning and no measurement behind it, and when SECTION_CAP lifted fusion
     the value silently became the wrong one -- the reranked arm scored below plain
     fusion and nothing said so. The knobs listed below are the ones whose value
     changes what a query returns; each must name at least one measurement in its
     `.env.example` paragraph.

Rule 3 has a limit worth stating rather than hiding: this cannot tell a relevant
citation from a nearby one. It enforces that a number came from somewhere, not that
it came from the right somewhere. That part is still a reader's job.

    uv run python tools/check_citations.py      # or plain python3; no dependencies
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEASUREMENTS = ROOT / "MEASUREMENTS.md"
ENV_EXAMPLE = ROOT / ".env.example"

SCAN = ("pipeline", "tools", "agent", "transform", ".agents")
SUFFIXES = {".py", ".md", ".sql", ".yml", ".yaml"}

# Knobs whose value decides what a query returns. Listed rather than derived, the
# same way check_lock_pin.py lists packages: the set is small, the reason differs
# per entry, and a derivation would have to guess. Add one when you add a knob that
# moves results; do not add one that only moves speed.
TUNING_KNOBS = {
    "SECTION_CAP": "caps one (doc_id, heading) in an answer; coupled to MAX_CHUNK_CHARS",
    "RERANK_WEIGHT": "retrieval prior against the cross-encoder in the rerank fusion",
    "RERANK_CANDIDATES": "not a recall knob -- the batch it forms sets every CE score",
    "VECTOR_WEIGHT": "arm weighting in the fused RRF",
    "RRF_K": "flattens the contribution of top ranks in the fusion",
    "EMBEDDING_PRECISION": "int8 and fp16 are different vector spaces",
    "KEYWORD_WEIGHT": "the other half of the fusion ratio; only the ratio matters",
    "ALIAS_EXPANSION": "query-side synonym bridging; worth 0.237 fused MRR@10 here",
}

# Deliberately absent, so the omission is a decision and not an oversight:
#   GRAPH_DEPTH   moves `make ask`, whose floor is eval_graph_rag rather than eval
#   KIWI_MORPH    moves the keyword arm, but it is already in index_signature, so a
#                 mismatch is caught loudly rather than silently
#   ENCODE_BATCH  same -- signatured, and AGENTS.md invariant 9 covers it at length
# Add one here when a knob changes what a query returns AND no signature can see it.

_ROW = re.compile(r"^\|\s*`(M:[a-z0-9-]+)`\s*\|(.*)\|\s*$")
_CITATION = re.compile(r"\[(M:[a-z0-9-]+)\]")
_CORPUS_CELL = re.compile(r"^(unknown|c:[0-9a-f]{12})$")


def defined_rows() -> dict:
    """id -> list of cells, for every row of the measurements table."""
    rows: dict = {}
    for line in MEASUREMENTS.read_text(encoding="utf-8").split("\n"):
        match = _ROW.match(line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in match.group(2).split(" | ")]
    return rows


def citations() -> dict:
    """id -> {files that cite it}, across the source trees and .env.example."""
    found: dict = {}
    targets = [ENV_EXAMPLE, MEASUREMENTS.parent / "AGENTS.md"]
    for entry in SCAN:
        base = ROOT / entry
        if base.is_dir():
            targets.extend(
                path
                for path in base.rglob("*")
                if path.is_file()
                and path.suffix in SUFFIXES
                and "__pycache__" not in path.parts
                and ".venv" not in path.parts
            )
    for path in targets:
        if not path.is_file() or path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for identifier in _CITATION.findall(text):
            found.setdefault(identifier, set()).add(str(path.relative_to(ROOT)))
    return found


def knob_paragraphs() -> dict:
    """knob -> the comment block immediately above its line in .env.example."""
    blocks: dict = {}
    buffer: list = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").split("\n"):
        stripped = line.strip()
        # Assignment FIRST: a commented-out default (`# RERANK_WEIGHT=3.0`) is the
        # normal way this file shows a knob, and testing for `#` first swallowed
        # every one of them as prose -- the checker then reported three documented
        # knobs as undocumented, which is the false alarm that gets a gate ignored.
        assignment = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", stripped)
        if assignment:
            blocks.setdefault(assignment.group(1), []).extend(buffer)
            buffer = []
            continue
        if stripped.startswith("#"):
            buffer.append(stripped)
            continue
        buffer = []
    return blocks


def main() -> int:
    if not MEASUREMENTS.exists():
        print(f"FAIL: no {MEASUREMENTS}")
        return 1

    rows = defined_rows()
    cited = citations()
    failures: list = []

    for identifier, where in sorted(cited.items()):
        if identifier not in rows:
            failures.append(
                f"[{identifier}] is cited by {', '.join(sorted(where))} but has no row in "
                f"MEASUREMENTS.md"
            )

    for identifier, cells in sorted(rows.items()):
        if len(cells) < 4:
            failures.append(f"`{identifier}` has {len(cells)} cells; expected value|corpus|machine|reproduce")
            continue
        corpus = cells[1]
        if not _CORPUS_CELL.match(corpus):
            failures.append(
                f"`{identifier}` corpus cell is {corpus!r}; expected `unknown` or a "
                f"`c:` id from `make corpus-id`"
            )

    paragraphs = knob_paragraphs()
    for knob, reason in sorted(TUNING_KNOBS.items()):
        block = "\n".join(paragraphs.get(knob, []))
        if not block:
            failures.append(f"{knob} is a tuning knob ({reason}) but is not documented in .env.example")
        elif not _CITATION.search(block):
            failures.append(
                f"{knob} sets a value that changes what a query returns ({reason}) and its "
                f".env.example paragraph cites no measurement"
            )

    if failures:
        print(f"FAIL: {len(failures)} citation/provenance problem(s):")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nA tuning value with no measurement behind it is a guess with a paragraph, and a "
            "measurement with no corpus is not comparable to any other. `make corpus-id` prints "
            "the current one."
        )
        return 1
    print(
        f"OK: {len(rows)} measurement(s), all with a corpus; {len(cited)} citation(s), all "
        f"resolving; {len(TUNING_KNOBS)} tuning knob(s), all citing"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
