"""Does running two stages at once help, or do they just slow each other down?

The appealing shape of heterogeneous execution is "load the models everywhere and
run them all at the same time". Whether that pays is an empirical question with a
famously non-uniform answer: published multi-DNN measurements have one
accelerator losing 27% under four concurrent models while another loses 1202%.
So this repo does not enable concurrent stages on a hunch -- it measures the
interference first, on the machine that would run it.

What is measured, per stage: throughput ALONE, then throughput while the other
stage runs the same work concurrently. Two numbers come out of that:

  * SLOWDOWN -- how much each stage loses to the other. This is the interference
    the papers warn about, and it is what makes "just run both" a bad default.
  * COMBINED THROUGHPUT -- whether the pair, taken together, finished more work
    per second than running them one after the other would have. A stage can lose
    30% and the pair can still win, if the devices were idle waiting on each
    other; that is the entire case for concurrency.

Both stages share one process here on purpose: the embedder and the reranker sit
on the same query path, so that is the arrangement that would actually exist.
onnxruntime releases the GIL inside Run(), so two Python threads genuinely
overlap.

    make bench-concurrent
    uv run python tools/bench_concurrency.py --passages 24 --pairs 12
"""

from __future__ import annotations

import argparse
import threading
import time

from pipeline import get_paths, get_settings
from pipeline.build_rag import OnnxEmbedder, connect_index, resolve_precision
from pipeline.reranker import OnnxReranker
from pipeline.reranker import resolve_precision as rerank_precision

QUERY = "가상자산사업자의 예치금 분리보관 의무"


def _passages(count: int) -> list:
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
    return ["토큰증권 발행인은 전매제한 조치를 하여야 한다. " * 12] * count


def _timed(work) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--passages", type=int, default=24, help="Passages the embedder encodes per run.")
    parser.add_argument("--pairs", type=int, default=12, help="Pairs the reranker scores per run.")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1.3,
        help="Slowdown above which concurrency is reported as not worth it (default 1.3x).",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    passages = _passages(args.passages)
    try:
        embedder = OnnxEmbedder(
            settings.embedding_model, allow_download=False, precision=resolve_precision(settings)
        )
        reranker = OnnxReranker(
            settings.rerank_model, allow_download=False, precision=rerank_precision(settings)
        )
    except RuntimeError as error:
        print(f"[concurrency] cannot run: {error}\nSKIP")
        return 0

    pairs = [(QUERY, text) for text in passages[: args.pairs]]
    print(
        f"[concurrency] embedder on {embedder.provider}/{embedder.precision}, "
        f"reranker on {reranker.provider}/{reranker.precision}"
    )

    def embed() -> None:
        embedder.encode(passages)

    def rerank() -> None:
        reranker.score(pairs)

    embed()  # warm both: first run allocates and compiles
    rerank()

    # Each stage is timed alone BEFORE and AFTER the concurrent run, and the
    # faster of the two is the baseline. A single alone-run measured first is not
    # trustworthy here: the first pass through a 500 MB model still takes page
    # faults, and a machine that has just been busy is not in the same state as
    # one that has been idle. Without this the tool reported a stage running
    # FASTER while sharing the CPU -- an ordering artefact presented as a result.
    first_embed = _timed(embed)
    first_rerank = _timed(rerank)

    together: dict = {}

    def run(name, work) -> None:
        together[name] = _timed(work)

    threads = [
        threading.Thread(target=run, args=("embed", embed)),
        threading.Thread(target=run, args=("rerank", rerank)),
    ]
    wall_started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall_together = time.perf_counter() - wall_started

    second_embed = _timed(embed)
    second_rerank = _timed(rerank)
    alone_embed = min(first_embed, second_embed)
    alone_rerank = min(first_rerank, second_rerank)
    spread = max(
        abs(first_embed - second_embed) / alone_embed,
        abs(first_rerank - second_rerank) / alone_rerank,
    )

    wall_sequential = alone_embed + alone_rerank
    embed_slow = together["embed"] / alone_embed
    rerank_slow = together["rerank"] / alone_rerank
    speedup = wall_sequential / wall_together

    print()
    print(f"{'stage':10} {'alone':>9} {'concurrent':>12} {'slowdown':>10}   alone runs")
    print(
        f"{'embedder':10} {alone_embed:8.2f}s {together['embed']:11.2f}s {embed_slow:9.2f}x"
        f"   {first_embed:.2f}s / {second_embed:.2f}s"
    )
    print(
        f"{'reranker':10} {alone_rerank:8.2f}s {together['rerank']:11.2f}s {rerank_slow:9.2f}x"
        f"   {first_rerank:.2f}s / {second_rerank:.2f}s"
    )
    if spread > 0.1:
        print(
            f"\nWARNING: the two alone-runs of a stage differ by {spread:.0%}. This machine is "
            f"noisy (thermal, other load), so treat any slowdown below that as nothing."
        )
    print()
    print(f"sequential total : {wall_sequential:.2f}s")
    print(f"concurrent wall  : {wall_together:.2f}s")
    print(f"combined speedup : {speedup:.2f}x")
    print()

    worst = max(embed_slow, rerank_slow)
    if speedup <= 1.0:
        print(
            f"VERDICT: concurrency LOSES here ({speedup:.2f}x). The two stages contend for the "
            f"same device; run them one after the other."
        )
    elif worst > args.tolerance:
        print(
            f"VERDICT: concurrency wins overall ({speedup:.2f}x) but one stage pays "
            f"{worst:.2f}x. Fine for batch work, bad for anything latency-sensitive -- a query "
            f"waiting behind a build would feel that."
        )
    else:
        print(
            f"VERDICT: concurrency is worth it ({speedup:.2f}x combined, worst stage "
            f"{worst:.2f}x). Placing these stages on DIFFERENT device classes "
            f"(DEVICE_PLACEMENT) should widen the gap further."
        )
    print(
        "\nNote: both stages ran on the same device here. The case concurrency is really for is "
        "stages on DIFFERENT classes (embedder on a GPU, reranker on an NPU), where they do not "
        "contend at all -- rerun this with DEVICE_PLACEMENT set on a machine that has them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
