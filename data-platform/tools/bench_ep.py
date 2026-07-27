"""Measure the execution providers on THIS machine instead of assuming them.

The whole GPU design rests on one claim that is only true sometimes: that the
device is faster than the CPU for this workload. It was measured false here for
the int8 asset on CoreML (869 ms/chunk against 517 ms/chunk on one CPU thread),
and the general rule behind that -- int8 is a CPU format, GPUs want fp16 -- holds
on CUDA and DirectML too. So the answer for any given box is a measurement, and
this is the thing that makes it.

Prints one row per (provider, asset) that is actually runnable here. Nothing is
downloaded: an asset that is not in the local Hugging Face cache is reported as
missing, with the command that would fetch it.

    make bench-ep
    uv run python tools/bench_ep.py --texts 32 --repeat 2
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from pipeline import get_paths, get_settings, runtime
from pipeline.build_rag import OnnxEmbedder, connect_index

SAMPLE = "토큰증권 발행인은 청약의 권유를 하기 전에 증권신고서를 제출하여야 하며, " * 8


def _texts(count: int) -> list:
    """Real chunks if there is an index, otherwise one representative passage."""
    paths = get_paths()
    if paths.index_sqlite.exists():
        connection = connect_index(paths.index_sqlite, read_only=True)
        try:
            rows = [
                row[0]
                for row in connection.execute(
                    "SELECT content FROM chunks ORDER BY chunk_id LIMIT ?", (count,)
                )
            ]
            if rows:
                return rows
        finally:
            connection.close()
    return [SAMPLE] * count


def _bench(provider: str, precision: str, texts: list, repeat: int) -> dict:
    os.environ["ORT_PROVIDER"] = provider
    runtime.detect.cache_clear()
    settings = get_settings()
    started = time.perf_counter()
    try:
        embedder = OnnxEmbedder(settings.embedding_model, allow_download=False, precision=precision)
    except RuntimeError as error:
        return {"provider": provider, "asset": precision, "status": str(error).split(".")[0]}
    load = time.perf_counter() - started
    if embedder.provider != provider:
        return {
            "provider": provider,
            "asset": precision,
            "status": f"session fell back to {embedder.provider}",
        }

    embedder.encode(texts[: embedder._batch])  # warm up: first run compiles and allocates
    best = None
    for _ in range(repeat):
        started = time.perf_counter()
        embedder.encode(texts)
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    batches = max(1, (len(texts) + embedder._batch - 1) // embedder._batch)
    return {
        "provider": provider,
        "asset": precision,
        "status": "ok",
        "load_s": round(load, 2),
        "ms_per_text": round(best / len(texts) * 1000, 1),
        "texts_per_s": round(len(texts) / best, 2),
        # Printed because it is the number that makes a CPU row comparable: the
        # CPU tier is only as fast as the worker count the run actually reached.
        "workers": embedder._encode_workers(batches) if provider == runtime.CPU_EP else 1,
    }


def _bench_isolated(provider: str, precision: str, texts: int, repeat: int) -> dict:
    """Run one row in a CHILD PROCESS, because a provider can take the process down.

    Not hypothetical: measured here (2026-07-27), fp16 on CoreML segfaults
    (SIGSEGV) after partitioning the graph into 149 pieces. A survey tool whose
    whole point is "try each provider" cannot die on the first one that misbehaves
    -- and a native crash is not catchable in-process, so isolation is the only
    way to report it as a row instead of as a missing report.
    """
    import tempfile
    from pathlib import Path

    # Result via FILE, not stdout: onnxruntime's native layer writes to fd 1/2
    # without always ending the line, so a JSON row printed by the child can
    # arrive glued to a provider warning and read as a failure that never happened.
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "row.json"
        completed = subprocess.run(  # noqa: S603
            [sys.executable, __file__, "--one", provider, precision,
             "--texts", str(texts), "--repeat", str(repeat), "--out", str(out)],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.exists() and out.stat().st_size:
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # A child killed mid-write leaves a truncated file.
                return {"provider": provider, "asset": precision, "status": "no result (truncated)"}
    # runtime.crash_signal, not `returncode < 0`: on Windows a real access
    # violation arrives as an unsigned NTSTATUS, which the POSIX test reads as a
    # clean non-zero exit -- on the one platform where the unverified NPU lives.
    signal_number = runtime.crash_signal(completed.returncode)
    if signal_number is not None:
        status = f"CRASHED (signal/status {signal_number})"
    elif completed.returncode != 0:
        status = f"failed (exit {completed.returncode})"
    else:
        status = "no result"
    return {"provider": provider, "asset": precision, "status": status}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 160, not 32. The CPU row runs _encode_workers = min(cores-2, 8, n_batches)
    # PARALLEL sessions, so 32 passages is 2 batches and therefore 2 workers -- a
    # 4x understatement of the CPU tier against the build this tool advises on. A
    # GPU that beats one CPU thread can lose badly to eight parallel CPU sessions,
    # and that is exactly the trap this tool exists to keep an operator out of.
    parser.add_argument("--texts", type=int, default=160, help="Passages per timed run (default 160).")
    parser.add_argument("--repeat", type=int, default=2, help="Timed runs; the best is reported.")
    parser.add_argument("--one", nargs=2, metavar=("PROVIDER", "PRECISION"),
                        help="Internal: benchmark a single row and write JSON to --out.")
    parser.add_argument("--out", help="Internal: where --one writes its row.")
    args = parser.parse_args(argv)

    if args.one:
        from pathlib import Path

        provider, precision = args.one
        row = _bench(provider, precision, _texts(args.texts), args.repeat)
        Path(args.out).write_text(json.dumps(row), encoding="utf-8")
        return 0

    providers = [runtime.CPU_EP, *runtime.candidate_providers()]
    rows = []
    for provider in providers:
        for precision in ("int8", "fp16"):
            rows.append(_bench_isolated(provider, precision, args.texts, args.repeat))
    texts = _texts(args.texts)

    width = max(len(row["provider"]) for row in rows)
    print(f"\n{len(texts)} passages, best of {args.repeat}\n")
    print(f"{'provider'.ljust(width)}  asset  {'ms/text':>9}  {'texts/s':>8}  {'workers':>7}  status")
    for row in rows:
        print(
            f"{row['provider'].ljust(width)}  {row['asset']:5}  "
            f"{row.get('ms_per_text', '-'):>9}  {row.get('texts_per_s', '-'):>8}  "
            f"{row.get('workers', '-'):>7}  {row['status']}"
        )
    ok = [row for row in rows if row["status"] == "ok"]
    if ok:
        winner = min(ok, key=lambda row: row["ms_per_text"])
        print(
            f"\nfastest here: {winner['provider']} / {winner['asset']} "
            f"({winner['ms_per_text']} ms/text)"
        )
        cpu = [row for row in ok if row["provider"] == runtime.CPU_EP]
        if cpu and winner["provider"] != runtime.CPU_EP:
            speedup = min(row["ms_per_text"] for row in cpu) / winner["ms_per_text"]
            print(f"that is {speedup:.1f}x the best CPU row.")
            if speedup < 3:
                print(
                    "under 3x: this is the regime where ENCODE_DEVICES=hybrid (Tier B) can "
                    "still add the CPU's share. Above it, the CPU contributes little."
                )
    print(
        "\nassets missing above are fetched by `make warm-models` with the matching "
        "EMBEDDING_PRECISION; the read path never downloads."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
