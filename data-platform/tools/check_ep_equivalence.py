"""Do two execution providers running the SAME asset agree closely enough?

The bit-stable-rebuild guarantee is a same-machine, same-device-profile promise.
Across hardware it cannot hold -- a GPU kernel sums in a different order than a
CPU one -- so the cross-hardware contract has to be written down as a tolerance
and then actually checked. That is this file.

Two properties, because one of them is not enough:

  * VECTOR AGREEMENT. Cosine between the two providers' vectors for the same
    passage must be >= --min-cosine (default 0.9999). This catches a provider
    that quietly computes something else -- a wrong pooling, a truncated graph, a
    partition boundary that drops a residual.
  * RANK AGREEMENT. The top-10 for a set of queries, scored against a fixed
    matrix of document vectors, must be IDENTICAL. Retrieval is a ranking, and a
    ranking can flip while every cosine still rounds to 1.0000 -- which is the
    failure that would actually reach a user.

Usage (needs both providers registered on this machine):
    uv run python tools/check_ep_equivalence.py --a CPUExecutionProvider \
        --b CUDAExecutionProvider --precision fp16

It is skipped, not failed, when the second provider is not available: most
machines have exactly one, and a CI runner with no GPU must still be able to run
the rest of the gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from pipeline import get_paths, get_settings, runtime
from pipeline.build_rag import OnnxEmbedder, connect_index

# Deliberately domain text: a generic sentence exercises none of the Korean
# tokenisation this corpus depends on.
FALLBACK_TEXTS = [
    "토큰증권 발행인은 전매제한 조치를 하여야 한다.",
    "가상자산사업자는 이용자의 예치금을 고유재산과 분리하여 보관하여야 한다.",
    "이 법은 공포 후 1년이 경과한 날부터 시행한다.",
    "금융위원회는 대통령령으로 정하는 바에 따라 필요한 조치를 명할 수 있다.",
    "투자계약증권의 모집 또는 매출에 관하여는 증권신고서를 제출하여야 한다.",
]
FALLBACK_QUERIES = ["전매제한", "예치금 분리보관", "시행일", "증권신고서 제출"]


def _corpus(limit: int) -> tuple:
    """Real chunks when an index exists, a small fixed set otherwise."""
    paths = get_paths()
    if not paths.index_sqlite.exists():
        return FALLBACK_TEXTS, FALLBACK_QUERIES
    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        rows = [
            row[0]
            for row in connection.execute(
                "SELECT content FROM chunks ORDER BY chunk_id LIMIT ?", (limit,)
            )
        ]
    finally:
        connection.close()
    return (rows or FALLBACK_TEXTS), FALLBACK_QUERIES


def _embedder(provider: str, precision: str, model: str) -> OnnxEmbedder:
    import os

    # ORT_PROVIDER is how runtime.detect is told to stop choosing; clearing the
    # detect cache is required because it memoises per process.
    os.environ["ORT_PROVIDER"] = provider
    # Pin the batch on BOTH sides. The tokenizer pads to the longest sequence in
    # the batch, so an unequal batch size would inject a ~1% difference that has
    # nothing to do with the execution providers being compared -- it would swamp
    # the very thing this file measures.
    os.environ.setdefault("ENCODE_BATCH", str(runtime.ENCODE_BATCH_DEFAULT))
    runtime.detect.cache_clear()
    embedder = OnnxEmbedder(model, allow_download=False, precision=precision)
    if embedder.provider != provider:
        raise SystemExit(f"asked for {provider}, session came up on {embedder.provider}")
    return embedder


def _cosine(left, right) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = (sum(a * a for a in left) ** 0.5) * (sum(b * b for b in right) ** 0.5)
    return dot / norm if norm else 0.0


def _top_k(query_vector, matrix, k: int) -> list:
    scored = [(_cosine(query_vector, vector), index) for index, vector in enumerate(matrix)]
    # Ties break on index so the order is total, exactly like the index's
    # chunk_id tiebreak. Without it a tie would flap for reasons unrelated to the EP.
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [index for _, index in scored[:k]]


def _encode_isolated(provider: str, precision: str, docs: int, model: str) -> dict:
    """Encode in a CHILD PROCESS. A provider that segfaults must FAIL, not vanish.

    Measured here: fp16 on CoreML segfaults on real chunk lengths. This check runs
    inside `make verify`, so an uncatchable native crash would take the whole gate
    down with a bare signal instead of a finding.
    """
    import tempfile
    from pathlib import Path

    # The child hands its result over in a FILE, not on stdout. onnxruntime's
    # native layer writes to fd 1/2 without always terminating the line, so a
    # JSON document printed by the child can arrive glued to the tail of a
    # provider warning -- which looked exactly like a crash and was not.
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "result.json"
        completed = subprocess.run(  # noqa: S603
            [sys.executable, __file__, "--encode-only", provider, precision,
             "--docs", str(docs), "--out", str(out)],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.exists() and out.stat().st_size:
            return json.loads(out.read_text(encoding="utf-8"))
    detail = (
        f"crashed with signal {-completed.returncode}"
        if completed.returncode < 0
        else f"exited {completed.returncode} without a result"
    )
    tail = (completed.stderr or "").strip().splitlines()
    return {"error": f"{provider}/{precision} {detail}", "stderr": tail[-1] if tail else ""}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--a", default=runtime.CPU_EP, help="Reference execution provider.")
    parser.add_argument("--b", default=None, help="Provider to compare (default: this box's GPU EP).")
    parser.add_argument("--precision", default=None, choices=("int8", "fp16"))
    parser.add_argument("--min-cosine", type=float, default=0.9999)
    parser.add_argument("--docs", type=int, default=64)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--encode-only",
        nargs=2,
        metavar=("PROVIDER", "PRECISION"),
        help="Internal: encode the fixed corpus on one provider and write JSON to --out.",
    )
    parser.add_argument("--out", help="Internal: where --encode-only writes its result.")
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.encode_only:
        from pathlib import Path

        provider, precision = args.encode_only
        texts, queries = _corpus(args.docs)
        try:
            embedder = _embedder(provider, precision, settings.embedding_model)
            payload = {"docs": embedder.encode(texts), "queries": embedder.encode(queries)}
        except RuntimeError as error:
            payload = {"error": str(error)}
        Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
        return 0
    provider_b = args.b or runtime.gpu_provider()
    if not provider_b or provider_b == args.a:
        print(
            f"[ep-equiv] only {args.a} is available on this machine; nothing to compare. "
            "SKIP (this is the expected result on a CPU-only runner)."
        )
        return 0

    precision = args.precision or runtime.precision_for(provider_b)
    texts, queries = _corpus(args.docs)
    print(f"[ep-equiv] {args.a} vs {provider_b}, asset={precision}, {len(texts)} passages")

    left_result = _encode_isolated(args.a, precision, args.docs, settings.embedding_model)
    right_result = _encode_isolated(provider_b, precision, args.docs, settings.embedding_model)
    for result in (left_result, right_result):
        if "error" in result:
            message = result["error"]
            if "not in the local Hugging Face cache" in message:
                # A missing local asset is not a failed comparison; it is an unrun one.
                print(f"[ep-equiv] cannot run: {message.splitlines()[0]}\nSKIP")
                return 0
            print(f"FAIL: {message}")
            if result.get("stderr"):
                print(f"       {result['stderr']}")
            return 1

    left_vectors, left_queries = left_result["docs"], left_result["queries"]
    right_vectors, right_queries = right_result["docs"], right_result["queries"]
    worst = min(
        _cosine(a, b) for a, b in zip(left_vectors, right_vectors, strict=True)
    )

    rank_mismatches = []
    for query, a_vector, b_vector in zip(queries, left_queries, right_queries, strict=True):
        a_rank = _top_k(a_vector, left_vectors, args.k)
        b_rank = _top_k(b_vector, right_vectors, args.k)
        if a_rank != b_rank:
            rank_mismatches.append((query, a_rank, b_rank))

    print(f"[ep-equiv] worst cosine: {worst:.6f} (floor {args.min_cosine})")
    print(f"[ep-equiv] top-{args.k} agreement: {len(queries) - len(rank_mismatches)}/{len(queries)}")

    failed = False
    if worst < args.min_cosine:
        print(f"FAIL: vectors disagree beyond tolerance ({worst:.6f} < {args.min_cosine})")
        failed = True
    for query, a_rank, b_rank in rank_mismatches:
        print(f"FAIL: top-{args.k} differs for {query!r}\n  {args.a}: {a_rank}\n  {provider_b}: {b_rank}")
        failed = True
    if failed:
        return 1
    print("OK: the two providers agree within the cross-hardware contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
