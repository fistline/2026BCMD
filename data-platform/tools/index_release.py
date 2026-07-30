"""Ship the serving index as a release asset. Git carries the hash, the release carries the bytes.

`data/serving/index.sqlite` is 186 MiB and a fresh clone cannot answer anything
until it has one: `make build` re-encodes the corpus in ~32 min [M:chunk-650].
Committing the file instead is not an option and not only because GitHub blocks a
blob over 100 MiB -- a SQLite file has no delta and no merge, so every rebuild
would park another whole copy in history, and invariant 9 promises byte-identical
rebuilds only on the SAME machine and device profile, so two people building the
same corpus would produce two different files that git could never reconcile.

So the bytes go where bytes go (a release asset) and the plane that is already
trusted carries the checksum: `index_release.json` is TRACKED. That is the whole
design. A release is mutable -- the same tag can be re-uploaded with different
bytes -- so a checksum that travels beside the asset proves nothing. A checksum
that travels in git does, because changing it is a commit.

WHAT IS VERIFIED BEFORE PUBLISHING (each refusal names what it protects):

  build_kind == canonical   An incrementally-built index is fine to QUERY and
                            wrong to record a floor from: a cached vector was
                            encoded beside different neighbours (cosine 0.9904,
                            enough to move R@10 by 0.15, see `make index-canonical`).
                            Shipping one would hand every consumer a floor they
                            cannot reproduce.
  corpus_id verifies        `tools/corpus_id.py` re-hashes every file in
                            source/CORPUS_MANIFEST.tsv against the bytes on disk.
                            A declared-but-wrong manifest already happened here
                            twice (3d31ee3, 87c1ecd).
  pipeline/ and source/ clean   Not the whole tree: the settings that shape an
                            index live in `.env`, which is not tracked at all, so
                            a commit sha never reproduces an index -- that is what
                            `index_signature` is for. Blocking on an unrelated
                            edited README would be theatre; blocking on uncommitted
                            pipeline code would not.
  the three eval floors     Retrieval, graph reachability, related-section. The
                            artifact ships with proof, or it does not ship.
  the tag does not exist    Tags are never reused, so a pinned pointer always
                            names the same bytes.

WHAT THE CONSUMER GETS. `fetch` compares the pointer's `index_signature` against
the locally computed one BEFORE downloading 92 MiB, verifies sha256 of both the
compressed and the decompressed file against the tracked pointer, and installs
atomically -- keeping the index you already had until the new one has passed
`assert_index_current` and the repo's own `smoke_test.py`, then rolling back to it
if either fails. The one thing that is NOT a rollback is a tree with no warmed
embedder: the bytes are proven, only the settings question is deferred, and
throwing away a verified 92 MB to learn nothing would be the wrong trade
(see `Unverifiable`).

WHY xz. Measured on this index [M:index-xz]: xz -6 96,254,760 B in 11.9 s, xz -1
101,885,596, zstd -3 104,602,864 in 0.4 s, gzip -6 107,690,695. The choice is not
about the 8 MB -- it is that `lzma` is in the standard library while stdlib zstd
needs Python 3.14 and this venv is 3.12, so xz costs the consumer no dependency
and happens to be the smallest as well. The file is NOT vacuumed first: the
published bytes are exactly the bytes `make index-canonical` wrote, so anyone on
the same machine profile can rebuild and compare sha256 -- which is what makes
invariant 9's byte-identical promise checkable rather than decorative.

    uv run python tools/index_release.py publish            # dry run: prints, uploads nothing
    uv run python tools/index_release.py publish --yes
    uv run python tools/index_release.py fetch
    python3 tools/index_release.py check                    # offline, stdlib only, no venv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POINTER = ROOT / "index_release.json"
INDEX = ROOT / "data" / "serving" / "index.sqlite"
ASSET = "index.sqlite.xz"

# xz -6, measured [M:index-xz]. Higher presets buy little on int8 vectors and cost
# minutes; the payload is close to incompressible either way (2.03x).
PRESET = 6

# Everything `fetch` refuses to run without. A pointer missing one of these is a
# hand-edit or a half-written publish, and both must fail loudly rather than
# produce a download that cannot be verified.
REQUIRED = (
    "tag",
    "asset",
    "repo",
    "sha256_xz",
    "sha256_sqlite",
    "bytes_xz",
    "bytes_sqlite",
    "corpus_id",
    "index_signature",
    "build_kind",
    "chunk_count",
    "vector_signature",
)

CHUNK = 1 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo_slug() -> str:
    """owner/name from the origin remote, https or ssh."""
    url = _git("remote", "get-url", "origin")
    slug = url.removesuffix(".git").removeprefix("git@github.com:")
    if "github.com/" in slug:
        slug = slug.split("github.com/", 1)[1]
    return slug


def _index_meta(path: Path) -> dict:
    """The index's own build identity. Read with stdlib sqlite3 -- no extension,
    no embedder, so this works before the venv is usable."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {key: value for key, value in connection.execute("SELECT key, value FROM index_meta")}
    finally:
        connection.close()


