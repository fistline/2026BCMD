"""Does encoding on N threads produce the same bytes as encoding on one?

`OnnxEmbedder._encode_workers` returns `min(cores - 2, 8, n_batches)`, so the
number of cores decides WHICH CODE PATH runs: a 4-core box takes the sequential
branch and a 12-core box takes `ThreadPoolExecutor.map`. Core count is exactly the
axis that varies across a Windows/macOS/Linux fleet, and the two paths must agree
bit-for-bit or the same corpus yields different vectors on different machines.

build_rag.py asserted that agreement in a comment and cited "the ENCODE_WORKERS=1
vs N equality check" -- which did not exist anywhere in the repo. This is that
check. It is an operator tool rather than a build gate on purpose: it encodes the
sample twice, which costs minutes, and `make verify` already runs long.

It needs the model in the local Hugging Face cache (any `make build` warms it) and
an index to draw real passages from. Run it once on a machine whose core count
puts it on the other branch:

    uv run python tools/check_workers_equality.py            # 64 passages
    uv run python tools/check_workers_equality.py --n 32
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _digest(vectors) -> str:
    running = hashlib.sha256()
    for vector in vectors:
        running.update(struct.pack(f"<{len(vector)}d", *(float(value) for value in vector)))
    return running.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=64, help="Passages to encode (default 64).")
    parser.add_argument("--workers", type=int, default=8, help="The N in 1-vs-N (default 8).")
    arguments = parser.parse_args()

    from pipeline import get_paths
    from pipeline.build_rag import OnnxEmbedder

    index = get_paths().index_sqlite
    if not index.exists():
        print(f"FAIL: no index at {index}. Run `make build` first -- this reads real passages.")
        return 1

    connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT title, rel_path, heading, content FROM chunks ORDER BY chunk_id LIMIT ?",
            (arguments.n,),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) < 2:
        print("FAIL: fewer than two passages in the index.")
        return 1

    texts = [
        "# " + (title or rel_path) + "\n" + ("## " + heading if heading != title else "") + "\n" + (content or "")
        for title, rel_path, heading, content in rows
    ]

    model = os.environ.get("EMBEDDING_MODEL", "Xenova/bge-m3")
    digests = {}
    for workers in (1, arguments.workers):
        os.environ["ENCODE_WORKERS"] = str(workers)
        digests[workers] = _digest(OnnxEmbedder(model).encode(texts))
        print(f"  ENCODE_WORKERS={workers:<3} {digests[workers][:40]}")

    if digests[1] == digests[arguments.workers]:
        print(
            f"OK: {len(texts)} passages encode bit-identically on 1 and "
            f"{arguments.workers} workers ({os.cpu_count()} cores on this box)"
        )
        return 0
    print(
        "FAIL: the threaded path disagrees with the sequential one. Every machine whose\n"
        "core count selects the other branch would hold DIFFERENT vectors for this corpus,\n"
        "and index_signature cannot see it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
