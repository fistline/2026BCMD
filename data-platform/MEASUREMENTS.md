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

**The `corpus` column says which documents the number was taken over**, because
two numbers are only comparable if they are. That is not a hypothetical: the
1433.9 s in `M:cold-rebuild` was compared against a fresh 1948.2 s here, and the
comparison was sound only because whoever ran it happened to know the two shared a
document set. Get the current one with `make corpus-id`; it is a hash over the
identity columns of `source/CORPUS_MANIFEST.tsv`, refused unless the manifest
matches the bytes on disk.

A row whose value came from an EVAL carries a second id, `j:<sha>`, for the
judgment set — the same queries over different documents and the same documents
under different judgments are different measurements, and BEIR treats a retrieval
number without all three named as unusable. `judgment_sha` in the eval baselines is
the same fingerprint.

Two honest limits. Rows marked `unknown` predate the column and their corpus cannot
be recovered — a guess would be a fabricated provenance, so they say so instead.
And rows dated 2026-07-28 carry the id computed on the 29th: the manifest grew from
48 declared documents to 70 that day (3d31ee3) without any document changing, so an
id minted on the 28th would have differed while the corpus did not. The column
answers "which documents", not "what the manifest said at the time".

| id | value | corpus | machine / date | reproduce |
|---|---|---|---|---|
| `M:cold-rebuild` | 1433.9 s cold → 51.5 s cached; 12,643 vectors, byte-identical | unknown | M4 Pro 12-core / 24 GB, macOS 15, onnxruntime 1.27.0, 2026-07-27 | `rm data/processed/vector_cache.sqlite && time make index` then `time make index` |
| `M:coreml-int8` | 148 partitions; 869 ms/chunk vs 517 ms/chunk on one CPU thread; 13.5 GB peak RSS; SIGKILL at 32 chunks | unknown | same, 2026-07-27 | `ORT_PROVIDER=CoreMLExecutionProvider make bench-ep` |
| `M:coreml-fp16` | 149 partitions; SIGSEGV on one run, 14275 ms/text (57× CPU) on another | c:9cb3690c0c9e | same, 2026-07-28 | `make bench-ep` (the CoreML rows) |
| `M:fp16-query` | short query: 12.7 ms (int8) vs 203.4 ms (fp16) on the CPU — 16× | unknown | same, 2026-07-27 | encode one short query with each asset under `DEVICE=cpu` |
| `M:fp16-bulk` | long chunks: 257.3 ms/text (int8) vs 250.2 ms/text (fp16) — a wash | c:9cb3690c0c9e | same, 2026-07-28 | `make bench-ep` (the two CPU rows) |
| `M:batch-composition` | one passage alone vs batched beside its corpus neighbours: cosine 0.992142, not byte-identical | c:9cb3690c0c9e | same, 2026-07-28 | encode `texts[15]` alone and inside `texts[0:16]`, compare |
| `M:ep-agreement` | two providers, one asset: worst cosine 0.999991, and a top-10 flip on 1 of 4 queries | c:9cb3690c0c9e | same, 2026-07-28 | `make ep-equiv` with both providers registered |
| `M:concurrency` | embedder 1.23×, reranker 2.17×, combined 1.18× (160 passages / 20 pairs, both on CPU/int8) | c:9cb3690c0c9e | same, 2026-07-28 | `RERANK=1 uv run python tools/bench_concurrency.py --passages 160 --pairs 20` |
| `M:tokenise-share` | tokenisation is 0.02 % of an encode (7.2 ms vs 33 356 ms over 64 passages) | c:9cb3690c0c9e | same, 2026-07-28 | time `_tokenize` against `_run` over four batches of 16 |
| `M:query-startup` | one-shot `make query` 1.91–2.34 s, `make ask` 1.87–2.79 s; the FIRST query in a process 1827 ms, every warm one after it 84 ms — a 22× cliff paid once per process | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | `time make query Q="..."`, then time two queries inside one python process |
| `M:batch-amortisation` | one process via `--queries-from`: 3 queries 2.17 s against 5.52 s as three one-shots (2.5×); at N=10 the design measured ~7× | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | `time uv run python -m agent.tools.hybrid_search --queries-from FILE` vs a shell loop |
| `M:rerank-batch` | the reranker is dynamically quantised too: the same pair scores −8.4216 alone, −7.8296 in a batch of 16 (Δ0.59), and 0.065 apart in two different 16-batches; the differences survive the 4-decimal rounding, and a 16+4 split vs one batch of 20 gives a different top-5. At 16 candidates in one batch the recorded floor reproduces exactly, per-kind | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | score one pair alone, inside a 16-batch, and inside another 16-batch; then `RERANK=1 RERANK_CANDIDATES=16 make eval` |
| `M:ocr-backends` | paddle vs onnxruntime: 0.9265 raw / 0.9291 whitespace-ignored; 40 lines shared, 5 paddle-only (one a 발의자 line), 1 onnx-only | c:9cb3690c0c9e | same, 2026-07-28 | `make ocr-compare FILE=<a scanned 법률안> ` (2 pages) |
| `M:quantisation-batch` | the int8 graph is dynamically quantised: same passage, two same-size batches with different neighbours → cosine 0.9904; a batch of two copies of the same text is byte-identical to a batch of one; a batch of one is byte-identical across every call | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | encode one passage alone, in a batch of 16, and in a batch of 16 with different peers |
| `M:batch1-quality` | batch 1 (history-independent) vs batch 16: build 757.2 s vs 1433.9 s, but vector MRR@10 0.476 → 0.417, R@10 0.923 → 0.769, fused R@5 0.846 → 0.769 |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-28 | pin `_batch = 1`, `make index-canonical`, `make eval` |
| `M:padding-modes` | 64 real chunks, single-threaded: batch16 dynamic pad 181.1 ms/passage, batch1 self pad 238.0, batch16 fixed-512 pad 162.2 | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | time `encode` under each tokenizer padding configuration |
| `M:ocr-coreml` | PP-OCRv5 with CoreML enabled: 38.2 s/page and one exception per op, against 2.1 s/page on the CPU — 18× | c:9cb3690c0c9e | same, 2026-07-28 | run `ocr_onnx` on one page with and without the CoreML flag |
| `M:token-density` | Korean legal text measures **2.054 chars/token** under the bge-m3 sentencepiece, against the 4 that `chunking.py` assumes — so a 1200-char chunk is ~584 tokens, not ~300. At a 512 cap, 28.1 % of chunks (3 670 / 13 047) truncate: markdown 64.7 %, txt 19.6 %, hwp 15.2 %. `CHUNK_OVERLAP_CHARS` covers only 18.4 % of the truncated tails, leaving 348 473 chars — 4.22 % of all chunk text — in NO vector. One truncated chunk against its full-text vector: cosine 0.961703. Sizing the CHUNKER instead: 650 chars is the largest ceiling at which no chunk exceeds 512 tokens (observed max 488); 800 leaves 1.03 % over and 900 leaves 12.68 % | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | tokenise `embed_text` for every row of `chunks` with the bge-m3 tokenizer; compare token counts against 512 and the truncation offset against the next chunk's `char_start` |
| `M:section-flooding` | One long article windows into several chunks that all match the same query, so a single 조문 fills the answer with its own fragments: 29 of 140 top-10 slots across the 14 eval queries (20.7 %), 6 of 10 on the worst. Capping a (doc_id, heading) to ONE chunk is what recovers it — and only a cap of 1 does anything, since caps of 2 and 3 reproduce the uncapped numbers exactly (the flooding is mostly 2 deep). Fused at chunk size 650: uncapped P@1 0.583 / MRR@10 0.744, capped 0.667 / 0.801, against a 1200-chunk baseline of 0.667 / 0.764 |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | count repeat `(doc_id, heading)` pairs in `hybrid_search(q, limit=10)` over `eval_queries.json`; then `SECTION_CAP=1\|2\|3 make eval` |
| `M:chunk-650` | Sizing the chunker to the embedder's cap (`MAX_CHUNK_CHARS` 1200 → 650) eliminates the truncation entirely — 28.08 % → 0.000 %, observed max 488 tokens against the 512 cap — at 20 344 chunks against 13 047. It is MORE expensive to build, not less: 1948.2 s for 19 808 vectors against 1433.9 s for 12 643 [M:cold-rebuild] — 1.36× the wall clock, because the 0.87× per-vector saving does not cover 1.57× the vectors. A `C^2/(C - overlap)` estimate said 0.62× and was wrong in both of its terms: most chunks were already below 650, so neither the count nor the sequence length moved the way a full-window model predicts. With `SECTION_CAP=1` the vector arm gains P@1 0.250 → 0.583 and MRR@10 0.474 → 0.648, and fused gains MRR@10 0.764 → 0.801 and R@10 0.917 → 1.000 with P@1 and R@5 unchanged. Per kind it is NOT uniform: `vocabulary_match` MRR 0.708 → 0.875 and `synonym_gap` 0.444 → 0.537, while `cross_bill` drops 1.000 → 0.833 — one query moving rank 1 → 2, its new rank 1 being 전자금융감독규정 제51조, topically apt but absent from the heading anchors |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | set `MAX_CHUNK_CHARS = 650`, `make build`, `make eval` |
| `M:arm-weighting` | `VECTOR_WEIGHT` swept, fused MRR@10: 0.3 → 0.750, 0.5 → 0.715, 0.7 → 0.674, **1.0 → 0.801**, 1.5 → 0.792, 2.0 → 0.792, with R@10 0.917 below 1.0 and 1.000 at or above it. NOT a smooth curve — it dips to a minimum at 0.7 before the peak, which on 12 graded queries is one or two queries flipping, so read 1.0 as the top of a plateau (1.0/1.5/2.0 all ≈ 0.79–0.80) rather than as a sharp optimum. The 0.3 the code shipped came from the hashing-embedder era and a 45-query judgment set that no longer exists |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | `VECTOR_WEIGHT=<w> uv run python -m pipeline.eval_retrieval` for each w |
| `M:rrf-k` | `RRF_K` is not a sensitive knob on this corpus: fused MRR@10 0.788 at 10, then 0.801 at 30, 60 and 120 — flat from 30 upward, with P@1, R@5 and R@10 identical throughout. The shipped 60 is the constant from the original RRF paper and is now measured rather than inherited |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | `RRF_K=<k> uv run python -m pipeline.eval_retrieval` for each k |
| `M:alias-expansion` | Query-side alias expansion is not a small lift on this corpus: fused P@1 0.667 → 0.417, R@5 0.917 → 0.750, R@10 1.000 → 0.917 and MRR@10 **0.801 → 0.564** when it is turned off. The figure this replaces (`0.725 → 0.802`) lived only in a .env.example comment and was taken on a corpus and judgment set that no longer exist, which is why the two disagree on the OFF number by 0.16 and cannot be compared |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | `ALIAS_EXPANSION=1\|0 uv run python -m pipeline.eval_retrieval` |
| `M:cap-chunk-coupling` | `SECTION_CAP` is NOT an independent improvement — it is a corrective for the fragmentation small chunks create, and at the old chunk size it HURTS. Four builds, same judgments, fused MRR@10: 1200 uncapped 0.764, **1200 capped 0.701**, 650 uncapped 0.744, **650 capped 0.801**. At 1200 the loss is concentrated: `vocabulary_match` 0.708 → 0.521. So the two changes are interdependent and 650 earns its 1.36× build cost on the metric, not only on the truncation it removes. Also measured in the same run: the `MIN_ADVANCE_CHARS` guard is not a no-op at 1200 — it moves the corpus 13 047 → 13 020 chunks and lifts the vector arm MRR@10 0.4745 → 0.513 while leaving fused identical |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | snapshot `data/`, set `MAX_CHUNK_CHARS = 1200`, `make build`, `SECTION_CAP=1\|99 make eval`, restore |
| `M:rerank-weight` | `RERANK_WEIGHT` swept against the reranked arm, fused = P@1 0.667 / MRR@10 0.801 for reference: 0.15 → 0.667/0.758, 0.3 → 0.667/0.761, 0.5 → 0.667/0.762, 1.0 → 0.667/0.802, 2.0 → 0.833/0.885, **3.0 → 0.917/0.926**, 4.0 → 0.917/0.925, 8.0 → 0.667/0.801. A peak with a flat top at 3–4 that converges to the fused number by 8, which is what `w/(k + retr_rank) + 1/(k + ce_rank)` must do as w grows — so the cross-encoder is worth its cost as a tie-breaker and not as a primary ranker. The shipped 0.15 put the reranked arm BELOW plain fusion once SECTION_CAP had lifted fusion |  c:9cb3690c0c9e j:41337043a6c9  | M4 Pro, 2026-07-29 | `RERANK=1 RERANK_WEIGHT=<w> uv run python -m pipeline.eval_retrieval` for each w |
| `M:table-noise` | 1 697 of 20 344 chunks (8.3 %) are at least 20 % box-drawing characters. At 30 % it is 892 chunks (4.4 %), at 40 % 277 (1.4 %). **They are not noise in the sense of being contentless, and an earlier version of this row said they were.** Reading the bands: 40 %+ holds 위험액 산정 tables (`채권옵션 │ 계약의 명목가치 × 기초자산의 시가 × 델타`), 30–35 % holds the 인가 절차 flowchart, 20–25 % holds the 등록 요건 table. The Hangul share falls with the box share (0.189 / 0.125 / 0.055 median) but never to zero — the drawing characters are FORMATTING AROUND real regulatory content, so dropping these chunks would delete answers. What is real is the dilution: at 40 % box characters, two fifths of a chunk's token budget is spent on lines. Concentrated but not local: 금융투자업규정시행세칙 1 145, 금융투자업규정 143, 신용정보업감독규정 136, and three more documents. One consequence is visible in the sectioning: `제8조(자료의 보관)` in 금융투자업규정시행세칙 spans chars 306 350 to 1 002 591 — 696 k characters, 1 498 chunks — because it is the LAST 조 heading and the appendix after it has none, so the windowing attributes all of it to that article | c:9cb3690c0c9e | M4 Pro, 2026-07-29 | count the box-drawing share of every `chunks.content`; group the heavy ones by `doc_id` |
| `M:workers-equality` | The threaded encode path (`ThreadPoolExecutor.map` over batches) is byte-identical to the sequential one: same sha256 over 64 real passages at `ENCODE_WORKERS=1` and `=8`. This matters because `_encode_workers` returns `min(cores - 2, 8, n_batches)`, so CORE COUNT picks the branch and a 4-core Windows box takes a different one from a 12-core Mac. Measured on ONE machine only; the cross-platform half is unrun | c:9cb3690c0c9e | M4 Pro 12-core, 2026-07-29 | `uv run python tools/check_workers_equality.py` |
| `M:cap-768-cost` | Raising the embedder cap 512 → 768 measured **11.4× slower**, not the 1.24× a padded-token-cell count predicts: 2 560 vectors in 54.9 min (1.287 s/vector) against 0.113 s/vector at 512 [M:cold-rebuild], still decelerating at the kill (no checkpoint in the final 14 min). Attention is quadratic in sequence length, and 8 concurrent workers at 768 push a 24 GB box into memory pressure. Projected full build 4.7 h | c:9cb3690c0c9e | M4 Pro, 2026-07-28 | set `_MAX_TOKENS = 768`, `make index-canonical`, read the vector-cache row count against wall clock |

## Retired

These were stated in the repo and are wrong. `tools/check_retired_numbers.py`
fails the build if one comes back.

| retired | replaced by |
|---|---|
| `~37 minutes` (cold rebuild) | `M:cold-rebuild` — the measurement was 1433.9 s (23.9 min) |
| `502 vs 466 ms/passage` (bulk fp16 vs int8) | `M:fp16-bulk` — 250.2 vs 257.3, and fp16 is the FASTER of the two |
