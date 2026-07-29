# Local-first AI-agent data platform — spoke node

Drop a file into `data/inbox/documents/`. Get hybrid RAG and a dependency graph
in one SQLite file, with no network call anywhere on the read path.

```
inbox ──promote──▶ data/raw ──Meltano──▶ DuckLake ──SQLMesh──▶ gold ──▶ index.sqlite
        immutable              lake.raw.*   bronze/silver/gold    vectors + FTS5 + graph
```

Stack: Python + uv · Meltano (EL) · DuckDB/DuckLake (lakehouse) · SQLMesh
(transform + audits) · SQLite with sqlite-vec and FTS5 (serving).

Configured as: `NODE_ROLE=spoke`, `FIRST_SOURCE=inbox_documents`,
`DATA_REMOTE=local_only`.

## Quick start

```bash
uv sync
uv run meltano install
make build            # seeds fixtures, runs the pipeline, then the smoke test
make query Q="how does promotion work"
make impact NODE=vector_store
```

`make build` is idempotent. Run it as often as you like; it never duplicates
rows and never mutates the raw zone.

To rebuild automatically whenever a file lands:

```bash
make watch
```

## Control plane vs data plane

This is the rule the whole design hangs on.

| | Control plane | Data plane |
| --- | --- | --- |
| Contains | Code, models, config | Documents, Parquet, embeddings, the index |
| Lives in | Git | `data/`, synced to S3/DVC or nowhere |
| Tracked by Git | Yes | **Never** |

`data/` is entirely git-ignored. If Git and the object store both claimed those
bytes, a two-machine sync would hit binary merge conflicts and the planes would
silently diverge. So the repo carries logic and the data plane carries bytes,
and neither is authoritative for the other.

Inside the data plane:

- **`data/inbox/documents/`** — drop zone. Anything you put here gets picked up.
- **`data/raw/`** — immutable. Files are copied in and never edited. A changed
  file does not overwrite its predecessor: the old bytes move to
  `data/raw/_revisions/<sha12>/`, and every promotion appends a line to
  `data/raw/_manifest.jsonl`. Because raw never changes underneath you,
  everything downstream can be deleted and rebuilt from code plus raw, on any
  machine, with the same result.
- **`data/processed/`** — the DuckLake catalog and its Parquet files. Fully
  regenerable; `make clean` deletes it.
- **`data/serving/index.sqlite`** — one file holding vectors, keyword index, and
  graph. One file means one `VACUUM`, one snapshot, one upload, and no way for
  the vector store and the keyword store to disagree about which documents
  exist.

### Fixtures and the git-ignore rule

`data/` is git-ignored, so a fresh clone has no input to build. The sample corpus
is therefore tracked at **`pipeline/fixtures/`** and copied into
`data/inbox/documents/` by `make build` (or `make ingest`). Existing files are
never overwritten. This keeps both rules intact: nothing under `data/` is
committed, and `make build` always has something to chew on.

## Spoke and hub

One repo, one code path, two configurations. Nothing branches.

```bash
make build                                   # spoke (default)
uv run meltano --environment=hub run el-inbox
PLATFORM_NODE_ROLE=hub make build            # hub
```

The difference is three config surfaces: the Meltano environment
(`environments:` in `meltano.yml`), the SQLMesh gateway (`gateways:` in
`transform/config.yaml`), and `.env`. If the two nodes ever need different
*code*, the design has failed.

## How retrieval works

`hybrid_search` runs two rankings and fuses them with reciprocal rank fusion:

- **sqlite-vec** ranks by embedding distance. Good at paraphrase, bad at exact
  tokens.
- **FTS5** ranks by bm25. Good at exact tokens, blind to synonyms.

Fusion is rank-based rather than score-based because the two scores are not
comparable: bm25 is an unbounded negative, cosine distance is bounded in [0, 2].
RRF sums `1/(k + rank)` across the rankings a chunk appears in, so neither scale
has to be normalised.

This matters concretely. Searching `hybrid_search` with vectors alone returns
prose *about* retrieval; FTS5 pins the function definition itself. The smoke test
asserts exactly that, so the property cannot silently regress.

Both halves tokenize identically (`unicode61` with `_` as a token character), so
`hybrid_search` is one token on both sides. If they disagreed, the same query
would address two different vocabularies.

### The graph arm: augment, don't re-rank

