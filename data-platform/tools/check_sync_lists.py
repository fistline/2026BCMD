"""Every target must sync THE SAME environment. Compared by EXPANSION, not by text.

The Makefile has carried a comment about this trap since 69af01e: `uv sync`
REMOVES what it is not asked for, so a target that syncs a shorter list silently
uninstalls what another target installed. The GPU axis then walked straight back
into it -- `setup-gpu` expanded an accelerator list while `reset` and `verify`
expanded the CPU one, so every gate uninstalled the wheel the setup had just
installed, and the build inside that gate re-encoded 12,643 vectors because the
vector cache is keyed on the provider.

A text check would not have caught it: every one of those lines reads
`$(UV) sync $(SOMETHING)` and looks correct. So this asks `make -n` what the
lines actually EXPAND to, and compares the resulting extras sets.

`setup-gpu` is exempt from the equality rule and only from that: it is the target
whose whole job is to CHANGE the machine's stack, so it necessarily syncs
something different from the current one.

    uv run python tools/check_sync_lists.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Targets that must all agree, because each is expected to leave the environment
# in the state the others assume.
TARGETS = ("setup", "reset", "verify")
# The target that changes the state rather than assuming it.
EXEMPT = "setup-gpu"

_SYNC = re.compile(r"\buv\s+sync\b(?P<args>[^\n;&|]*)")


def extras_in(text: str) -> list:
    """Every `uv sync` line's extras set, in order of appearance."""
    sets: list = []
    for match in _SYNC.finditer(text):
        extras = frozenset(re.findall(r"--extra\s+(\S+)", match.group("args")))
        sets.append(extras)
    return sets


def dry_run(target: str, environment: dict | None = None) -> str:
    completed = subprocess.run(  # noqa: S603
        ["make", "-n", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    return completed.stdout


def main() -> int:
    import os

    failures: list = []
    # Two worlds: a machine with no accelerator stack recorded, and one with.
    # The second is what `.gpu-stack` produces, and it is the case that broke.
    for label, overrides in (("no .gpu-stack", {}), ("GPU=cuda", {"GPU": "cuda"})):
        environment = {**os.environ, **overrides}
        seen: dict = {}
        for target in TARGETS:
            for extras in extras_in(dry_run(target, environment)):
                seen.setdefault(frozenset(extras), []).append(target)
        if len(seen) > 1:
            rendered = "; ".join(
                f"{sorted(extras) or ['(none)']} <- {', '.join(sorted(set(targets)))}"
                for extras, targets in seen.items()
            )
            failures.append(f"[{label}] targets do not agree on the environment: {rendered}")
        elif overrides.get("GPU") and seen:
            only = next(iter(seen))
            if not any(extra.startswith("gpu-") for extra in only):
                failures.append(
                    f"[{label}] the recorded accelerator stack did not reach the sync list "
                    f"({sorted(only)}) -- a gate would uninstall the wheel setup-gpu installed"
                )

    if failures:
        print(f"FAIL: {len(failures)} sync-list disagreement(s):")
        for failure in failures:
            print(f"  {failure}")
        print(
            f"\nEvery target except `{EXEMPT}` must sync the SAME extras. `uv sync` removes what "
            f"it is not asked for, so a shorter list silently uninstalls another target's work."
        )
        return 1
    print(f"OK: {', '.join(TARGETS)} agree on the environment, with and without a recorded stack")
    return 0


if __name__ == "__main__":
    sys.exit(main())
