"""Stop a retired measurement from coming back by copy-paste.

Four numbers in this repo were wrong at once because each lived in two to five
places and the real measurement landed in one of them. MEASUREMENTS.md fixes the
cause (one home per figure, cited by id); this catches the resurrection.

It is deliberately a denylist of SPECIFIC retired literals, not a general
"is this number true" check -- that check cannot exist, and pretending otherwise
would be the same mistake in a new place. What it can do is make a known-wrong
string a red build.

The patterns are assembled from parts so this file does not match itself, the
same trick data-platform/Makefile uses for its evasion-token check.

    uv run python tools/check_retired_numbers.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where prose about measurements actually lives. The scan is deliberately narrow:
# MEASUREMENTS.md itself must be allowed to name what it retired, and git history
# is not ours to rewrite.
SCAN = ("pipeline", "tools", "transform", "agent")
SCAN_FILES = ("README.md", "AGENTS.md", ".env.example", "Makefile")
EXEMPT = {"MEASUREMENTS.md", "check_retired_numbers.py"}

# (assembled pattern, what to say instead)
RETIRED = [
    (r"~?\s*" + "37" + r"\s*[- ]?minute", "the cold rebuild was measured at 1433.9 s (23.9 min): cite [M:cold-rebuild]"),
    (r"\b" + "502" + r"\s*(vs|versus|/)\s*" + "466", "bulk fp16 vs int8 was measured at 250.2 vs 257.3 ms/text: cite [M:fp16-bulk]"),
]


def _targets() -> list:
    paths: list = []
    for name in SCAN:
        directory = ROOT / name
        if directory.is_dir():
            paths.extend(p for p in directory.rglob("*.py") if p.name not in EXEMPT)
            paths.extend(p for p in directory.rglob("*.md") if p.name not in EXEMPT)
    for name in SCAN_FILES:
        path = ROOT / name
        if path.exists() and path.name not in EXEMPT:
            paths.append(path)
    return sorted(set(paths))


def main() -> int:
    failures: list = []
    for path in _targets():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern, replacement in RETIRED:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                line = text[: match.start()].count("\n") + 1
                failures.append(
                    f"{path.relative_to(ROOT)}:{line}: retired figure {match.group(0)!r} -- {replacement}"
                )

    if failures:
        print(f"FAIL: {len(failures)} retired measurement(s) are back:")
        for failure in failures:
            print(f"  {failure}")
        print("\nSee MEASUREMENTS.md. A figure has one home and is cited by id.")
        return 1
    print(f"OK: no retired measurements ({len(_targets())} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