`graph_rag_search` (`make ask`) layers the dependency graph on top of that
search without touching it. Its `results` section is `hybrid_search`,
bit-identical; a second `related` section carries passages from documents the
graph connects to the top hits. The two are deliberately **not** fused into one
list: the graph arm is a doc-level filter over the same retrieval, not
independent evidence, so RRF-summing the two would double-count what the direct
arm already found — at `rrf_k=60` a graph-only chunk can never displace a
direct top-8 at any weight below ~0.9, so fusion's only observable effect would
be promoting the direct ranking's tail over its head. Keeping the sections
separate keeps the precision `make eval` protects, and makes every graph
contribution inspectable.

Related documents are found two ways. A **direct** edge joins two indexed
documents (Python imports, front-matter `depends_on`, wiki links). A
**co-citation** joins documents that assert same-orientation edges onto the
same non-document node — which is the only bill-to-bill connectivity that
exists, because bills never edge to each other: they edge to shared statute
nodes (`amends`, `delegates_to`), and the down-then-up walk is done as one
join. Hub pivots are damped (`1/log2(2+degree)`) and skipped entirely past
`HUB_DOC_CAP`, or the statute half the corpus delegates to would relate
everything to everything. `nodes.doc_id` is never read for any of this: for a
referenced node it is `MIN()` over asserting documents — an attribution
artifact, not provenance — so every related row instead carries the actual
edges (`connection.links`, with relations and verbatim citing evidence).

Passages inside a related document are chosen in three labelled tiers:
`query_match` (the user's own wording, alias-expanded, found it — token
evidence required, so KNN filler cannot wear the label; only the user's
variants score this tier, or the statute's name would drown the article that
answers the question), `lexical_overlap` (the user's wording found nothing, so
the *connective* vocabulary is tried as fallback variants: the shared
statute's name and the verbatim citing evidence, which is what reaches an
article that shares no characters with the question), and `doc_head` (nothing matched;
the head chunk is shown only because the graph insists the document is
related). Only representative chunks — rows the vector and keyword halves
actually index — are ever emitted.

### Korean

Korean is a first-class corpus here, and it needs more than "the same code with
Korean text in it". Two things are done differently:

**FTS5 indexes character bigrams, not words.** `unicode61` treats a whole Hangul
run as one token, so `가상자산` cannot match inside `가상자산이용자보호법` — the
query returns zero rows. Rather than switching to the `trigram` tokenizer, the
CJK runs are expanded to overlapping 2-grams on both the write and the query side
(`expand_cjk` / `build_fts_query`). Trigram was measured and rejected: it grows
the index by 114% instead of 17%, and it **cannot match two-character terms at
all** — which is most of Korean legal vocabulary (증권, 부칙, 신고).

**The embedder tokenizes Unicode and hashes CJK n-grams.** The previous
`[A-Za-z_]`-only pattern produced *zero* tokens for Korean, so every Korean chunk
embedded to an all-zero vector. That was worse than useless: sqlite-vec ranks by
L2 distance, so an all-zero query matched all-zero chunks at `distance = 0.0` and
injected that noise into RRF at the same weight as the top keyword hit. Fixing
FTS5 alone would have made retrieval *worse*, which is why both land together.

`EMBEDDING_DIM` defaults to **1024** and the hashing provider refuses to start
below 512: Korean character n-grams have a far larger feature vocabulary than
English words, and at 256 the hash collisions destroy the signal.

### What the default embedder can and cannot do

It matches on shared characters, so it is strong exactly where legal queries
usually are — when your question reuses the document's own vocabulary:

```
Q: 예치금 분리보관 의무
   → 제4조(예치금의 보호), 제84조(예치금의 보호), 제20조(예치금 보호)   ← three bills, correct
```

It cannot bridge synonyms that share no characters:

```
Q: 스테이블코인 발행자의 자기자본 요건
   → misses 제103조(자산연동형 디지털자산 발행인의 자격요건)
     because 스테이블코인 ↔ 자산연동형 and 발행자 ↔ 발행인 have no characters in common
```

If your questions are phrased in your own words rather than the statute's, that
gap is the reason to switch providers. Be aware what it costs, and that the
cheap options were measured and are **not** better:

| Provider | Korean quality | Cost |
| --- | --- | --- |
| `hashing` (default) | good on shared vocabulary, blind to synonymy | 0 MB, no download |
| fastembed + `paraphrase-multilingual-MiniLM-L12-v2` | **worse than the default** on this corpus | 138 MB + 240 MB |
| `sentence_transformers` + `BAAI/bge-m3` | genuinely better at paraphrase | ~2.2 GB + torch |

```bash
uv add sentence-transformers
EMBEDDING_PROVIDER=sentence_transformers EMBEDDING_MODEL=BAAI/bge-m3 make build
```

Both providers are fully implemented in `pipeline/build_rag.py`. Changing either
the provider or the dimension changes `index_signature`, and the next query
fails loudly until you rebuild rather than silently mixing two vector spaces.

