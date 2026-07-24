"""Acceptance gate for document-type profiles, and a corpus triage report.

The naive "let a model write the parser" pattern was measured to LOSE to
per-document model extraction (EVAPORATE, VLDB 2024: -13.8 F1). The same paper
beat it by +12.1 F1 with one addition: a verification harness. This is that
harness. Author profiles against it, not against your own reading of the regex.

Two commands:

    python -m pipeline.doctypes.gate --report   triage: what is claimed, by what
    python -m pipeline.doctypes.gate            the gate: exits non-zero on failure

Note what is deliberately NOT checked. Span tiling and character coverage look
like coverage proofs and are worthless: `sections()` emits consecutive spans from
0 to len(text) by construction, so a profile that matches 3 of 179 headings
scores tiling=True and coverage=1.000, identical to an honest one. Marker recall
and the largest-section ratio are what separate them.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from pipeline import get_paths
from pipeline.chunking import _KO_SECTION_RE, SUPPORTED_SUFFIXES, parse_document
from pipeline.doctypes import PROFILE_MODULES, classify, edges, load_profiles, sections

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "doctypes"

# A profile that finds fewer headings than the generic scan is worse than no
# profile. Slightly under 1.0 is allowed: zone markers legitimately absorb a few.
MIN_MARKER_RECALL = 0.95
# One section holding most of the document means the profile matched near the top
# and swallowed the rest. This is the exact EVAPORATE failure shape.
MAX_LARGEST_SECTION = 0.5
# A pathological regex hangs the build with no error while every other check
# reports clean.
MAX_SECTION_SECONDS = 5.0

# A profile is "import re plus four tuples". Anything else at module level is
# rejected structurally rather than by a denylist of forbidden names, which would
# never reject `import ctypes` and would need maintenance forever.
ALLOWED_CALLS = {"compile", "Marker", "EdgeRule"}
FORBIDDEN_NODES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.For, ast.AsyncFor, ast.While, ast.If, ast.With, ast.AsyncWith,
    ast.Try, ast.Raise, ast.Global, ast.Nonlocal,
)

FAILURES: list = []


def check(description: str, condition: bool, detail="") -> None:
    if condition:
        print(f"  ok   {description}")
        return
    FAILURES.append(f"{description}: {detail}")
    print(f"  FAIL {description}: {detail}")


def _profile_source(profile) -> Path:
    return Path(profile.__file__)


def audit_purity(profile) -> None:
    """A profile must be data: no statements, no calls outside the allowlist."""
    tree = ast.parse(_profile_source(profile).read_text(encoding="utf-8"))

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        check(
            f"{profile.DOC_TYPE}: only imports and tuple assignments at module level",
            False,
            f"{type(node).__name__} at line {node.lineno}",
        )
        return

    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            check(
                f"{profile.DOC_TYPE}: contains no executable control flow",
                False,
                f"{type(node).__name__} at line {node.lineno}",
            )
            return
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ALLOWED_CALLS:
                check(
                    f"{profile.DOC_TYPE}: calls only {sorted(ALLOWED_CALLS)}",
                    False,
                    f"{name} at line {node.lineno}",
                )
                return
    check(f"{profile.DOC_TYPE}: is data, not code", True)


def _generic_marker_count(text: str) -> int:
    return len(_KO_SECTION_RE.findall(text))


def measure(profile, text: str) -> dict:
    """The two checks that actually separate an honest profile from a broken one."""
    started = time.monotonic()
    spans = sections(profile, text)
    elapsed = time.monotonic() - started

    headed = [span for span in spans if span[0]]
    generic = _generic_marker_count(text)
    largest = max((end - start) for _heading, start, end in spans) if spans else 0
    return {
        "sections": len(spans),
        "headed": len(headed),
        "generic": generic,
        "marker_recall": (len(headed) / generic) if generic else 0.0,
        "largest_ratio": (largest / len(text)) if text else 1.0,
        "seconds": elapsed,
    }


def _read_fixture(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def gate_profile(profile) -> None:
    """Every check a profile must pass before it may be committed."""
    doc_type = profile.DOC_TYPE
    sample = FIXTURE_DIR / f"{doc_type}.sample.txt"
    golden = FIXTURE_DIR / f"{doc_type}.golden.json"

    # Without this an empty glob prints "gate passed" having checked nothing.
    check(f"{doc_type}: has a golden sample", sample.exists(), str(sample))
    check(f"{doc_type}: has a golden expectation", golden.exists(), str(golden))
    if not (sample.exists() and golden.exists()):
        return

    audit_purity(profile)

    text = _read_fixture(sample)
    check(f"{doc_type}: claims its own sample", classify(sample.name, text) is profile, "not claimed")

    stats = measure(profile, text)
    check(
        f"{doc_type}: marker recall >= {MIN_MARKER_RECALL}",
        stats["marker_recall"] >= MIN_MARKER_RECALL,
        f"{stats['headed']} headed vs {stats['generic']} generic = {stats['marker_recall']:.3f}",
    )
    check(
        f"{doc_type}: no section swallows the document",
        stats["largest_ratio"] <= MAX_LARGEST_SECTION,
        f"largest section is {stats['largest_ratio']:.3f} of the text",
    )
    check(
        f"{doc_type}: sectioning terminates quickly",
        stats["seconds"] <= MAX_SECTION_SECONDS,
        f"{stats['seconds']:.2f}s",
    )

    # Evidence must be verbatim source text. The engine slices it, so this
    # verifies the engine as much as the profile.
    fabricated = [
        quote
        for _relation, _source, _sk, _target, _tk, quote in edges(profile, "sample", text)
        if quote not in text
    ]
    check(f"{doc_type}: every edge quote is verbatim source text", not fabricated, fabricated[:2])

    # CRLF twin. The pipeline normalises line endings, which is exactly why this
    # bug survives testing: an author tests the file they read and gets a false
    # green while real extractor output returns nothing.
    crlf_headings = [heading for heading, _s, _e in sections(profile, text.replace("\n", "\r\n"))]
    lf_headings = [heading for heading, _s, _e in sections(profile, text)]
    check(f"{doc_type}: markers survive CRLF input", crlf_headings == lf_headings, "headings differ under CRLF")

    # Chunk ids must not move silently: they are positional, so a profile edit
    # that renumbers sections invalidates every stored embedding.
    expected = json.loads(golden.read_text(encoding="utf-8"))
    parsed = parse_document(sample.name, sample.read_bytes())
    actual_headings = [chunk.heading for chunk in parsed.chunks]
    actual_ids = [chunk.chunk_id for chunk in parsed.chunks]
    check(
        f"{doc_type}: headings match the golden file",
        actual_headings == expected["headings"],
        f"{len(actual_headings)} headings, expected {len(expected['headings'])}",
    )
    check(f"{doc_type}: chunk ids match the golden file", actual_ids == expected["chunk_ids"], "ids moved")


def gate_exclusivity() -> None:
    """No profile may claim a document another profile or the generic path owns."""
    paths = get_paths()
    bundled = [
        path
        for path in sorted((paths.root / "pipeline" / "fixtures").iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    claimed = []
    for path in bundled:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        profile = classify(path.name, text)
        if profile is not None:
            claimed.append(f"{path.name} -> {profile.DOC_TYPE}")
    check("no profile claims a bundled repo fixture", not claimed, claimed)

    for profile in load_profiles():
        sample = FIXTURE_DIR / f"{profile.DOC_TYPE}.sample.txt"
        if not sample.exists():
            continue
        text = _read_fixture(sample)
        others = [
            other.DOC_TYPE
            for other in load_profiles()
            if other is not profile and classify(sample.name, text) is other
        ]
        check(f"{profile.DOC_TYPE}: no other profile claims its sample", not others, others)


def gate_determinism() -> None:
    """Prove the whole parse is reproducible across processes.

    An AST scan cannot see set-iteration order and a seed change cannot see an
    import, so both are needed. PYTHONHASHSEED is fixed at interpreter start,
    which is why this re-parses in FRESH subprocesses rather than in a loop.
    """
    for profile in load_profiles():
        sample = FIXTURE_DIR / f"{profile.DOC_TYPE}.sample.txt"
        if not sample.exists():
            continue
        digests = []
        script = (
            "import json,sys;"
            "from pipeline.chunking import parse_document;"
            f"d=parse_document({sample.name!r}, open({str(sample)!r},'rb').read());"
            "print(json.dumps([[c.chunk_id,c.heading,c.content] for c in d.chunks],"
            "ensure_ascii=False,sort_keys=True))"
        )
        for seed in ("0", "1", "12345"):
            environment = dict(os.environ, PYTHONHASHSEED=seed)
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env=environment,
                cwd=str(get_paths().root),
                check=False,
            )
            if result.returncode != 0:
                check(f"{profile.DOC_TYPE}: parses under PYTHONHASHSEED={seed}", False, result.stderr[-200:])
                return
            digests.append(result.stdout)
        check(
            f"{profile.DOC_TYPE}: identical output across three PYTHONHASHSEEDs",
            len(set(digests)) == 1,
            f"{len(set(digests))} distinct outputs",
        )


def report() -> int:
    """Triage: which profile claims what, and what is left unclaimed."""
    paths = get_paths()
    root = paths.raw_documents
    if not root.exists():
        print(f"[report] {root} does not exist; run `make ingest` first.")
        return 1

    print(f"[report] profiles registered: {', '.join(PROFILE_MODULES) or '(none)'}")
    print(f"[report] scanning {root}")
    unclaimed = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        parsed = parse_document(path.name, path.read_bytes())
        profile = classify(path.name, parsed.content)
        generic = _generic_marker_count(parsed.content)
        if profile is None:
            unclaimed.append((path.name, generic))
            print(f"  --        {path.name[:44]:46} chunks={len(parsed.chunks):4} generic_markers={generic}")
            continue
        stats = measure(profile, parsed.content)
        print(
            f"  {profile.DOC_TYPE:9} {path.name[:44]:46} chunks={len(parsed.chunks):4} "
            f"headed={stats['headed']:4} recall={stats['marker_recall']:.2f} "
            f"largest={stats['largest_ratio']:.3f}"
        )

    print(f"\n[report] unclaimed: {len(unclaimed)}")
    for name, generic in unclaimed:
        note = " (has 조문 markers a profile could use)" if generic > 3 else ""
        print(f"    {name[:56]}{note}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", action="store_true", help="Triage the corpus instead of gating.")
    args = parser.parse_args(argv)

    if args.report:
        return report()

    print("profile gate")
    profiles = load_profiles()
    check("at least one profile is registered", bool(profiles), PROFILE_MODULES)
    for profile in profiles:
        gate_profile(profile)
    gate_exclusivity()
    gate_determinism()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("profile gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
