"""Inbox watcher: promote `data/inbox/documents/` into the immutable raw zone.

Promotion is a copy plus an append-only manifest line. Nothing in `data/raw` is
ever edited in place:

  * first sighting of a path      -> copied to data/raw/documents/<rel_path>
  * identical bytes seen again    -> no-op (this is what makes re-runs safe)
  * changed bytes for a known path-> the previous bytes are preserved under
                                     data/raw/_revisions/<sha12>/<rel_path>
                                     before the new revision is written

Run once (`--once`) or as a polling loop (`--watch`). The loop uses stat polling
rather than an OS notification library so the watcher has no dependency that
could fail to build on a fresh machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline import Paths, get_paths
from pipeline.chunking import BINARY_SUFFIXES, SUPPORTED_SUFFIXES

POLL_SECONDS = 2.0
QUIET_SECONDS = 1.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_inbox(paths: Paths) -> int:
    """Copy the tracked seed documents into the git-ignored inbox.

    `data/` is entirely git-ignored, so a fresh clone has no input. Two tracked
    trees are materialised here, which keeps the "logic in Git, bytes in the data
    plane" split intact while still giving `make build` something to chew on:

      * `pipeline/fixtures/` — the small deterministic sample set the smoke test
        and the retrieval floor stand on (flat).
      * `source/` — the curated real corpus, one subfolder per collection; walked
        recursively and copied into the inbox PRESERVING that first subfolder, so
        `source/norms/x.txt` lands at `inbox/norms/x.txt` and keeps collection
        `norms` (`collection_of` = first inbox folder). Fixtures stay flat, so they
        remain the `_root` collection the smoke/eval floors expect.

    Only SUPPORTED_SUFFIXES are materialised, so a human-facing PDF sitting beside
    each `.hwp` original stays in `source/` and never enters the pipeline. An
    existing inbox file is never overwritten, which keeps this idempotent and lets
    a manual drop win over a re-seed.
    """
    # (candidate, destination-relative-to-inbox). Fixtures flatten by basename;
    # source keeps its subfolder so the folder-as-collection convention survives a
    # fresh clone -- otherwise the whole curated corpus re-seeds into `_root` and
    # every per-collection scope is lost.
    entries = []
    if paths.fixtures.exists():
        entries += [(c, Path(c.name)) for c in sorted(paths.fixtures.iterdir())]
    if paths.source.exists():
        entries += [(c, c.relative_to(paths.source)) for c in sorted(paths.source.rglob("*"))]

    if not entries:
        return 0
    paths.inbox.mkdir(parents=True, exist_ok=True)

    copied = 0
    for candidate, dest_rel in entries:
        if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        destination = paths.inbox / dest_rel
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
        copied += 1
    return copied


def _append_manifest(paths: Paths, entry: dict) -> None:
    with paths.raw_manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def promote_inbox(paths: Paths, verbose: bool = True) -> dict:
    """Promote every supported inbox file into the raw zone. Idempotent."""
    paths.ensure()
    summary = {"promoted": 0, "revised": 0, "unchanged": 0, "skipped": 0, "rejected": 0}

    for source in sorted(paths.inbox.rglob("*")):
        if not source.is_file():
            continue
        if source.suffix.lower() not in SUPPORTED_SUFFIXES:
            summary["skipped"] += 1
            continue

        rel_path = source.relative_to(paths.inbox).as_posix()

        # A binary document is validated BEFORE it is promoted. The raw zone is
        # immutable, so a file that cannot be parsed must never enter it: once
        # promoted it would crash the extractor on every subsequent build, and
        # the only way out would be to violate the immutability rule.
        if source.suffix.lower() in BINARY_SUFFIXES:
            from pipeline.extract import ExtractionError, extract_text

            try:
                extract_text(rel_path, source.read_bytes())
            except ExtractionError as error:
                summary["rejected"] += 1
                if verbose:
                    print(f"[watcher] rejected: {rel_path}: {error}")
                continue

        target = paths.raw_documents / rel_path
        incoming_sha = _sha256_file(source)

        if target.exists():
            existing_sha = _sha256_file(target)
            if existing_sha == incoming_sha:
                summary["unchanged"] += 1
                continue
            archive = paths.raw_revisions / existing_sha[:12] / rel_path
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                shutil.copy2(target, archive)
            target.unlink()
            summary["revised"] += 1
            action = "revised"
        else:
            summary["promoted"] += 1
            action = "promoted"

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _append_manifest(
            paths,
            {
                "action": action,
                "rel_path": rel_path,
                "content_sha256": incoming_sha,
                "byte_size": target.stat().st_size,
                "promoted_at": _now(),
            },
        )
        if verbose:
            print(f"[watcher] {action}: {rel_path} ({incoming_sha[:12]})")

    return summary


def _inbox_signature(paths: Paths) -> frozenset:
    """Cheap change detector: (path, size, mtime) for every inbox file."""
    entries = []
    for path in sorted(paths.inbox.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            stat = path.stat()
            entries.append((path.as_posix(), stat.st_size, int(stat.st_mtime_ns)))
    return frozenset(entries)


def watch(paths: Paths, poll_seconds: float = POLL_SECONDS) -> int:
    """Block, rebuilding the whole pipeline whenever the inbox changes.

    This is the "drop a file, get RAG + graph with no further command" path.
    """
    from pipeline.run import build  # deferred: run.py imports this module

    print(f"[watcher] watching {paths.inbox} (poll {poll_seconds}s, ctrl-c to stop)")
    previous = _inbox_signature(paths)
    while True:
        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("\n[watcher] stopped")
            return 0
        current = _inbox_signature(paths)
        if current == previous:
            continue
        # Wait for writes to settle so a half-copied file is never ingested.
        time.sleep(QUIET_SECONDS)
        settled = _inbox_signature(paths)
        if settled != current:
            previous = current
            continue
        print("[watcher] inbox changed, rebuilding")
        try:
            build(paths=paths)
        except Exception as error:  # keep watching even if one build fails
            print(f"[watcher] build failed: {error}", file=sys.stderr)
        previous = settled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Promote inbox files into the immutable raw zone.")
    parser.add_argument("--watch", action="store_true", help="Poll the inbox and rebuild on change.")
    parser.add_argument("--seed", action="store_true", help="Copy tracked seeds (source/ + fixtures) into the inbox.")
    parser.add_argument(
        "--poll-seconds", type=float, default=POLL_SECONDS, help="Polling interval for --watch."
    )
    args = parser.parse_args(argv)

    paths = get_paths().ensure()
    if args.seed:
        seeded = seed_inbox(paths)
        print(f"[watcher] seeded {seeded} tracked file(s) into {paths.inbox}")

    if args.watch:
        return watch(paths, poll_seconds=args.poll_seconds)

    summary = promote_inbox(paths)
    print(
        "[watcher] promoted={promoted} revised={revised} unchanged={unchanged} "
        "skipped={skipped} rejected={rejected}".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
