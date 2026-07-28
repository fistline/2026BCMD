"""Every knob the code reads is documented, and every knob documented is read.

Both directions have bitten this repo. `ORT_THREADS` was advertised in
runtime.py's knob list and read by nothing, so an operator could set it and watch
it do nothing. In the other direction several live knobs -- the ones that change
what a build produces -- were documented nowhere, which is worse: the only way to
discover them was to read the source.

Collected by AST, not by grep, so a knob named inside a comment or a docstring is
not mistaken for a read.

    uv run python tools/check_knobs.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ("pipeline", "tools")
DOCS = (".env.example",)

# Helpers that READ the environment on their caller's behalf, taking the knob
# name as their first argument. Without these, a knob reached through one of them
# looks documented-but-unread -- exactly the false alarm that teaches people to
# ignore a checker.
ENV_HELPERS = {"_precision", "_env_flag"}

# Environment read by something other than this project's own configuration.
# Each entry is a knob we do not own and therefore do not document.
THIRD_PARTY = {
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
    "MELTANO_PROJECT_ROOT", "SQLMESH_GATEWAY", "PATH", "HOME", "TMPDIR",
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "NO_COLOR", "CI",
}


def _read_keys(path: Path) -> set:
    """Literal keys passed to os.environ.get / os.environ[...] / os.getenv."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return set()
    keys: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            is_environ_get = name == "get" and getattr(getattr(target, "value", None), "attr", "") == "environ"
            if name in ENV_HELPERS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
            if name == "getenv" or is_environ_get:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    keys.add(node.args[0].value)
        elif isinstance(node, ast.Subscript):
            value = node.value
            if getattr(value, "attr", "") == "environ":
                index = node.slice
                if isinstance(index, ast.Constant) and isinstance(index.value, str):
                    keys.add(index.value)
    return keys


def _documented() -> set:
    """Knobs named in .env.example, set or commented-out, plus the Knobs: block."""
    documented: set = set()
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            # A backticked mention is prose, not documentation. Operators scan
            # .env.example for the canonical `KNOB=` form; a name inside a
            # sentence about a make target is not that form.
            if "`" in line:
                continue
            match = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=", line)
            if match:
                documented.add(match.group(1))
    runtime = ROOT / "pipeline" / "runtime.py"
    if runtime.exists():
        for line in runtime.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s+([A-Z][A-Z0-9_]{2,})=", line)
            if match:
                documented.add(match.group(1))
    return documented


def main() -> int:
    read: dict = {}
    for name in SCAN:
        directory = ROOT / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            for key in _read_keys(path):
                read.setdefault(key, []).append(str(path.relative_to(ROOT)))

    documented = _documented()
    live = {key for key in read if key not in THIRD_PARTY}

    undocumented = sorted(live - documented)
    unread = sorted(documented - set(read) - THIRD_PARTY)

    if not undocumented and not unread:
        print(f"OK: {len(live)} knob(s) read, all documented; none documented-but-unread")
        return 0

    print("FAIL: the knobs and the documentation disagree.")
    for key in undocumented:
        where = ", ".join(sorted(set(read[key]))[:3])
        print(f"  READ BUT UNDOCUMENTED  {key}  ({where})")
    for key in unread:
        print(f"  DOCUMENTED BUT UNREAD  {key}  (nothing calls os.environ for it)")
    print(
        "\nAdd the knob to .env.example, or delete the documentation. A knob an operator "
        "can set and watch do nothing is worse than an undocumented one."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