def tag_for(corpus_id: str, signature: str, chunk_count: int, vector_signature: str) -> str:
    """A tag names one artifact, so it has to move whenever the bytes would.

    `corpus_id` answers WHICH DOCUMENTS and only that, by its own docstring.
    `index_signature` covers the embedder, the tokenizer and the n-gram widths.
    Neither covers the CHUNKING -- `MAX_CHUNK_CHARS` is absent from both, and the
    650-vs-1200 change that reshaped this index [M:chunk-650] moved no signature
    at all. Without a third input, two genuinely different indexes derive the same
    tag, and the first version of this function claimed the opposite in its own
    docstring while doing exactly that.

    `chunk_count` is what closes it: 20 344 chunks at 650 against 13 047 at 1200.
    It is a proxy rather than a proof -- two settings could coincide on a count --
    which is why publishing also refuses to reuse an existing tag.

    `vector_signature` closes the other hole in `index_signature`: the EXECUTION
    PROVIDER, left out so a CPU-only spoke can read an index a GPU box built. Two
    providers produce different vectors (cosine 0.999991, and a top-10 flip
    [M:ep-agreement]) and therefore different bytes, under an identical
    index_signature and an identical chunk count.
    """
    body = corpus_id.removeprefix("c:")
    material = f"{signature}|chunks={chunk_count}|vec={vector_signature}".encode()
    return f"index-{body}-{hashlib.sha256(material).hexdigest()[:8]}"


# --------------------------------------------------------------------------
# check -- offline, stdlib only (runs in root `make check`, before any venv)
# --------------------------------------------------------------------------
def problems_with(pointer: dict) -> list[str]:
    """Everything wrong with a pointer, as sentences. A pure function of the dict
    so the gate can be tested without a file, a release, or a network."""
    problems = [f"missing field: {field}" for field in REQUIRED if field not in pointer]
    for field in ("sha256_xz", "sha256_sqlite"):
        value = pointer.get(field, "")
        if isinstance(value, str) and len(value) != 64:
            problems.append(f"{field} is not a sha256 ({len(value)} chars)")
    # fetch does arithmetic with these (the free-space precheck), so a quoted
    # number here would surface as a TypeError halfway through a download.
    for field in ("bytes_xz", "bytes_sqlite", "chunk_count"):
        if field in pointer and not isinstance(pointer[field], int):
            problems.append(f"{field} is not a number ({pointer[field]!r})")
    if pointer.get("build_kind") != "canonical":
        problems.append(f"build_kind is {pointer.get('build_kind')!r}, must be 'canonical'")
    if pointer.get("asset") != ASSET:
        problems.append(f"asset is {pointer.get('asset')!r}, expected {ASSET!r}")
    # The tag is DERIVED. Recomputing it here is what catches a hand-edited
    # pointer whose fields no longer describe the artifact it names.
    derivation = ("corpus_id", "index_signature", "tag", "chunk_count", "vector_signature")
    if all(key in pointer for key in derivation) and isinstance(pointer["chunk_count"], int):
        expected = tag_for(
            pointer["corpus_id"],
            pointer["index_signature"],
            pointer["chunk_count"],
            pointer["vector_signature"],
        )
        if pointer["tag"] != expected:
            problems.append(
                f"tag {pointer['tag']!r} does not derive from corpus_id + index_signature + "
                f"chunk_count + vector_signature (expected {expected!r})"
            )
    return problems


