"""A lock movement in a load-bearing package must be a decision, not a diff nobody read.

`uv.lock` is committed, so a version change IS visible in review — but only to
someone who looks, and these four packages change behaviour in ways that do not
announce themselves:

  onnxruntime (any fork)  the graph optimiser, the EP set, and which assets load
                          at all. fp16 already fails to load at the DEFAULT
                          optimisation level on 1.27.0; a minor bump can move that.
  tokenizers              padding and truncation, which decide the batch geometry
                          that decides the vectors.
  numpy                   the dtype and reduction behaviour under the encoder.
  kiwipiepy               the morphology in index_signature's kiwi slot.

So the resolved versions are pinned here as literals with a reason each. When one
moves, this goes red and the person who moved it writes the new literal down —
which is the whole point: the diff becomes a sentence someone had to type.

    uv run python tools/check_lock_pin.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# tomllib is 3.11+, and BOTH Makefile gates invoke this with a bare `python3` on
# purpose -- the cheap invariants run without a venv, so they also run on
# whatever interpreter the machine ships. macOS ships 3.9.6 and Ubuntu 22.04
# ships 3.10, so importing it at module scope crashed the gate with a traceback
# BEFORE the `uv sync` that could have repaired anything.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on the host interpreter
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "uv.lock"

# package -> (expected versions, why this package is pinned)
# More than one version is normal: the lock carries a separate resolution per
# marker (platform, python version), and all of them matter.
PINNED = {
    "onnxruntime": (
        {"1.24.3", "1.27.0"},
        "graph optimiser and EP set; fp16 already fails to load at the default level",
    ),
    "onnxruntime-gpu": (
        {"1.24.3", "1.28.0"},
        "same, for the CUDA fork; also the wheel-availability question per platform",
    ),
    "onnxruntime-directml": (
        {"1.24.3", "1.24.4"},
        "same, for the DirectML fork",
    ),
    "onnxruntime-qnn": (
        {"1.24.3", "1.24.4"},
        "same, for the QNN fork (unverified on hardware)",
    ),
    "tokenizers": (
        {"0.23.1"},
        "padding and truncation decide the batch geometry, which decides the vectors",
    ),
    "numpy": (
        {"2.2.6"},
        "dtype and reduction behaviour under the encoder",
    ),
    "kiwipiepy": (
        {"0.23.2"},
        "morphology recorded in index_signature's kiwi slot",
    ),
}


_PACKAGE = re.compile(
    r"^\[\[package\]\]\s*$(?P<body>.*?)(?=^\[\[|\Z)", re.MULTILINE | re.DOTALL
)
_FIELD = re.compile(r'^(name|version)\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def resolved_versions() -> dict:
    """name -> {versions}. Falls back to a regex when tomllib is unavailable.

    The lock's `[[package]]` blocks are machine-generated and flat, so the regex
    is exact for this file rather than a general TOML parser -- and it keeps the
    checker runnable on the interpreter the gate actually uses.
    """
    text = LOCK.read_text(encoding="utf-8")
    versions: dict = {}
    if tomllib is not None:
        for package in tomllib.loads(text).get("package", []):
            name, version = package.get("name"), package.get("version")
            if name and version:
                versions.setdefault(name, set()).add(version)
        return versions
    for block in _PACKAGE.finditer(text):
        fields = dict(_FIELD.findall(block.group("body")))
        if fields.get("name") and fields.get("version"):
            versions.setdefault(fields["name"], set()).add(fields["version"])
    return versions


def main() -> int:
    if not LOCK.exists():
        print(f"FAIL: no lock file at {LOCK}")
        return 1

    found = resolved_versions()
    failures: list = []
    for name, (expected, reason) in PINNED.items():
        actual = found.get(name)
        if actual is None:
            failures.append(f"{name}: pinned to {sorted(expected)} but absent from the lock ({reason})")
        elif actual != expected:
            failures.append(
                f"{name}: lock has {sorted(actual)}, this file expects {sorted(expected)} -- {reason}"
            )

    if failures:
        print(f"FAIL: {len(failures)} pinned package(s) moved:")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nIf the move was deliberate, update the literal in tools/check_lock_pin.py in the\n"
            "SAME commit and say what you re-measured. These packages change vectors, asset\n"
            "loading or the EP set, and none of those announce themselves at runtime."
        )
        return 1
    print(f"OK: {len(PINNED)} load-bearing package(s) are at their pinned versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