### Does a GPU help?

Sometimes, and the deciding factor is the ASSET, not the card. `int8` — the
default — is a CPU format: on a GPU execution provider it needs Tensor-Core int8
plus a TensorRT calibration to beat the CPU, and without them it loses. Measured
here on Apple CoreML with the int8 `bge-m3` graph: 148 CoreML partitions out of
1775 nodes, **869 ms/chunk against 517 ms/chunk on a single CPU thread**. So with
`int8` the pipeline stays on the CPU even where a GPU exists, deliberately.

Setting `EMBEDDING_PRECISION=fp16` switches to the GPU-shaped asset, and
`pipeline/runtime.py` then uses whatever this platform registers — CUDA on
Linux/Windows, DirectML on any DX12 Windows GPU — falling back to the CPU rather
than failing if the provider does not come up. Two measured caveats:

- **fp16 on a CPU is a 16x tax on queries** (12.7 ms → 203.4 ms to encode one
  short query; bulk encoding of long chunks is a wash). So the asset is a
  fleet-wide decision: a CPU-only spoke inheriting a GPU hub's `fp16` still
  works, just far slower on every query. The build warns when it sees this.
- **CoreML is not selected automatically on macOS.** On this repo's models it
  produced 148–149 graph partitions and then died — `SIGKILL` at 32 chunks with
  int8, `SIGSEGV` with fp16 — and a native crash cannot be caught and fallen back
  from. Force it with `ORT_PROVIDER=CoreMLExecutionProvider` if your model and
  hardware differ.

Ask your own machine instead of trusting the numbers above:

```bash
make setup-gpu STACK=cuda   # or directml / qnn. macOS needs none: CoreML is in the default wheel
make gpu-probe              # what gets selected here, and what fp16 would change
make bench-ep               # ms/passage per (provider, asset), with the winner named
```

Every measured figure in this section lives in `MEASUREMENTS.md` with the command
that reproduces it; cite the id rather than restating a number.

Two things to know before switching: the precision is part of `index_signature`
(it is a different vector space, so it is a **fleet-wide** decision, not a
per-machine one), and the execution provider deliberately is **not** — an index
built on a GPU box stays queryable on a CPU-only spoke.

The larger build win is not the GPU at all. `make index` reuses the vector of any
passage whose text has not changed (`data/processed/vector_cache.sqlite`), so
landing one document no longer re-encodes the corpus.

## How the graph works

`graph_query` walks `edges` with a recursive CTE. Edges come from Python imports
(stdlib AST) and document front matter (`depends_on:`, `uses:`), plus weaker
`references` and `mentions` edges from wiki links and backticked identifiers.

Impact is transitive, which is why this is a graph and not a join. The fixtures
encode `service_api → search_core → vector_store`, so asking what a change to
`vector_store` affects must reach `service_api` at depth 2 — and `service_api`
never mentions `vector_store` anywhere in its text.

Two guards keep the recursion honest: a visited-path check (so a cyclic import
chain cannot hang the agent) and a depth cap.

The graph knows only what the corpus declares. Dynamic imports, plugin
registries, and dependencies expressed only in prose are invisible to it, so
treat it as a lower bound on impact.

## Layers and audits

| Layer | Kind | Job |
| --- | --- | --- |
| `bronze.*` | VIEW | Landing shape preserved; explicit type casts only |
| `silver.*` | FULL | Cleaned, de-duplicated, current-batch only |
| `gold.*` | FULL | `chunks`, `entities`, `relations` — the index input |

Audits are **blocking**: a failure stops the build rather than warning, because
the next step writes the index the agent trusts. Four generic audits live in
`transform/audits/` alongside the built-ins (`not_null`, `unique_values`):

- `assert_text_not_blank` — a blank chunk is retrievable but says nothing.
- `assert_no_self_loops` — a self-loop makes graph traversal revisit forever.
- `assert_relation_endpoints_resolved` — an edge whose endpoint has no node makes
  multi-hop traversal walk off the graph and silently under-report impact.
- `assert_every_document_chunked` — a document that reaches `silver` but owns no
  chunk vanished whole from retrieval, and absence leaves no malformed row to catch.

Its section-grain twin lives in the chunker, not an audit, because it needs the
profile identity the gold layer discards: `pipeline/chunking.py` raises
`ChunkingError` when a section a profile declared produces no chunk — the 부칙/시행일
loss, made impossible to ship silently for every profile, present and future.

Each defect these audits exist to catch was reproduced during development and
confirmed to fire — most recently by re-introducing the 부칙 bug and watching the
build fail — which is the argument for keeping them blocking.