def cmd_check(_args) -> int:
    if not POINTER.exists():
        print(f"[check] no {POINTER.name}: nothing published yet (that is a valid state)")
        return 0
    try:
        pointer = json.loads(POINTER.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"FAIL: {POINTER.name} is not valid JSON: {error}")
        return 1

    problems = problems_with(pointer)
    if problems:
        print(f"FAIL: {POINTER.name} ({len(problems)} problem(s))")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"[check] {POINTER.name} -> {pointer['tag']} ({pointer['bytes_xz'] / 1e6:.1f} MB asset)")
    return 0


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------
def _vector_signature_of_the_build() -> str:
    """Which vector space these vectors live in -- read from the CACHE, not computed.

    `make warm-cache` needs this to label a cache it derives from the published
    index, and the label has to be the one the build that produced those vectors
    used. Computing it here would read the CURRENT embedder, which is not
    necessarily the one that ran: switch the execution provider after building and
    publish, and every consumer gets a cache labelled with a provider that never
    touched it. The cache file is the build's own record, and `make
    index-canonical` always writes a fresh one, so for a canonical index it is
    always present and always right.
    """
    from pipeline import get_paths

    cache = get_paths().vector_cache
    if not cache.exists():
        raise SystemExit(
            f"[publish] no {cache.name}, so the vector space of this index cannot be recorded\n"
            "  from the build that made it. `make index-canonical` writes one; a cache disabled\n"
            "  build cannot be published (consumers could not warm a cache from it)."
        )
    connection = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'vector_signature'"
        ).fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        raise SystemExit(f"[publish] {cache.name} records no vector_signature. Nothing published.")
    return row[0]


def _eval_proof() -> dict:
    """Run the three floors and keep what they printed. A failure stops the
    publish -- the artifact cannot ship unproven, so there is no flag here to skip
    them.

    Exit code alone is NOT enough proof. Every one of these modules returns 0 when
    `evaluate()` returns None, which is what a corpus mismatch or a missing index
    looks like -- a sensible thing for a gate on a fresh clone, and a vacuous
    "floor held" here. So an empty rendering is a refusal too.

    `--json` is deliberately not used: only two of the three accept it, and the one
    that does pretty-prints, so parsing it would silently record nothing.
    """
    proof = {}
    for name, module in (
        ("retrieval", "pipeline.eval_retrieval"),
        ("graph", "pipeline.eval_graph"),
        ("ask", "pipeline.eval_graph_rag"),
    ):
        print(f"[publish] eval floor: {name} ...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", module, "--assert-baseline"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={**os.environ, "RERANK": "0"},
        )
        rendered = result.stdout.strip()
        if result.returncode != 0:
            tail = (rendered + "\n" + result.stderr.strip()).strip().splitlines()[-10:]
            raise SystemExit(
                f"[publish] the {name} floor did not hold; nothing published.\n  "
                + "\n  ".join(tail)
            )
        if not rendered:
            raise SystemExit(
                f"[publish] the {name} eval printed nothing, which is what it does when it evaluates\n"
                "  nothing (no index, or a corpus that does not match the judgments). That exit code\n"
                "  is not proof of a floor. Nothing published."
            )
        proof[name] = rendered.splitlines()[-12:]
    return proof


