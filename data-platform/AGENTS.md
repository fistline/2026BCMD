# Local-first AI-agent data platform — agent guide

Cross-tool operating rules for this repository, in the [AGENTS.md](https://agents.md)
format. Setup, architecture, and rationale live in `README.md`. This file holds
only what an agent must not get wrong.

Meltano extracts → DuckLake stores → SQLMesh refines → one SQLite file serves
vectors, keyword search, and a dependency graph.

## Commands

First run needs `make setup` (`uv sync` + `meltano install`). Run it again after
changing dependencies: Meltano builds the tap its own virtualenv, so a new
package reaches the pipeline only once the plugins are reinstalled.

| Command | Purpose |
| --- | --- |
| `make build` | Full pipeline plus smoke test. **The default action.** |
| `make watch` | Rebuild whenever `data/inbox/documents/` changes |
| `make smoke` | Assert `hybrid_search` and `graph_query` still work |
| `make query Q="..."` | Hybrid search from the shell |
| `make ask Q="..."` | Hybrid search plus graph-related context (`graph_rag`) |
| `make impact NODE=x` | Blast radius for a node |
| `make eval` | Retrieval regression floor: P@1 / R@5 / MRR@10 per arm and per kind |
| `make eval-ask` | Related-section floor for `make ask` (graph arm membership) |
| `make gate` | Acceptance gate for document-type profiles |
| `make triage` | Which profile claims each document in `data/raw` |
| `make dupes` | Content-duplicate renditions the fingerprint dedup collapsed |
| `make dupes-twins` | Do `source/` twin renditions (`X.hwp` beside `X.pdf`) match? Reads no lake state |
| `make dupes-controls` | Fuzzy twin-detector controls; also runs blocking inside `make verify` |
| `make gpu-probe` | Which execution provider this machine selects, and whether `fp16` would change it |
| `make bench-ep` | Measured ms/passage per (provider, asset). Decide GPU-or-not with numbers |
| `make bench-concurrent` | Do two stages running at once help, or starve each other? Measures the interference |
| `make setup-gpu STACK=cuda\|directml\|qnn` | Install an accelerator wheel; records `.gpu-stack` so every target syncs it |
| `make ep-equiv` | Cross-hardware contract: same asset on two providers must agree. Skips on a CPU-only box |
| `make verify` | Full verification gate |
| `make clean` | Delete regenerable state only |

Single stage: `uv run python pipeline/run.py --only transform`.
Always run `make build` (not `make index`) after changing a model or the tap:
the stages are ordered for a reason.

## Invariants

Each rule names what breaks when it is violated. These are the expensive
mistakes; everything else is recoverable.

1. **Never commit anything under `data/`.** Git and the object store would both
   claim the same bytes and diverge on the next sync. Two tracked trees seed the
   git-ignored inbox on every build: `pipeline/fixtures/` (the small smoke-test
   sample set) and `source/` (the curated real corpus, one subfolder per
   collection). `seed_inbox` walks both and copies only
   SUPPORTED_SUFFIXES. Fixtures land flat (the `_root` collection); a `source/` file keeps its
   first subfolder as its inbox path, so `source/norms/x.txt → inbox/norms/x.txt`
   stays in collection `norms` on a fresh clone. Add corpus originals to `source/`
   (in the subfolder that names their collection) and fixtures to
   `pipeline/fixtures/`, never to `data/`. `source/CORPUS_MANIFEST.tsv` covers **every**
   file under `source/`: sha256 + bytes to check a copy, and where it came from to
   re-fetch it. Add the row with the file — provenance that lives only in the session
   that did the download is provenance nobody else can verify, and this manifest ran
   for months describing 48 of 70 files while claiming to describe all of them. Where
   `source` is not a URL it says so in words (the issuing system, or that this repo
   authored the summary); it is never left blank.

2. **Never edit `data/raw/` or `data/inbox/` in place.** They are append-only
   landing zones; the watcher preserves superseded bytes under
   `data/raw/_revisions/` and logs every promotion to `_manifest.jsonl`. Editing
   raw destroys the guarantee that `data/processed/` is rebuildable from code
   plus raw, and two machines stop agreeing.

3. **Keep serving in one SQLite file.** `data/serving/index.sqlite` holds
   vectors, FTS5, and the graph together. Splitting it loses the single-file
   snapshot, and lets the vector and keyword halves disagree about which
   documents exist.

4. **Never fork code for spoke vs hub.** They differ only through Meltano
   environments, SQLMesh gateways, and `.env`. Forked nodes drift and the repo
   stops running everywhere.

5. **Keep audits blocking.** A failing audit must stop the build, because the
   next step writes the index the agent trusts. Do not downgrade one to
   non-blocking to get a build through.

6. **Secrets stay in `.env`.** Git-ignored, never committed, never echoed into
   build output or logs.

7. **Never put a network call on the read path.** Retrieval, embedding, and
   traversal are local, which is what keeps the agent working offline. The
   default embedder is deterministic and needs no model download.

8. **Web fetching: API first, then scrape.** Prefer an official API for any
   source that offers one (`fetch_law.py`). For sources with no API, scrape their
   PUBLIC pages with `scrapling` (`fetch_web.py`) — escalating static → render →
   stealth browser as the page demands, including past indiscriminate bot-blocking
   of public content. Be a good citizen: rate-limit, fetch only what you need, and
   do not fetch authenticated/private data you are not entitled to. What breaks if
   ignored: fetched pages must land in `data/inbox/documents/` and go through the
   normal pipeline — never straight into `data/raw` or the index, or the retrieved
   material loses the provenance every other document carries.

9. **Reproducibility is scoped to a machine and its device profile — and the
   cross-hardware promise is a TOLERANCE, not equality.** A rebuild on the same
   machine with the same profile is byte-for-byte identical, and the pinned
   single-thread sessions are what buy that. Across machines it cannot hold: a
   GPU kernel accumulates in a different order than a CPU one, so the contract
   there is "same asset, different provider ⇒ cosine ≥ 0.9999 and identical
   top-10", asserted by `tools/check_ep_equivalence.py`. Two consequences worth
   knowing before touching `pipeline/runtime.py`: the ASSET (int8 vs fp16) is in
   `index_signature` because it is a different vector space, and the EXECUTION
   PROVIDER deliberately is not, because an index built on a GPU box must stay
   queryable on a CPU-only spoke. What breaks if ignored: put the provider in the
   signature and the first `make sync` hands every spoke an index it refuses to
   read. The ENCODE BATCH is in the signature for the same reason as the asset —
   the tokenizer pads to the batch's longest sequence, so batch composition moves
   the vectors (measured: cosine 0.9918, three orders of magnitude more than any
   provider difference). It is not a throughput knob and the GPU does not get a
   bigger one -- and the batch is not a knob for a deeper reason than padding:
   the int8 graph is DYNAMICALLY QUANTISED, so the scale is computed over the
   whole batch and a passage's vector depends on its neighbours' VALUES
   (measured: cosine 0.9904 between two same-size batches with different
   neighbours; a batch of one is byte-identical across every call). The
   consequence is that an incrementally-built index and a cold-built one hold
   DIFFERENT vectors for the same corpus, and index_signature cannot see it.
   Batch 1 removes that and was measured WORSE at retrieval (vector MRR@10
   0.476 -> 0.417), because a larger batch calibrates the quantisation scale
   better -- so the hazard is accepted where it is harmless (querying) and
   blocked where it is not: `make eval-baseline` REFUSES to record a floor from
   an incrementally-built index, and `make index-canonical` produces one that is
   safe to record from. `index_meta.build_kind` says which kind an index is.
   The VECTOR CACHE goes the other way and records the execution
   provider (`vector_cache.vector_signature`), because it is node-local under
   `data/processed/` and never synced: it commits every 256 passages, so without
   that slot one cache can hold vectors from two providers, and the 0.999991
   agreement between providers is far too close for `verify_sample`'s 0.97 floor
   to catch. Also: `detect()` reads no timing and no file — it must stay a pure
   function of (environment, enumeration) so it can be cached and tested without
   hardware. The Tier B split DOES read a recorded timing, which is why
   `ENCODE_SPLIT` exists as the deterministic override.

10. **Nothing on the read path or the build path may download, probe or compile.**
    Invariant 7 covers the download; the other two are the same rule. A probe
    builds a real session, and building a session is the operation measured to
    crash the process — so `make gpu-probe` runs each one in a supervised child
    (`runtime.probe_provider`), and a probe's result is ADVISORY: the partition
    count is printed, never gated on, because only CoreML and QNN report it at all.
    What breaks if ignored: `pipeline.runtime --save` is a step of `make verify`,
    so an in-parent probe ends the whole gate with a bare signal number.

11. **To put the embedder on an accelerator while the control loop stays on the
    CPU, use `DEVICE_PLACEMENT="embedder=gpu,reranker=cpu"`.** That is the whole
    mechanism — one environment variable, no controller code. It is not a tier
    system on purpose: (a) the embedder's asset is fleet-wide because it is in
    `index_signature`, while the reranker's is machine-local, so one policy cannot
    own both; (b) two stages on one device were measured to cost the light stage
    2.17x; (c) two models resident on one accelerator doubles the memory where
    overshoot is a SIGKILL, not an exception; (d) they then share a blast radius.
    Moving the reranker is the operator's decision and the operator owns the
    `make eval-rerank-baseline` re-record, because a provider change was measured
    to flip a top-10 at cosine 0.999991.

## Conventions

- **Node ids are slugs; underscores and Hangul survive.** `search_core.py` →
  `search_core`, `가상자산이용자보호법.md` → `가상자산이용자보호법`. A binary format
  keeps its suffix (`bill.hwp` → `bill-hwp`) so a `.hwp` and its `.hwpx` twin stay
  distinct. `document_id` and `normalise_entity` must agree for every supported
  suffix, or graph edges dangle at ids no node carries.
- **Synonymy is not a tokenizer problem.** Character n-grams bridge morphology,
  never synonymy: 스테이블코인 and 자산연동형 share no characters. That gap is
  closed by `pipeline/aliases.tsv`, a query-time table where every row cites the
  article asserting the equivalence. Add a row only with a measurement, and
  record what you rejected in `excluded.tsv`.
- **The Korean write path and query path must stay in step.** `expand_cjk` writes
  character bigrams into FTS5 and `build_fts_query` must produce the same ones.
  If they diverge, every Korean keyword query silently returns nothing;
  `index_signature` exists to turn that into a loud error.
- **Push logic to the lowest layer that can express it.** Bronze preserves shape
  and only casts types; silver cleans and de-duplicates; gold is the index input
  and nothing else reads it.
- **Supported formats live in ONE table**, `SUPPORTED_SUFFIXES` in
  `pipeline/chunking.py`. Text (`.md .markdown .txt .rst`), code (`.py`), and
  binaries decoded by `pipeline/extract.py`: `.hwp` (hwpkit), `.hwpx` `.docx`
  `.xlsx` `.pptx` (standard library only -- a sweep over every XML text node, not
  a schema-aware parse), `.pdf` (pypdf, text layer only), and `.doc` `.xls` `.ppt`
  behind `uv sync --extra legacy` (office-oxide; the one format group with no
  maintained pure-python reader). A file whose suffix is absent is skipped
  SILENTLY by the tap, so adding a format means adding it here, not anywhere else.
  **Images (`.png`/`.jpg`) are deliberately NOT supported** and must not be added:
  an image has no text to extract deterministically, only a model's reading of it.
  That belongs to `tools/ocr/ocr_prepare.py`, which OCRs images or scanned PDFs
  offline, a human reviews the draft, and the reviewed `.txt` enters the inbox --
  the build stays model-free.
- **The retrieval surface holds one row per distinct chunk content.** A standard
  약관 clause (예: 제1조(목적)) is copied verbatim into dozens of filings; indexing
  it once per copy lets N identical hits fill every top-K and bury the
  distinctive article. `build_rag` collapses identical `content` into a single
  representative for `chunks_vec` and `chunks_fts` (same set in both, so the
  halves still agree), while `chunks` keeps every row for provenance and the
  gold<->index parity. So `COUNT(chunks_vec) == COUNT(chunks_fts) ==
  COUNT(DISTINCT content) <= COUNT(chunks)` — not the old three-way equality.
- **Everything is idempotent.** Re-running any stage is safe. A stage that is
  not idempotent is a bug, not a trade-off.
- **`hybrid_search` for content, `graph_query` for reachability.** Impact
  questions cannot be answered by search, because the modules a change breaks
  are exactly the ones that never mention it.

## Measuring retrieval

Change retrieval and run `make eval`. Read the PER-KIND table, never only the
mean: reverting the arm weighting leaves the overall MRR bit-identical at 0.699
while `particle_glue` drops 0.833 to 0.611. A mean-only reading would call that
no change.

Judgments in `pipeline/eval_queries.json` are anchored by a HEADING SUBSTRING,
never by chunk_id. Chunk ids are positional, so a sectioning change re-points every
one of them while they all still resolve — an earlier judgment set survived exactly
that with `#0110` silently moving from a capital-adequacy article to 제97조(준수사항).
The anchor is a LITERAL: `_matching_chunk_ids` re.escape()s it, so a `제\d+조` or a
`제7조\(신고\)` written as a pattern resolves to NOTHING, the query drops out of
`graded`, and only the "unresolved judgments" line says so.

`make verify` fails when an ARM metric drops more than 0.02, OR a per-KIND metric
more than 0.08, below `pipeline/eval_baseline.json` — so a category collapse the
mean hides (the `particle_glue` case above) fails the blocking build, not just an
arm-mean drop. Re-record it with `make eval-baseline` only alongside a measurement
that justifies the move; the baseline also carries the git SHA it was recorded at.

## Grading skill outputs

`make eval` measures retrieval. `tools/skill-eval/grade.py` measures the other
thing this repo produces — **documents a skill wrote** — against the assertions in
that skill's `evals/evals.json`. It lives here because measurement tooling does,
not because it touches the corpus: it reads nothing from `data/` and imports no
`pipeline` module.

```
python3 tools/skill-eval/grade.py ../gen-docs/st_prospectus/sto-filing-workspace/iteration-1 --rules sto-filing
```

The harness is skill-agnostic; every domain regex sits in `rules_<skill>.py` beside
it, so a skill's rules stay reviewable next to its assertions rather than smeared
through the runner. Add a skill by writing one rules module and one `RULES` entry.

Two things it will not do, both learned by getting them wrong. It **never invents a
verdict** — an assertion no rule matches comes out `pending`, and a human fills it in
`<iteration>/manual_grades.json`, which is applied only where the machine abstained and
only when the recorded `_digest` still matches the outputs that were read (re-running an
eval otherwise re-attaches yesterday's verdicts to files nobody looked at).
And banned-phrase checks read the surrounding 60 characters for a negation marker,
because a filing is *required* to say "원금·수익 보장과 무관", so bare keyword
matching flags the compliant document.

Like `tools/hitl/`, this is **off the build path** — `make verify` does not call it,
because a skill eval needs an LLM run first (~10 minutes and ~100k tokens per case),
which no blocking build can afford. Invocation is the only switch; there is nothing
to enable. The one thing worth configuring is what a `pending` verdict does to the
score, and the default hides it: `pass_rate` is `passed / (passed + failed)`, so
un-graded assertions drop out of the denominator and 13 of 29 blanks once read as
"100%". `--require-complete` exits non-zero on any pending and prints the keys to
fill — use it when declaring a grading pass finished, not on every run.

Output is `grading.json` per run in the skill-creator viewer's schema
(`expectations[].text/passed/evidence`); its aggregator wants sibling `run-*`
directories, which this layout does not have — symlink rather than restructure.

## Document-type profiles

A profile in `pipeline/doctypes/` is **data**, not code: four tables of regexes.
It cannot do I/O, read the clock, or assign a `chunk_id` — the engine owns all of
that, so an authored profile inherits idempotence for free. A model writes one
once, offline; `make build` never calls a model.

Author against `make gate`, never against your own reading of the regex. The
naive version of this pattern was measured to LOSE to per-document model
extraction (EVAPORATE, VLDB 2024, -13.8 F1); the acceptance gate is what turns
it into a win. See `.agents/skills/doctype-profile-authoring/`.

## Skills

Task procedures and tool entry points live as Agent Skills and load on demand
rather than sitting in this file, so the same ones work in Codex, Claude Code and
Antigravity — every one reads the open SKILL.md standard. `.agents/skills/` is the
source of truth and the only tracked copy; `.claude/skills/` is a vendor adapter
built by `make sync-skills` (or `make skills` from the repo root) and git-ignored,
so a Windows clone cannot turn its links into text files that silently fail to
load. Edit only the canon; never hand-build the adapter.

Tool entry points (reach for these first on any question):

- `.agents/skills/corpus-search/SKILL.md` — find what the documents say
  (`hybrid_search`: vector + FTS5, fused; `graph_rag`: the same search plus a
  `related` section from graph-connected documents, never re-ranked)
- `.agents/skills/corpus-graph/SKILL.md` — trace how documents relate
  (`graph_query`: 위임·참조 reachability across bills)

Task procedures (these call the tools above as steps):

- `.agents/skills/document-drafting/SKILL.md` — draft grounded in the corpus
- `.agents/skills/code-impact-analysis/SKILL.md` — blast radius of a code change
- `.agents/skills/doctype-profile-authoring/SKILL.md` — teach the build a new
  document type by compiling a committed regex profile
- `.agents/skills/legal-schema-authoring/SKILL.md` — extend the graph's
  relation-weight + node-kind ontology for legal documents (위임/개정/준용) as a
  committed, `make eval-graph`-measured diff; the ontology counterpart to the
  extraction profile above
- `.agents/skills/source-onboarding/SKILL.md` — bring a new external source in:
  research, per-path robots verdict, API-vs-scrape-vs-manual, sample, decision gate
- `.agents/skills/hitl-review/SKILL.md` — review flagged items in a browser and
  approve into the inbox (the reusable HITL server); use and how to wire a producer
- `.agents/skills/correction-harvesting/SKILL.md` — turn a retrieval miss or a
  corrected answer into a heading-anchored eval, then a measured `aliases.tsv` row
  or an `excluded.tsv` rejection (offline; default outcome is an eval + a rejection)
- `.agents/skills/graph-viz/SKILL.md` — see the corpus graph in the browser:
  `make graph-serve` (live viewer, DB picker, auto-refresh) or `make graph`
  (standalone offline HTML). Renderer in `tools/viz/`, stdlib-only like `tools/hitl/`

The `allowed-tools:` frontmatter in the tool-entry skills pre-approves their one
command in Claude Code; Codex and Antigravity ignore that key and read the body,
which names the same command in prose — so no per-tool wiring is needed.