`pipeline/run.py` restates the bronze models on every build. That forces SQLMesh
to clear their intervals and re-execute everything downstream, so a build always
rebuilds gold from what just landed *and* always re-runs the audits. A bare
`plan` is a no-op when no model definition changed, and `run --ignore-cron` only
fills *missing* intervals, so neither alternative would re-run the gate.

## NOTE: the loader is project-local, and why

**There is no Singer target for DuckLake on PyPI** (`target-ducklake` returns
404). The documented fallback is to land via `target-duckdb` and let SQLMesh
promote the result. That fallback was implemented, tested, and **rejected**:

> `target-duckdb` 0.8.0 loads each batch with
> `COPY <table> FROM '<temp>.csv' WITH (new_line '\r\n')` and never passes
> `HEADER false`. Against DuckDB ≥ 1.x the CSV sniffer classifies the first data
> row as a header, so **the first record of every stream is silently dropped**.
> Reproduced on this corpus: the tap emitted 7 documents / 20 chunks /
> 28 relations; 6 / 19 / 27 landed. Adding `HEADER false` to the same `COPY`
> loads all 7.

Silent row loss upstream of a RAG index is worse than a visible failure, so this
repo ships **`target-ducklake-local`** (`pipeline/target_ducklake.py`), a small
loader built on the documented Meltano SDK `BatchSink` API that writes straight
into DuckLake. It is a project-local plugin, **not** a Meltano Hub plugin, and it
removes the promote-from-a-staging-database hop entirely.

Load semantics are full refresh: each stream's table is dropped and recreated
once per process. The tap always re-reads the whole immutable raw zone, so this
is both correct and what makes re-runs idempotent.

## Version floors

Every pin traces to a source. Nothing here is from memory.

| Component | Pin | Floor comes from |
| --- | --- | --- |
| Python | `>=3.10` | `max()` of the `requires_python` of Meltano 4.2.1 (`>=3.10`), duckdb 1.5.4 (`>=3.10.0`), singer-sdk 0.54.5 (`>=3.10`), sqlmesh 0.236.0 (`>=3.9`), read from PyPI JSON metadata |
| DuckDB | `>=1.5.2` | ducklake.select: "DuckLake v1.0 requires DuckDB v1.5.2+" |
| Meltano | `>=4.2.1` | Current PyPI release |
| SQLMesh | `>=0.236.0` | Current PyPI release; its DuckDB gateway supports `type: ducklake` catalogs natively |
| sqlite-vec | `>=0.1.9` | Current PyPI release |
| singer-sdk | `>=0.54.5` | Current PyPI release |

`.python-version` pins **3.12** for the venv. That is above the `>=3.10` floor
and is a compatibility choice for the Meltano plugin venvs, not a hard
requirement.

## Document formats

`SUPPORTED_SUFFIXES` in `pipeline/chunking.py` is the whole list; a file whose
suffix is not in it is skipped silently by the tap.

| Group | Formats | Reader | Installed by default |
| --- | --- | --- | --- |
| Text | `.md` `.markdown` `.txt` `.rst` | none needed | yes |
| Code | `.py` | none needed | yes |
| Hancom | `.hwp` | `hwpkit` | yes |
| OOXML | `.hwpx` `.docx` `.xlsx` `.pptx` | standard library | yes |
| PDF | `.pdf` | `pypdf` (text layer only) | yes |
| Office 97-2003 | `.doc` `.xls` `.ppt` | `office-oxide` | **no** — `uv sync --extra legacy` |

The OOXML readers use no third-party parser at all. An OOXML file is a zip of
XML, so extraction is a sweep over every text node rather than a schema-aware
parse, which is what makes it pick up tables, text boxes and footnotes without
knowing the schema. `.doc`/`.xls`/`.ppt` are the exception: their text sits
behind a piece table in an OLE compound file and no maintained pure-python reader
exists, so they are the one place a young parser is used — scoped to exactly the
formats with no alternative, kept opt-in, and still subject to `_validate`, which
rejects a document rather than index a misparse.

**Images are not a supported format, on purpose.** A `.png` has no text to
extract deterministically, only a model's reading of it, and `make build` runs no
model. Scans go through the operator path instead:

```bash
uv sync --extra ocr
uv run python tools/ocr/ocr_prepare.py scan.pdf -o draft.md      # scanned PDF
uv run python tools/ocr/ocr_prepare.py page_01.png page_02.png -o draft.md
uv run python tools/ocr/ocr_prepare.py scans_dir/ -o draft.md
# review draft.md against the source, then save the corrected text as
#   data/inbox/documents/<name>.txt
```

