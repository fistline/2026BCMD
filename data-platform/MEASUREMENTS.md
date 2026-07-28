# Measurements

Every performance number this repo states lives here once, with the command that
reproduces it. Elsewhere, cite the id (`[M:cold-rebuild]`) rather than restating
the figure.

The reason is not tidiness. Four numbers in this repo were wrong at the same
time — `~37 minutes` in three places for a rebuild measured at 1433.9 s, and
`502 vs 466 ms/passage` for a pair measured at 250.2 vs 257.3 with the sign the
other way — because each had two to five homes and the real measurement arrived
after the prose. A figure with one home cannot disagree with itself.

A row is only added by someone who ran the command. If a number is an estimate,
it does not belong here; say "estimated" wherever it is used.

| id | value | machine / date | reproduce |
|---|---|---|---|
| `M:cold-rebuild` | 1433.9 s cold → 51.5 s cached; 12,643 vectors, byte-identical | M4 Pro 12-core / 24 GB, macOS 15, onnxruntime 1.27.0, 2026-07-27 | `rm data/processed/vector_cache.sqlite && time make index` then `time make index` |
| `M:coreml-int8` | 148 partitions; 869 ms/chunk vs 517 ms/chunk on one CPU thread; 13.5 GB peak RSS; SIGKILL at 32 chunks | same, 2026-07-27 | `ORT_PROVIDER=CoreMLExecutionProvider make bench-ep` |
| `M:coreml-fp16` | 149 partitions; SIGSEGV on one run, 14275 ms/text (57× CPU) on another | same, 2026-07-28 | `make bench-ep` (the CoreML rows) |
| `M:fp16-query` | short query: 12.7 ms (int8) vs 203.4 ms (fp16) on the CPU — 16× | same, 2026-07-27 | encode one short query with each asset under `DEVICE=cpu` |
| `M:fp16-bulk` | long chunks: 257.3 ms/text (int8) vs 250.2 ms/text (fp16) — a wash | same, 2026-07-28 | `make bench-ep` (the two CPU rows) |
| `M:batch-composition` | one passage alone vs batched beside its corpus neighbours: cosine 0.992142, not byte-identical | same, 2026-07-28 | encode `texts[15]` alone and inside `texts[0:16]`, compare |
| `M:ep-agreement` | two providers, one asset: worst cosine 0.999991, and a top-10 flip on 1 of 4 queries | same, 2026-07-28 | `make ep-equiv` with both providers registered |
| `M:concurrency` | embedder 1.23×, reranker 2.17×, combined 1.18× (160 passages / 20 pairs, both on CPU/int8) | same, 2026-07-28 | `RERANK=1 uv run python tools/bench_concurrency.py --passages 160 --pairs 20` |
| `M:tokenise-share` | tokenisation is 0.02 % of an encode (7.2 ms vs 33 356 ms over 64 passages) | same, 2026-07-28 | time `_tokenize` against `_run` over four batches of 16 |
| `M:ocr-backends` | paddle vs onnxruntime: 0.9265 raw / 0.9291 whitespace-ignored; 40 lines shared, 5 paddle-only (one a 발의자 line), 1 onnx-only | same, 2026-07-28 | `make ocr-compare FILE=<a scanned 법률안> ` (2 pages) |
| `M:ocr-coreml` | PP-OCRv5 with CoreML enabled: 38.2 s/page and one exception per op, against 2.1 s/page on the CPU — 18× | same, 2026-07-28 | run `ocr_onnx` on one page with and without the CoreML flag |

## Retired

These were stated in the repo and are wrong. `tools/check_retired_numbers.py`
fails the build if one comes back.

| retired | replaced by |
|---|---|
| `~37 minutes` (cold rebuild) | `M:cold-rebuild` — the measurement was 1433.9 s (23.9 min) |
| `502 vs 466 ms/passage` (bulk fp16 vs int8) | `M:fp16-bulk` — 250.2 vs 257.3, and fp16 is the FASTER of the two |