def cmd_publish(args) -> int:
    if not INDEX.exists():
        raise SystemExit(f"[publish] no index at {INDEX}. Run `make index-canonical` first.")

    meta = _index_meta(INDEX)
    build_kind = meta.get("build_kind")
    if build_kind != "canonical":
        raise SystemExit(
            f"[publish] this index was built {build_kind!r}, not 'canonical'. A cached vector was\n"
            "  encoded beside different neighbours than a cold build gives it, so its eval floor is\n"
            "  not reproducible by whoever downloads it. Run `make index-canonical`."
        )

    # corpus_id.py is a sibling tool, not a package member: import it by path.
    sys.path.insert(0, str(ROOT / "tools"))
    from corpus_id import corpus_id as compute_corpus_id

    try:
        corpus = compute_corpus_id()
    except ValueError as error:
        raise SystemExit(f"[publish] the corpus does not verify, so no id can be issued: {error}") from error

    dirty = _git("status", "--porcelain", "--", "pipeline", "source")
    if dirty:
        raise SystemExit(
            "[publish] pipeline/ or source/ has uncommitted changes, so the published index would\n"
            "  name a commit that does not describe it:\n  " + dirty.replace("\n", "\n  ")
        )

    signature = meta["index_signature"]
    chunk_count = int(meta.get("chunk_count", 0))
    vector_signature = _vector_signature_of_the_build()
    tag = tag_for(corpus, signature, chunk_count, vector_signature)
    repo = _repo_slug()

    if shutil.which("gh") is None:
        raise SystemExit("[publish] `gh` is not installed; it is what uploads the asset.")
    exists = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo], capture_output=True, text=True
    )
    if exists.returncode == 0:
        raise SystemExit(
            f"[publish] release {tag} already exists. Tags are never reused -- a pinned pointer must\n"
            "  always name the same bytes. Delete it deliberately if it was a mistake."
        )

    proof = _eval_proof()

    archive = INDEX.with_suffix(".sqlite.xz")
    print(f"[publish] compressing {INDEX.stat().st_size:,} B -> {archive.name} (xz -{PRESET}) ...", flush=True)
    with INDEX.open("rb") as source, lzma.open(archive, "wb", preset=PRESET) as target:
        shutil.copyfileobj(source, target, CHUNK)

    pointer = {
        "tag": tag,
        "asset": ASSET,
        "repo": repo,
        "sha256_xz": _sha256(archive),
        "sha256_sqlite": _sha256(INDEX),
        "bytes_xz": archive.stat().st_size,
        "bytes_sqlite": INDEX.stat().st_size,
        "corpus_id": corpus,
        "index_signature": signature,
        # Not in index_signature (a spoke must be able to read a GPU-built index),
        # so it is recorded here -- `make warm-cache` labels a derived cache with it.
        "vector_signature": vector_signature,
        "build_kind": build_kind,
        "chunk_count": chunk_count,
        "nodes": int(meta.get("node_count", 0)),
        "edges": int(meta.get("edge_count", 0)),
        "embedding_provider": meta.get("embedding_provider", ""),
        "embedding_dim": int(meta.get("embedding_dim", 0)),
        "commit": _git("rev-parse", "HEAD"),
        "eval": proof,
    }

    notes = (
        f"`data/serving/index.sqlite` built canonical from corpus `{corpus}`.\n\n"
        f"- signature: `{signature}`\n"
        f"- {pointer['chunk_count']:,} chunks, {pointer['nodes']} nodes, {pointer['edges']} edges\n"
        f"- {pointer['bytes_sqlite']:,} B raw / {pointer['bytes_xz']:,} B xz\n"
        f"- sha256 (sqlite): `{pointer['sha256_sqlite']}`\n"
        f"- commit: `{pointer['commit']}`\n\n"
        "These numbers are informational. The checksum `make fetch-index` verifies against is\n"
        "`index_release.json` in the repo, not this page: a release asset can be re-uploaded under\n"
        "the same tag, a tracked file cannot change without a commit.\n"
    )

    if not args.yes:
        print(json.dumps(pointer, indent=2, ensure_ascii=False))
        print(f"\n[publish] DRY RUN. Nothing uploaded, {POINTER.name} not written.")
        print(f"[publish] re-run with --yes to create release {tag} on {repo}.")
        archive.unlink()
        return 0

    subprocess.run(
        ["gh", "release", "create", tag, str(archive),
         "--repo", repo, "--title", tag, "--notes", notes],
        check=True,
    )
    POINTER.write_text(json.dumps(pointer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    archive.unlink()
    print(f"\n[publish] released {tag} and wrote {POINTER.name}.")
    print(f"[publish] COMMIT {POINTER.name} -- until it is committed the bytes have no trusted checksum.")
    return 0


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def _local_signature() -> tuple[str | None, str]:
    """What this machine's settings would build, and why not when it cannot.

    None is not a mismatch: the configured onnx model is a one-time download
    (`make warm-models`) and invariant 7 forbids reaching for it here, so a tree
    that has never warmed cannot compute this. Saying WHICH of the two happened is
    the difference between an actionable message and a shrug.
    """
    try:
        from pipeline import get_settings
        from pipeline.build_rag import get_embedder, index_signature

        settings = get_settings()
        return index_signature(get_embedder(settings), settings.embedding_dim), ""
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


def _download(pointer: dict, target: Path) -> None:
    """`gh` when it works, plain HTTPS otherwise. The fallback is not only for a
    machine without `gh`: an INSTALLED but unauthenticated gh fails on a call that
    needs no auth at all, since the asset of a public release is a plain file."""
    url = f"https://github.com/{pointer['repo']}/releases/download/{pointer['tag']}/{pointer['asset']}"
    if shutil.which("gh"):
        gh = subprocess.run(
            ["gh", "release", "download", pointer["tag"], "--repo", pointer["repo"],
             "--pattern", pointer["asset"], "--output", str(target), "--clobber"],
            capture_output=True,
            text=True,
        )
        if gh.returncode == 0:
            return
        print(f"[fetch] gh could not download it ({gh.stderr.strip().splitlines()[-1:] or ['?']}); "
              "falling back to HTTPS", flush=True)
    print(f"[fetch] GET {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as handle:  # noqa: S310 -- fixed https host
        shutil.copyfileobj(response, handle, CHUNK)


class Unverifiable(Exception):
    """The index could not be CHECKED, which is not the same as being wrong.

    A tree that has never run `make warm-models` has no embedder to compute a
    signature with, and invariant 7 forbids downloading one here. The bytes are
    still proven -- they matched a sha256 that lives in git -- so throwing them
    away would cost another 92 MB to learn nothing. The settings question is
    deferred to the read path, which asks it on every single query anyway.
    """


def _verify_through_read_path() -> None:
    """Open the installed index the way the agent will, then run the repo's own
    end-to-end assertions.

    Neither half is new code, on purpose. `assert_index_current` is the guard that
    already refuses an index built by different retrieval logic, and it is called
    first because it names the disagreement precisely. `smoke_test.py` is what
    `make build` runs: it asserts hybrid retrieval and multi-hop graph traversal
    against this index, which is the only thing that proves the vectors, the FTS
    table and the graph all arrived intact and agree with each other. A hand-rolled
    `SELECT count(*)` here would pass on an index whose vector half was truncated.
    """
    from pipeline import get_paths, get_settings
    from pipeline.build_rag import assert_index_current, connect_index, get_embedder

    settings = get_settings()
    try:
        embedder = get_embedder(settings)
    except Exception as error:
        raise Unverifiable(f"{type(error).__name__}: {error}") from error

    connection = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        assert_index_current(connection, embedder, settings.embedding_dim)
    finally:
        connection.close()

    smoke = subprocess.run(
        [sys.executable, "pipeline/smoke_test.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "RERANK": "0"},
    )
    if smoke.returncode != 0:
        tail = (smoke.stdout + smoke.stderr).strip().splitlines()[-12:]
        raise ValueError("smoke test failed:\n    " + "\n    ".join(tail))


def cmd_fetch(args) -> int:
    if not POINTER.exists():
        raise SystemExit(f"[fetch] no {POINTER.name}: nothing has been published for this repo.")
    pointer = json.loads(POINTER.read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED if field not in pointer]
    if missing:
        raise SystemExit(f"[fetch] {POINTER.name} is missing {', '.join(missing)}; run `make check`.")

    if INDEX.exists() and _sha256(INDEX) == pointer["sha256_sqlite"]:
        print(f"[fetch] already current: local index is {pointer['tag']}. Nothing to do.")
        return 0

    # Cheapest refusal first: 92 MB is not worth downloading to learn the settings
    # disagree. None means the embedder is not warmed yet, which is not a mismatch.
    local, why = _local_signature()
    if local is not None and local != pointer["index_signature"]:
        raise SystemExit(
            "[fetch] the published index was built by different retrieval settings and would be\n"
            "  refused at query time anyway. Nothing downloaded.\n"
            f"  published: {pointer['index_signature']}\n"
            f"  this tree: {local}\n"
            "  Align .env with the publisher, or build your own with `make build`."
        )
    if local is None:
        print(f"[fetch] the signature was not pre-checked here ({why}).")
        print("[fetch] it is still checked after install, through the read path -- `make warm-models` if that fails.")

    serving = INDEX.parent
    serving.mkdir(parents=True, exist_ok=True)
    if not all(isinstance(pointer[field], int) for field in ("bytes_xz", "bytes_sqlite")):
        raise SystemExit(f"[fetch] {POINTER.name} has non-numeric byte counts; run `make check`.")
    need = pointer["bytes_xz"] + pointer["bytes_sqlite"] + (INDEX.stat().st_size if INDEX.exists() else 0)
    free = shutil.disk_usage(serving).free
    if free < need:
        raise SystemExit(
            f"[fetch] needs {need / 1e6:.0f} MB free (download + decompressed + the index it keeps as\n"
            f"  a rollback), {free / 1e6:.0f} MB available. Nothing started."
        )

    archive = serving / ".index.download"
    staged = serving / ".index.new"
    previous = serving / "index.sqlite.previous"
    try:
        print(f"[fetch] {pointer['tag']} <- {pointer['repo']} ({pointer['bytes_xz'] / 1e6:.1f} MB)", flush=True)
        _download(pointer, archive)
        actual = _sha256(archive)
        if actual != pointer["sha256_xz"]:
            raise SystemExit(
                "[fetch] the downloaded asset does not match the checksum committed in\n"
                f"  {POINTER.name}. The release was replaced, or the transfer is corrupt. Nothing installed.\n"
                f"  expected {pointer['sha256_xz']}\n  got      {actual}"
            )
        print("[fetch] decompressing ...", flush=True)
        with lzma.open(archive, "rb") as source, staged.open("wb") as target:
            shutil.copyfileobj(source, target, CHUNK)
        actual = _sha256(staged)
        if actual != pointer["sha256_sqlite"]:
            raise SystemExit(
                f"[fetch] the decompressed index does not match {POINTER.name}. Nothing installed.\n"
                f"  expected {pointer['sha256_sqlite']}\n  got      {actual}"
            )

        # Atomic, and the old index survives until the new one has answered a
        # query. A held connection notices the swap by inode and fails loudly
        # (connect_index) rather than answering from the replaced file.
        had_previous = INDEX.exists()
        if had_previous:
            os.replace(INDEX, previous)
        os.replace(staged, INDEX)
        try:
            _verify_through_read_path()
        except Unverifiable as error:
            # Kept, not rolled back: see Unverifiable. Loud, because an index whose
            # settings have not been checked is a fact the operator must carry.
            print(f"\n[fetch] INSTALLED BUT NOT CHECKED against this tree's settings ({error}).")
            print("[fetch] the bytes match the checksum in git, so they are the published ones.")
            print("[fetch] run `make warm-models`, then `make smoke` to close the gap.")
        except Exception as error:
            if had_previous:
                os.replace(previous, INDEX)
            else:
                INDEX.unlink(missing_ok=True)
            raise SystemExit(
                f"[fetch] the index installed but did not pass the read path, so it was rolled back\n"
                f"  and the index you had is back in place:\n  {error}\n"
                "  This is a real disagreement, not a missing model (that is reported separately):\n"
                "  align .env with the publisher, or build your own with `make build`."
            ) from error
        if had_previous:
            previous.unlink()
    finally:
        archive.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)

    print(f"[fetch] installed {pointer['tag']}: {pointer['chunk_count']:,} chunks, corpus {pointer['corpus_id']}.")
    print("[fetch] `make eval` should reproduce the published floors exactly -- these are the same bytes.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish", help="Upload the canonical index and write the tracked pointer")
    publish.add_argument("--yes", action="store_true", help="Actually create the release (default: dry run)")
    publish.set_defaults(handler=cmd_publish)

    # No --force. The only thing it could bypass is the signature check, and an
    # index the read path will refuse anyway is not worth 92 MB and a rollback.
    sub.add_parser(
        "fetch", help="Install the published index, verified against the tracked pointer"
    ).set_defaults(handler=cmd_fetch)

    sub.add_parser("check", help="Validate the pointer file (offline, stdlib only)").set_defaults(handler=cmd_check)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
