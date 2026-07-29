"""No token that stands in for work not done.

An ellipsis at the head of a comment, a TODO, a "omitted for brevity" -- each is a
place where prose promised something and stopped. They are cheap to write and
invisible in review, so this is a check rather than a convention.

It lived as one `grep` line inside `data-platform/Makefile`'s `verify` target, and
that placement is the reason it exists as a file now: `verify` is the gate nobody
runs automatically. The pre-commit hook runs the ROOT `make check`, and CI runs the
individual checkers -- neither had this one. Measured consequence: an ellipsis
entered `pipeline/eval_retrieval.py` in 32f15ca on 2026-07-28, three commits landed
on top of it, and `make verify` stayed red for a day with nobody seeing it.

So the pattern moves here, where all three callers can share it, and the Makefile
line becomes a call instead of a definition.

The tokens are ASSEMBLED rather than written out, the same trick check_seam.py uses,
so this file does not trip the rule it enforces.

    uv run python tools/check_evasion.py          # or plain python3; no dependencies
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Trees scanned, relative to data-platform/. Exactly the Makefile line this
# replaced -- `tools/` is deliberately absent, and widening it was tried and
# reverted in the same sitting: the operator tools are full of ellipsis-as-SYNTAX
# (`os.environ[...]`, `from onnxruntime import ...`, `[<skills-root> ...]`,
# `make ocr-compare FILE=...`), and 7 of the 9 hits were that. A checker that
# cries wolf teaches people to ignore it, which is the failure check_knobs.py
# documents at length. Distinguishing a placeholder from an evasion needs a
# parser, and this rule is not worth one.
SCAN = ("pipeline", "transform", "agent", ".agents", "Makefile", "meltano.yml")

SUFFIXES = {".py", ".md", ".sql", ".yml", ".yaml", ".toml", ".tsv", ""}

# Assembled so the checker is not its own first violation.
_TOKENS = (
    "TO" + "DO",
    "FI" + "XME",
    "omitted" + " for brevity",
)
_PATTERN = re.compile("|".join([*(re.escape(token) for token in _TOKENS), r"[.]{3}"]))

# A file may not be text; a file may be huge. Neither is a violation.
_MAX_BYTES = 4_000_000


def _candidates() -> list:
    found: list = []
    for entry in SCAN:
        path = ROOT / entry
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(
                child
                for child in sorted(path.rglob("*"))
                if child.is_file()
                and child.suffix in SUFFIXES
                and "__pycache__" not in child.parts
                and ".venv" not in child.parts
            )
    return found


def violations() -> list:
    found: list = []
    for path in _candidates():
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            if _PATTERN.search(line):
                found.append((path.relative_to(ROOT), number, line.strip()[:100]))
    return found


def main() -> int:
    found = violations()
    if found:
        print(f"FAIL: {len(found)} evasion token(s):")
        for path, number, line in found:
            print(f"  {path}:{number}: {line}")
        print(
            "\nEach of these is a place where the prose stopped short of the work. "
            "Write the sentence, or delete the line -- do not leave the marker."
        )
        return 1
    print(f"OK: no evasion tokens ({len(_candidates())} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
