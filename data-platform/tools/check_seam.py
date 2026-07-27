"""Keep the hardware seam mechanical: only ONE module in `pipeline/` may name onnxruntime.

The design says the controller must not depend on a specific hardware API. Said
that way it is a convention, and a convention is enforced by whoever happens to
review the diff. This turns it into a check: every module under `pipeline/` other
than the two permanent exemptions must be writable, readable and testable without
onnxruntime existing at all.

Three rules, checked over the AST rather than the text, because a comment
explaining the seam must not trip the check that guards it:

  1. no `import onnxruntime` / `from onnxruntime import ...`
  2. no `importlib.util.find_spec("onnxruntime")` -- availability is a question for
     `runtime.have_onnx()`, which answers it without importing and without raising
  3. no `*ExecutionProvider` string literal outside a docstring -- the moment a
     module names a provider it has an opinion about hardware, and that opinion
     belongs in runtime.py where the measurements that justify it live

Two permanent exemptions, and no allowlist beyond them. An allowlist rots: entries
stay after the code that needed them is gone, and then the checker enforces a map
of the past. `tools/` is deliberately NOT checked -- the operator tools exist
precisely to name providers and measure them.

    uv run python tools/check_seam.py          # from anywhere; paths resolve to the repo
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = "pipeline"

# runtime.py IS the seam. smoke_test.py asserts that an unregistered provider is
# rejected, which requires naming one that does not exist.
EXEMPT = {"runtime.py", "smoke_test.py"}

# Assembled rather than written out, so this file does not trip its own rule 3 --
# the same trick data-platform/Makefile uses for its evasion-token pattern.
_EP_SUFFIX = "Execution" + "Provider"
_TARGET = "onnx" + "runtime"


def _docstring_nodes(tree: ast.AST) -> set:
    """id() of every Constant that is a docstring, so prose is not a violation."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def violations(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    found: list = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _TARGET or alias.name.startswith(_TARGET + "."):
                    found.append((node.lineno, f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == _TARGET or node.module.startswith(_TARGET + ".")):
                found.append((node.lineno, f"imports from {node.module}"))
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == "find_spec":
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and argument.value == _TARGET:
                        found.append(
                            (node.lineno, "probes for onnxruntime; use runtime.have_onnx()")
                        )
        elif isinstance(node, ast.Constant):
            value = node.value
            if (
                isinstance(value, str)
                and value.endswith(_EP_SUFFIX)
                and id(node) not in docstrings
            ):
                found.append((node.lineno, f"names a provider: {value!r}"))
    return found


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent / PACKAGE
    if not root.is_dir():
        print(f"FAIL: {root} does not exist")
        return 1

    failures: list = []
    checked = 0
    for path in sorted(root.rglob("*.py")):
        if path.name in EXEMPT:
            continue
        checked += 1
        for line, reason in violations(path):
            failures.append(f"{path.relative_to(root.parent)}:{line}: {reason}")

    if failures:
        print(f"FAIL: the hardware seam is broken in {len(failures)} place(s):")
        for failure in failures:
            print(f"  {failure}")
        print(
            f"\nOnly {PACKAGE}/{' and '.join(sorted(EXEMPT))} may name an execution provider. "
            f"Everything else goes through pipeline/runtime.py, so the pipeline stays "
            f"writable and testable without onnxruntime installed."
        )
        return 1
    print(f"OK: the hardware seam holds ({checked} module(s) checked, {len(EXEMPT)} exempt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