A born-digital PDF needs none of this — `make build` reads its text layer
directly. A PDF with no text layer raises rather than indexing an empty
document, which is the signal to run OCR on it.

## Enabling optional pieces

**S3 sync.** Set `DATA_REMOTE=s3://bucket/prefix` in `.env` and
`uv add "dvc[s3]"`. `make sync` then configures the remote and pushes. While
`DATA_REMOTE=local_only` it is an explicit, logged no-op rather than a silent
one.

**Web fetching.** `agent/tools/scrapling_mcp.py` is the seam. It is not a
dependency of the build and nothing in the pipeline imports it:

```bash
uv add "scrapling[ai]"
uv run scrapling install
claude mcp add ScraplingServer "$(pwd)/.venv/bin/scrapling" mcp
```

Prefer an official API over scraping. Respect robots.txt and site terms; do not
defeat anti-bot measures. Fetched pages go into `data/inbox/documents/` and
through the normal pipeline, so they carry the same provenance as everything
else.

## Agent surface

**`AGENTS.md` is the single source of truth** for agent instructions, in the
[agents.md](https://agents.md) format. It is read natively by OpenAI Codex,
Google Antigravity, Gemini CLI, Cursor, Copilot, Windsurf, Zed and others.

Claude Code is the exception: it reads `CLAUDE.md` and *not* `AGENTS.md`. So
`CLAUDE.md` is a two-line pointer containing `@AGENTS.md`, which is the import
form the Claude Code docs recommend. A symlink also works, but creating one on
Windows needs Administrator rights or Developer Mode, so the import is the
portable choice. Add Claude-specific instructions *below* the import; anything
above it would be read before the shared rules.

Editing rule: change `AGENTS.md` only. `CLAUDE.md` should never accumulate a
second copy of the rules — that is precisely how two agents start following
different instructions.

Everything about setup, architecture and rationale lives here in the README, not
in `AGENTS.md`. Agent guides are read on every single session, so they are kept
to what an agent must not get wrong; anything derivable from the codebase is
left out on purpose.

### Skills follow the same pattern

Workflows are packaged as [Agent Skills](https://agentskills.io) — a folder with
a `SKILL.md` carrying `name` and `description` frontmatter. Both fields are set
on both skills, which satisfies all three toolchains at once (Codex requires
both; Antigravity requires only `description`).

`.agents/skills/` is the source of truth. Codex and Antigravity both read that
path natively:

| Tool | Project skills path |
| --- | --- |
| OpenAI Codex | `$REPO_ROOT/.agents/skills/` |
| Google Antigravity | `<workspace>/.agents/skills/` (`.agent/` still works, deprecated) |
| Claude Code | `.claude/skills/` only |

So `.claude/skills/<name>` points into `.agents/skills/<name>` — normally a
**symlink**, which the Claude Code docs explicitly support: a skill entry "can be
a symlink to a directory elsewhere on disk", and the same target reachable twice
is loaded once. Zero duplication, so the two cannot drift.

That directory is **generated and git-ignored**: run `make sync-skills` (or
`make skills` from the repo root) once per clone, alongside `make hooks`. It is
not committed because git carrying symlinks is a trap on Windows — a
`core.symlinks=false` clone turns each one into a text file holding a path, and
the skills then fail to load with no error at all. Generating instead means the
mechanism is chosen on the machine that will use it: symlinks where the OS allows
them, a copy where it does not, and a copy is promoted back to a symlink the next
time one can be made. `make verify` checks the adapter whenever it exists — a
drifted copy fails the gate rather than silently serving old instructions — and
reports "not built yet" rather than failing when it does not, so a fresh clone
can still commit.

- `.agents/skills/document-drafting/SKILL.md` — draft grounded in the corpus.
- `.agents/skills/code-impact-analysis/SKILL.md` — trace a change's blast radius.
- `agent/tools/` — `hybrid_search`, `graph_query`, and the Scrapling MCP hook.
  Each is importable *and* a JSON-printing CLI, so one implementation backs an
  in-process call, a shell call, and an MCP wrapper.

> **NOTE: deviation from the original folder contract.** The spec placed skills
> at `agent/skills/`, which no toolchain discovers. They were moved to
> `.agents/skills/` so Codex, Antigravity and Claude Code all load them
> automatically. `agent/` still holds the tools and prompts, which are project
> code rather than a standard-governed location.

## Verification

`make verify` runs the whole gate: evasion-token scan, `uv sync`, tool versions,
`make build`, the smoke test, the data-plane git check, the agent guides, and
skill frontmatter.
