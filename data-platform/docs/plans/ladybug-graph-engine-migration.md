# [기각 · SHELVED] Ladybug 그래프 엔진 전환 — 구현 계획 (하드닝됨)

> **결정(2026-07-26): 전환하지 않는다. 그래프 순회 엔진은 재귀 CTE를 그대로 유지한다.**
> 이 문서는 착수되지 않은 계획으로 **기록·재고(再考) 근거**로만 남긴다(구현 안 함).
> 아래의 "GO to implement"는 *당시 계획 관점의 판정*이며 **현재 방침이 아니다**.
> 재고 조건(세 가지 동시 충족 시에만): ① 그래프가 수백만 엣지 규모로 커져 CTE 순회가
> 실측 병목이 되고, ② 임의 Cypher/그래프 분석이 제품 요구가 되며, ③ 단일-파일 불변식을
> serving 디렉터리로 완화할 의향이 생길 때. 그전까지는 CTE 유지가 정답이다.

- 상태: **기각(SHELVED) — 그래프 엔진 재귀 CTE 유지 결정(2026-07-26). 미착수.**
- 절차: 베스트프랙티스 조사(웹검증) → 초안 → 적대적 리뷰(4렌즈) → 하드닝. 이 문서는 하드닝 결과.
- 목표: `data-platform`의 그래프 순회 엔진을 재귀 CTE에서 LadybugDB(임베디드 openCypher, Kùzu 계열)로 전환.
- 원칙: serving 단일-파일 불변식을 '단일 serving 디렉터리'로 완화, GRAPH_ENGINE 플래그로 이중구동+엄격 패리티 게이트 후 전환, CTE는 규칙 충족 전까지 폴백 유지.

## GO / NO-GO

GO to implement — with two blocker-derived items that must land as written in their phases, not deferred. (1) Phase 0/2/3: the shared graph_generation token (index_meta + lbdb Meta + pre-open sidecar) and the pre-Database() sidecar guard are mandatory before GRAPH_ENGINE=ladybug is ever the default — they close the silent cross-store skew that the split newly introduces. (2) Phase 0: the committed wheelhouse (find-links) is mandatory, not optional — without it the stated offline fresh-machine requirement cannot be met given no-build-package. Both are fully specified above and are normal engineering work, not open questions. The three residual_open_decisions (wheelhouse storage medium, dvc-push-vs-local-rebuild of lbdb, CTE-permanence rule) each have a safe recommended default, so implementation can proceed on the defaults if the user defers. No blocker requires further discovery; the one empirical unknown (ladybug 0.18.3's actual open-time failure mode and list/predicate catalog coverage) is scheduled as Phase-1 bring-up work and is fully backstopped by the sidecar guard and the strict parity gate, so it does not hold up starting.

## 착수 전 대기 중인 사용자 결정 (각 안전 기본값 있음)

1. **How the mandatory ladybug 0.18.3 wheelhouse is stored and distributed: commit wheels directly into the repo, use Git LFS, or stand up an internal/private package index that offline machines can reach.**
   - 왜 사용자 결정인가: This is a repo-size / infra / security-policy tradeoff the platform owner controls (binary blobs in git vs LFS quota vs running an index), not a code-design choice — and it is load-bearing because offline fresh-machine install is a HARD requirement with the sdist fallback deliberately disabled.
   - 권장 기본값: Commit the supported {CPython 3.10-3.14 × macOS arm64/x86_64, manylinux, musllinux, Windows} wheels into an in-repo wheelhouse/ via Git LFS, with uv pointed at it via find-links; refresh on each supported-Python addition. Simplest to make air-gapped installs work with no extra infrastructure.

2. **Whether graph.lbdb is pushed through dvc with the rest of data/ or excluded and always rebuilt locally from synced gold on each spoke.**
   - 왜 사용자 결정인가: It sets the multi-machine operational contract (does a dvc pull give a spoke a ready-to-serve graph, or must every spoke run make build?), and depends on how the team actually distributes serving state — an ops policy, not an implementation detail.
   - 권장 기본값: Exclude graph.lbdb (+ sidecar) from the dvc artefact and treat it as a local cache rebuilt from synced gold, because a pre-1.0 lbdb is not portable across wheels and would otherwise require a wheel-version match on every spoke; index.sqlite continues to sync as today.

3. **Commit to eventually removing the recursive CTE (accepting a future git-revert-scale rollback), or accept permanent dual-write with the CTE retained indefinitely as a proven fallback over ladybug.**
   - 왜 사용자 결정인가: It trades a standing maintenance + double-build cost against rollback safety for a pre-1.0 single-steward fork whose upstream is archived and which may never reach format stability — a risk-appetite call the platform owner owns, not something a plan can decide unilaterally.
   - 권장 기본값: Retain the CTE until ladybug clears an explicit bar (3 consecutive all-gates-green releases AND one survived on-disk-format bump); if the fork never clears it, keep the CTE permanently as the fallback and treat ladybug purely as an acceleration layer. Safest given the archived-upstream durability risk.

---

## Hardened migration plan: recursive-CTE graph engine → LadybugDB (data-platform)

Grounded in verified files: `pipeline/build_graph.py` (the CTE in `_traversal_sql` at lines 129-187, `graph_query`, `resolve_node` at 238-266 — `LIKE '%'||:needle||'%'` with **no ESCAPE**, `build_graph` DROP+recreate + `INSERT OR IGNORE` + index_meta node_count/edge_count at 118-122), `pipeline/graph_rag.py` (`_require_graph` reads `sqlite_master`:74-78, `_edges_touching`:100, `_node_labels`:114, `_discover_related`:205, Python co-citation), `agent/tools/graph_query.py` (raw `SELECT node_id FROM nodes` start-check), `agent/tools/graph_rag.py` + `agent/tools/hybrid_search.py` (provenance footer reads `read_index_meta` node_count/edge_count at :135-136 / :110-111), `pipeline/eval_graph.py` (compare() **SKIPS on count drift** at 162-169), `pipeline/eval_graph_rag.py`, `pipeline/smoke_test.py`, `pipeline/run.py` (`stage_index` calls `build_index` then `build_graph`, each opening/closing its **own** `open_lake`; `stage_sync` dvc-pushes `data/`; `STAGES=('ingest','transform','index','sync')`), `pipeline/watcher.py` (POLL 2s / QUIET 1s → whole-pipeline rebuild on every inbox change), `tools/viz/graph.py` + `tools/viz/server.py`, `pipeline/build_rag.py` (`build_index` DROP TABLE **in place** at 517-542, `index_signature` guard at 749, `connect_index`/`open_lake`/`read_index_meta`), `pipeline/__init__.py` (`Paths`/`Settings`), the `Makefile`, `pyproject.toml`. Anchoring facts: relation vocabulary is a **closed set of 8** (`gold/relations.sql`: `depends_on`/`imports`/`delegates_to`=1.0, `uses`/`defines`=0.8, `supersedes`=0.6, `references`=0.5, `related_to`=0.4, ELSE `mentions`=0.3); fixture graph is 96 nodes / 76 edges; gold audits `assert_relation_endpoints_resolved.sql` + `assert_no_self_loops` guarantee no dangling endpoints or self-loops at COPY time; `eval_graph_queries.json` holds **7** queries, all with **exact** start slugs and no relation filter; `data/` and `*.sqlite` are gitignored.

The migration is **additive, reversible, and generation-guarded**: introduce a Ladybug backend behind a `GRAPH_ENGINE` flag, keep the SQLite `nodes`/`edges` tables and the CTE alive throughout, stamp both serving artifacts with a shared build generation, gate the default flip on strict parity, and defer CTE removal behind a concrete, measurable rule. Every graph read goes through one new seam, `pipeline/graph_store.py`.

---

### Phase 0 — Dependency, durability, and config groundwork (no behavior change)

1. **`pyproject.toml` + wheelhouse (offline-install is a HARD contract, not "optional")**
   - Add exact pin `"ladybug==0.18.3"` to `[project].dependencies` (exact, not a range, because the on-disk format is pre-1.0 and a floating minor could silently break it). `requires-python = ">=3.10"` already satisfies ladybug's floor.
   - Add `[tool.uv] no-build-package = ["ladybug"]` so a platform gap fails **loud** instead of compiling the C++ sdist offline.
   - **Commit a mandatory in-repo `wheelhouse/`** (via Git LFS) holding the ladybug 0.18.3 wheels for the team's supported `{CPython 3.10-3.14} × {macOS arm64/x86_64, manylinux, musllinux, Windows}` matrix, and point uv at it (`[tool.uv.sources]` / `find-links` / `UV_FIND_LINKS`). Locked hashes verify *integrity*; the wheelhouse provides *availability* on a cold cache — without it, `uv sync --offline` on a fresh/air-gapped box has nothing to install from and the whole project (one shared lock) fails to build. PyPI hashes remain the online convenience path.
   - `uv lock` to record wheel hashes. Ladybug is a **core dependency, not an extra**, so `SYNC_EXTRAS` is untouched.
   - **Cross-tool guard:** `no-build-package` is uv-only. Add a CI/container-level `--only-binary=:all:` (or `PIP_ONLY_BINARY=:all:`) policy and declare uv-only installation as a project invariant, so a `pip install .` or Dockerfile base image cannot silently reinstate the sdist compile.
   - **Escape hatch for interpreter/OS churn:** document that any Python/platform bump that outruns the frozen 0.18.3 wheel matrix is gated by a ladybug re-pin + full parity re-run; provide a narrow, opt-in `ALLOW_LADYBUG_SDIST=1` toggle (drops `no-build-package` for a deliberate bridge build) so a platform gap is a conscious act, never an absolute wall.

2. **`pipeline/__init__.py`**
   - `Paths`: add `graph_lbdb: Path` = `data/serving/graph.lbdb` and `graph_lbdb_meta: Path` = `data/serving/graph.lbdb.meta.json` (the pre-open sidecar). `ensure()` already creates `serving`.
   - `Settings`: add `graph_engine: str`; in `get_settings()` add a `_graph_engine()` validator (default `"sqlite"`, allowed `{"sqlite","ladybug","dual"}`) so a typo'd `.env` fails on first read, exactly like `_graph_depth()`.

3. **`.gitignore`**: add `*.lbdb`, `*.lb.wal`, `*.lbdb.tmp`, `*.lbdb.lock`, `*.lbdb.meta.json` under the serving-artefacts block (belt-and-suspenders for out-of-tree `PLATFORM_DATA_DIR`).

4. **`.env.example`** (+ local `.env`): document `GRAPH_ENGINE=sqlite  # sqlite | ladybug | dual`.

5. **`AGENTS.md` invariant #3 + `README.md`** — amend #3 to preserve BOTH clauses it carries (snapshot + which-documents-agree), not drop half:
   > **#3 — Serving is one *directory*, snapshotted as a unit.** Serving is `data/serving/` holding `index.sqlite` (sqlite-vec vectors + FTS5 keywords) **beside** `graph.lbdb` (the graph). Ladybug cannot write its graph into SQLite (ATTACH/scan is read-into-only), which forces the split. Two rules replace the old single-file guarantees: (a) the two artefacts must be captured/backed-up together at the **same build generation** with no `*.lb.wal` present — a directory copy is not atomic, so snapshot only from a completed build; (b) the graph and the chunks are separate files that must agree about which documents exist, enforced by the **shared `graph_generation` token** (below), not by co-location. The vector/keyword halves still live in one `index.sqlite` and still cannot disagree.

### Phase 1 — Ladybug backend module `pipeline/graph_ladybug.py` (new, dormant)

Implements the engine; nothing calls it yet.

- `open_graph_ro(lbdb)` / `open_graph_rw(lbdb)`: one `ladybug.Database` (cache RO Database per `(path, mtime)` at module scope), `Connection` per call (per-thread). RO opener raises `FileNotFoundError("... run make build")` if the file is absent.
- **Guard BEFORE `Database()` open (do not trust pre-1.0 open-time validation as the first line):** `open_graph_ro` first reads the plain-text sidecar `graph.lbdb.meta.json` (`{ladybug_version, graph_generation, node_count, edge_count}`) with a cheap file read, and compares `ladybug_version` to `ladybug.__version__` **and** `graph_generation` to `read_index_meta()['graph_generation']`. On any mismatch it raises the actionable "graph.lbdb is stale/format-mismatched — run make build", **without ever constructing the C++ opener** on a possibly-incompatible file (closes the segfault-before-friendly-message hole). Only after the sidecar passes does it open the Database. (Empirically confirm ladybug 0.18.3's actual open-time failure mode on a deliberately old-format file during Phase 1 bring-up; the sidecar is the guard regardless of what that check turns out to do.)
- `build_graph_lbdb(paths, generation)`: the RW build (Phase 2).
- `traversal_cypher(direction, max_depth, has_filter)`: builds the parity Cypher (see below). `max_depth` is a validated **literal** (1..12) because the `*1..N` bound is compile-time; `$start`, `$relations`, `$limit` stay real parameters.
- `_ladybug_graph_query`, `_ladybug_resolve_node`, `_ladybug_edges_touching`, `_ladybug_node_labels`, `_ladybug_node_exists`, `_ladybug_graph_stats` (node_count/edge_count/present-node-set/relation→min-weight), `read_ladybug_graph(lbdb, include_fixtures)` (viz). Each returns dict/row shapes **byte-identical** to the SQLite versions, incl. `via_relations` joined to a **comma string** (matches `GROUP_CONCAT`; smoke asserts `hit["via_relations"] == "imports"`).

### Phase 2 — Deterministic, idempotent, crash-safe lbdb build

`build_graph_lbdb(paths, generation)` runs after `build_graph` returns and **opens its own `open_lake(paths)`** (a second read of the same committed gold — there is no shared handle to reuse; `build_graph`/`build_index` each open and close their own lake. Sequential reads of committed gold cannot drift):

1. **Reset deletes only stale scaffolding — NEVER the live `graph.lbdb`.** Delete `graph.lbdb.tmp`, `graph.lbdb.lock`, and any leftover tmp WAL. Build into `graph.lbdb.tmp` (WAL named off the `.tmp` stem so it cannot collide with a live reader's `graph.lb.wal`). `os.replace(tmp → graph.lbdb)` is the **only** operation that retires the old file — on POSIX it atomically swaps inodes and existing RO fds keep serving the old inode across the swap (this is what preserves the live `graph-serve`/agent/watch reader — the old plan's "delete then swap" would open a no-file window that watcher's 2s rebuild loop maximizes). *Windows caveat:* `os.replace` onto a file another process holds open raises `PermissionError`; document that on Windows `graph-serve`/agent must not hold the lbdb handle across a build, or restrict the atomic-swap guarantee to POSIX.
2. Export **sorted, deduplicated** Parquet from the DuckDB/DuckLake connection (Parquet hand-off, **not** Ladybug ATTACH — the DuckLake `ducklake` catalog extension is not reliably ATTACH-able by Ladybug; Parquet is deterministic, offline, and carries its own schema):
   - `_nodes.parquet` ← `SELECT entity_id, kind, label, doc_id, rel_path, in_degree, out_degree FROM lake.gold.entities ORDER BY entity_id`
   - `_edges.parquet` ← relations scan, deduped to reproduce `INSERT OR IGNORE` keep-first on PK `(source,target,relation)`. **Parity-critical:** the SQLite build feeds `INSERT OR IGNORE` rows ordered by `(source_entity,target_entity,relation)` only, so among genuine cross-document duplicates the surviving weight is *already order-unspecified on the sqlite side*. To make **both engines deterministic and identical**, pin one canonical dedup for both: `QUALIFY row_number() OVER (PARTITION BY source_entity,target_entity,relation ORDER BY doc_id, rel_path)=1`, and (in the same change) make `build_graph`'s gold read apply the same `ORDER BY source_entity,target_entity,relation,doc_id,rel_path` so its `INSERT OR IGNORE` keep-first matches the QUALIFY winner. Without this, a duplicate edge's surviving weight can drift between engines and move `path_strength`.
3. `CREATE NODE TABLE Node(...)`, then `CREATE REL TABLE Edge(FROM Node TO Node, ...)`; `COPY Node FROM '_nodes.parquet'` **before** `COPY Edge FROM '_edges.parquet'` (rel COPY resolves FROM/TO against existing PKs — satisfied because gold.entities is the UNION of every edge endpoint per `assert_relation_endpoints_resolved.sql`). Exactly **one** COPY per table.
4. `CREATE NODE TABLE Meta(k STRING PRIMARY KEY, v STRING)`; write `node_count`, `edge_count`, `ladybug_version`, and `graph_generation`.
5. `CHECKPOINT`, close Connection+Database (merges WAL into the file so the artifact is self-contained), **assert no `.lb.wal` remains**, delete temp parquet, write the `graph.lbdb.meta.json` sidecar (`ladybug_version`, `graph_generation`, `node_count`, `edge_count`), then `os.replace(tmp → graph.lbdb)`.
6. **Generation + provenance write-back into `index.sqlite`:** `build_graph` (Phase 5) computes `graph_generation = sha256` over the sorted `(node_rows, edge_rows)` it already fetches and writes it to `index_meta` alongside node_count/edge_count. `build_graph_lbdb` recomputes the identical hash from its own gold read (deterministic → equal) and writes it to lbdb Meta + sidecar. **Also have `build_graph_lbdb` write `node_count`/`edge_count` into `index_meta`** so the provenance footers (`agent/tools/hybrid_search.py:110-111`, `agent/tools/graph_rag.py:135-136`) stay populated after Phase 7 retires `build_graph`'s writes.

### Phase 3 — Dispatch seam `pipeline/graph_store.py` (new)

The single module every consumer imports. Reads `GRAPH_ENGINE` via `get_settings()` and dispatches `graph_query`, `resolve_node`, `edges_touching`, `node_labels`, `node_exists`, `edges`, `graph_stats`, `read_index_graph` — signatures **identical** to today's. The `sqlite` branch forwards the passed `connection` to the CTE functions imported from `build_graph.py`; the `ladybug` branch ignores the sqlite handle and opens its own RO lbdb handle (after the sidecar generation check); `dual` runs both and asserts parity in-band, returning the ladybug result. `build_graph.py` stays the CTE source of truth and must **not** import `graph_store` (no cycle); `graph_store` imports CTE impls from `build_graph` and ladybug impls from `graph_ladybug`.

### Phase 4 — Route every consumer through the seam (still `GRAPH_ENGINE=sqlite` → zero behavior change)

- **`agent/tools/graph_query.py`**: import `graph_query`/`resolve_node`/`DIRECTIONS` from `graph_store`; replace the raw `SELECT node_id FROM nodes WHERE node_id = ?` with `graph_store.node_exists(node, connection=...)`.
- **`pipeline/graph_rag.py`**: `_edges_touching`/`_node_labels` become thin wrappers over `graph_store.edges_touching`/`node_labels`; `_require_graph` becomes engine-aware (sqlite: `sqlite_master`; ladybug: sidecar + Meta present). All co-citation/hub-damping Python stays byte-identical. `_discover_related` keeps its `connection` param (used only in sqlite / synthetic tests).
- **`pipeline/eval_graph.py`**: `graph_query` from `graph_store`; replace the direct `COUNT(*) FROM nodes/edges`, `SELECT node_id FROM nodes`, `SELECT relation, MIN(weight) ... GROUP BY relation` with `graph_store.graph_stats(...)` so the floor runs under either engine.
- **`pipeline/eval_graph_rag.py`**: only touches `chunks` (sqlite) + routed `graph_rag_search` → no graph-table change.
- **`pipeline/smoke_test.py`**: `test_graph_query`/`test_graph_rag` import from `graph_store`; provenance edge-read becomes `graph_store.edges(...)`. Keep the sqlite `nodes`/`edges` assertions during dual-write; add a `graph.lbdb`+sidecar+Meta assertion when `GRAPH_ENGINE=ladybug`. `test_reachability_diagnostics` (synthetic `:memory:` sqlite driving `_discover_related`) **stays on the sqlite path by construction** — it tests Python co-citation logic, not the engine, and must never route through ladybug (it would fail on a fresh clone with no lbdb).
- **`tools/viz/graph.py`**: add `read_ladybug_graph` + an lbdb `index_has_graph` variant; dispatch **only** the `--source index` path on `GRAPH_ENGINE`. `auto`/`lake` paths read DuckLake directly and are unaffected.
- **`tools/viz/server.py`**: `discover_sources`/`load_source`/`source_signature` recognize `graph.lbdb` as an index source when engine=ladybug, reusing the graph.py loaders.

### Phase 5 — Dual write in the build (with the experimental path fenced off)

`pipeline/run.py` `stage_index`, after `build_graph(paths)` (which now also stamps `graph_generation` into `index_meta`), calls `build_graph_lbdb(paths, generation)`. **While `GRAPH_ENGINE != "ladybug"`, wrap `build_graph_lbdb` in try/except that logs a warning and continues** — the shipping sqlite path (vectors, FTS, nodes/edges, `export_chunks`, `sync`) must complete even if the experimental Ladybug build throws (a pre-1.0 catalog gap, a yanked wheel, a format bump). Only after the Phase-7 flip makes ladybug the serving engine does an lbdb build failure become fatal. This keeps the experimental artifact from holding the default build (and the watcher's advancing serving index) hostage during exactly the window where lbdb defects are expected. `STAGES` ordering unchanged; log lbdb node/edge counts alongside sqlite.

### Phase 6 — Parity gate (`GRAPH_ENGINE=dual` + strict diff harness), before any flip

**Three gates; recall floors are NOT the exact-parity oracle** (verified: `eval_graph.compare()` is tolerance-based and *skips* on count drift, so it cannot see via/ordering/weight drift or a silently-smaller graph — the old plan overstated what gates 2/3 prove). Strict equality lives in the harness:

- **Gate 1 — `pipeline/graph_parity.py` (`make parity`), strict row equality, run on a representative corpus, not only fixtures.** Sweeps `{sampled start nodes} × {upstream,downstream} × {depth 1..3} × {no filter, single relation, disjunction}` comparing `_cte_graph_query` vs `_ladybug_graph_query`, normalizing rows to `(node_id, depth, full-precision total_weight, frozenset(via_relations.split(',')))` in identical order (compare **unrounded** weight so a sub-4dp tie-break flip is catchable — see ordering fix below). **First assert graph identity, hard-fail (not skip):** `node_count`, `edge_count`, the full present-node **set**, and the `relation→MIN(weight)` map must be identical across engines. Then diff `resolve_node` **on partial, underscore-containing needles** (e.g. `service`, `_api`, `자산기본법안_민병덕`), not just the exact start slugs the 7 canned queries use — the LIKE-vs-CONTAINS divergence lives entirely in the non-exact tail. Then diff `edges_touching` rows.
- **Gate 2 — `GRAPH_ENGINE=ladybug make eval-graph --assert-baseline` green** (protects reach/p_at_1/mrr/path_strength). Treated as a *floor*, not the identity oracle — identity is Gate 1's job.
- **Gate 3 — `GRAPH_ENGINE=ladybug make eval-ask --assert-baseline` green** (related_recall floor — protects the co-citation/hub-damping path riding on `edges_touching`, which graph_query parity alone would not cover).

CI runs all three from a **from-scratch `--offline` build against the wheelhouse**, proving offline-installability + format-vs-wheel agreement + traversal parity together.

### Phase 7 — Flip the default, then (behind a concrete rule) retire the CTE

- Only after all three gates are green across a representative corpus: change `GRAPH_ENGINE` default to `"ladybug"` in `_graph_engine()` and `.env.example`. Rollback stays one env var (Tier 1) with no rebuild.
- **Keep the CTE-path tested after the flip:** parameterize the CI "second engine" run as the **NON-default** engine (or run an explicit `{sqlite, ladybug}` matrix) so the retained sqlite/CTE rollback path is gated green for as long as both write paths exist — otherwise the "instant, config-only" Tier-1 rollback rots and the first incident operator discovers it broke.
- **Concrete CTE-removal rule (the hedge the task asked to nail down):** the CTE + sqlite `nodes`/`edges` write path stay until ladybug clears an explicit bar — **3 consecutive ladybug releases each passing all three gates, AND at least one release that bumps the on-disk format version, proving the delete+rebuild+sidecar-guard survives a real format change.** If the single-steward fork never clears that bar (upstream Kùzu is archived; pre-1.0 may persist indefinitely), the **documented default is to KEEP the CTE permanently** — ladybug as an acceleration layer over a retained CTE fallback — and record the double-build cost as the accepted price of that safety. Removal, if it happens, is a separate change re-gated by the same three checks; only then does rollback rise to a git revert + rebuild.

Each phase is independently shippable and leaves `make verify` green.

---

### Schema DDL (single generic Edge table)

```
CREATE NODE TABLE Node(node_id STRING PRIMARY KEY, kind STRING, label STRING,
                       doc_id STRING, rel_path STRING, in_degree INT64, out_degree INT64);
CREATE REL TABLE Edge(FROM Node TO Node, relation STRING, weight DOUBLE,
                      doc_id STRING, rel_path STRING, evidence STRING);
CREATE NODE TABLE Meta(k STRING PRIMARY KEY, v STRING);  -- node_count, edge_count, ladybug_version, graph_generation
```

One homogeneous `Edge` table with `relation` as a **property** (not the rel type). It is 1:1 with today's generic `edges` table (every endpoint is the same self-referential `Node`; `kind` is an attribute), so no reshaping into per-`(FROM,TO)` typed tables. The runtime `relations=[...]` filter becomes a bound value predicate `WHERE r.relation IN $relations` — Ladybug can parametrize **values** but not **labels**, so typed-per-relation tables would force compiling a label-disjunction string from an allowlist, fragile across pre-1.0 parser separator forms. The 8-relation vocabulary is small and stable; planner-pruning gains from typed tables are negligible at this scale (76 edges now, low thousands later) and not worth the parity risk. `kind` stays a STRING property (multi-label = multiple node TABLES → N² rel tables). REL TABLE GROUP is wrong here (keyed by distinct FROM/TO pairs; all Node→Node collide). Typed tables noted as future-only, if the vocabulary ever becomes dynamic or graph-algorithm projections are wanted.

### Parity-preserving Cypher (the crown jewel)

Reproduce `_traversal_sql` with a bounded **ACYCLIC** variable-length match + two-level aggregation — **not** WSHORTEST (which minimizes a summed scalar and returns one path per target; it cannot express min-HOP-COUNT-then-MAX-accumulated-weight, which may come from *different* paths, nor via_relations over min-depth arrivals). DOWNSTREAM shown; UPSTREAM flips the arrow to `<-[e:Edge* ...]-` (== the CTE's `DIRECTIONS` source/target swap). `<MAXDEPTH>` is injected as a validated **literal** (1..12); `$start`/`$relations`/`$limit` are parameters. **Order on the UNROUNDED `max(tw)`, round only in the returned column** (the CTE orders by unrounded `total_weight DESC` at build_graph.py:185 and rounds only in the SELECT projection at :174 — ordering on the rounded value would diverge the moment two path-sums differ by <1e-4, which the weight-retuning workflow can produce):

```
MATCH p = (s:Node {node_id:$start})
          -[e:Edge * ACYCLIC 1..<MAXDEPTH> (r,_ | WHERE $relations IS NULL OR r.relation IN $relations)]->
          (m:Node)
WITH m, length(p) AS d,
     reduce(w=0.0, r IN rels(p) | w + r.weight) AS tw,
     list_extract(list_transform(rels(p), r -> r.relation), length(p)) AS last_rel
WITH m, min(d) AS depth, max(tw) AS tw_unrounded,
     collect({d:d, lr:last_rel}) AS arr
RETURN m.node_id AS node_id, depth, round(tw_unrounded, 4) AS total_weight,
       m.kind AS kind, m.label AS label, m.rel_path AS rel_path,
       list_distinct(list_transform(list_filter(arr, x -> x.d = depth), x -> x.lr)) AS via_relations
ORDER BY depth ASC, tw_unrounded DESC, node_id ASC
LIMIT $limit
```

Mapping: `ACYCLIC` = node-distinct guard = the CTE's visited-node INSTR check (use ACYCLIC, **not** TRAIL, which only bars repeated relationships). `reduce(+weight)` = accumulating `total_weight` (delegates_to=1.0 outranks mentions=0.3). `min(d)`/`max(tw)` computed **independently** over all acyclic paths = the CTE's separate `MIN(depth)`/`MAX(total_weight)`. The per-hop predicate `(r,_ | WHERE r.relation IN $relations)` prunes exactly like the CTE's `AND edges.relation IN (...)`. `via_relations` = distinct LAST-hop relation (`list_extract(...,length(p))`, 1-indexed) among **min-depth** arrivals only (`list_filter(x.d=depth)`) = `GROUP_CONCAT(DISTINCT via_relation) WHERE walk.depth=shortest.depth`; the adapter joins it to a comma string. Order/LIMIT byte-identical.

**resolve_node — make LIKE and CONTAINS the SAME semantics, not merely similar** (SQLite `LIKE` treats `_`/`%` as wildcards; the corpus is underscore-rich — `service_api`, `01_디지털자산기본법안_민병덕-hwp` — so an underscore needle matches any single char under sqlite but a literal underscore under Cypher `CONTAINS`, silently resolving the same human input to a different start node). Normalize both to **literal substring**: in `build_graph.py:255-257` add `LIKE ... ESCAPE '\'` and escape `_`/`%` in the needle, so the ladybug side's literal `CONTAINS` is a faithful mirror:

```
MATCH (n:Node)
WHERE lower(n.node_id) = $exact
   OR lower(n.node_id) CONTAINS $needle
   OR lower(coalesce(n.label,'')) CONTAINS $needle
RETURN n.node_id AS node_id, n.kind AS kind, n.label AS label, n.rel_path AS rel_path,
       n.in_degree AS in_degree, n.out_degree AS out_degree
ORDER BY CASE WHEN lower(n.node_id)=$exact THEN 0 ELSE 1 END, (n.in_degree+n.out_degree) DESC, n.node_id
LIMIT $limit
```

Explicit CASE reproduces `(node_id=exact) DESC` (don't rely on boolean-in-ORDER-BY across engines). Gate 1 diffs this on partial underscore needles.

**edges_touching** (one-hop, NOT the walk): `MATCH (a:Node)-[e:Edge]->(b:Node) WHERE a.node_id IN $ids OR b.node_id IN $ids RETURN a.node_id AS source_id, b.node_id AS target_id, e.relation AS relation, e.weight AS weight, e.evidence AS evidence ORDER BY source_id, target_id, relation` — same columns/order as the SQLite SELECT so the Python co-citation join stays byte-identical.

**Cycle-guard parity precondition:** ACYCLIC (true node identity) is the semantically *correct* guard; the CTE's `/`-delimited visited string (build_graph.py:142/:159) is subtly buggy — a node_id containing `/` produces false-positive "visited" blocks. Current node_ids (entity_ids/slugs) contain no `/`, so they agree today. Add a **build-time assertion that no node_id contains `/`** and a targeted parity fixture with a `/`-bearing node_id, so the intended divergence (ladybug fixes the CTE bug) is a conscious, tested decision.

**Verify-against-pinned-build (Phase 1 bring-up):** confirm `list_extract`/`list_filter`/`list_transform`/`list_distinct`/`reduce` exist in ladybug 0.18.3's catalog (fall back to `min()`/`max()`/`collect()` + lambdas if not), and that the recursive per-hop predicate accepts a bound `$relations` list with the pinned parser's disjunction syntax. `via_relations` compared as a SET (neither GROUP_CONCAT nor collect guarantees element order).

### Load path (summary)

`graph.lbdb` is a **derived** artifact rebuilt from DuckLake gold every `make build`, never migrated across versions. `build_graph_lbdb` opens its own `open_lake(paths)` after `build_graph` returns → export sorted+deduped Parquet (QUALIFY keep-first, ORDER pinned to match the sqlite build) → CREATE Node then Edge, COPY Node before Edge, one COPY each → write Meta (incl. `graph_generation`) → CHECKPOINT, close, assert no WAL, write sidecar, `os.replace` tmp into place. Delete-file (not `DROP TABLE`) each build guarantees the on-disk format matches the installed wheel and identical inputs → identical bytes. The build is the ONLY READ_WRITE handle; every consumer opens READ_ONLY (many RO coexist; RW excludes RO — Makefile sequences build strictly before smoke/eval, and the tmp+atomic-swap handles the live-viewer/watch case).

### Make / STAGES changes

`STAGES` and build/watch flow unchanged. `stage_index` dual-writes `graph.lbdb` beside `index.sqlite` (fenced by try/except while engine≠ladybug). `make impact`/`ask`/`graph`/`graph-serve`/`eval-graph`/`eval-ask`/`smoke` route through the seam and pick the engine from `GRAPH_ENGINE` (viz `auto`/`lake` unaffected; only `--source index` dispatches). `SYNC_EXTRAS` untouched (ladybug is core). `make clean` already `rm -rf data/serving` (removes `graph.lbdb` + sidecar). NEW `parity:` → `$(PY) -m pipeline.graph_parity`. `verify`/CI: after the existing floors, re-run `eval-graph`/`eval-ask` under the **non-default** engine + `$(MAKE) parity`, from a from-scratch `--offline` wheelhouse build. Add a `make` check that fails if the wheelhouse is missing a declared `{python × platform}` tag.

### Sync / DATA_REMOTE (previously missing from blast radius)

`stage_sync` dvc-pushes `data/`, which would include `graph.lbdb`. A pre-1.0 lbdb is not portable across wheels, and a spoke that `dvc pull`s and runs `make ask` (GRAPH_ENGINE=ladybug) **without rebuilding** would open a graph written by a possibly-different wheel. **Decision (recommended): exclude `graph.lbdb`/`.meta.json` from the pushed artefact** (`.dvcignore` / selective add) — treat it as an always-locally-rebuilt cache derived from synced gold, exactly analogous to how it is rebuilt every `make build`. `index.sqlite` continues to sync (its `index_signature` guard reconciles it across machines). Document that a pulled spoke must `make build` to materialize its graph; the sidecar generation guard turns any stale/mismatched pull into an actionable "run make build" instead of a silent wrong-generation join. Confirm the WAL-absent assertion holds at sync time (it does: the build asserts no WAL post-CHECKPOINT).

### Feature-flag transition

`GRAPH_ENGINE` (Settings, default `sqlite`) selects `sqlite|ladybug|dual` at the seam. Both artifacts are always built from the same gold every `make build`, so both engines are queryable and the floors score on either. `dual` runs both in-band and asserts equality (nightly measurement). Default flips to `ladybug` only after the three gates are green on a representative corpus; the sqlite/CTE path and its CI matrix leg stay until the concrete removal rule is met.

### Rollback

- **Tier 1 (engine regression / parity surprise):** set `GRAPH_ENGINE=sqlite`, restart — instant, no rebuild, no data loss (sqlite `nodes`/`edges` + CTE still built). Config change, not code. **Kept green by the non-default-engine CI matrix leg** so it doesn't rot after the flip.
- **Tier 2 (format churn after a wheel bump, or corrupt/stale lbdb):** `rm -f data/serving/graph.lbdb* && make build` regenerates from `lake.gold`; or fall to Tier 1. A stale-format lbdb fails fast at the **sidecar generation/version check before `Database()` open** (not relying on unverified C++ open-time validation), printing "run make build". Because delete+rebuild happens every build and lbdb is excluded from sync, a lbdb is never carried across an engine version in the normal flow.
- **Deep rollback (abandon Ladybug):** git revert of the Phase-4 routing + Phase-5 dual-write + the pyproject pin, then rebuild — but that cost only exists **after** the CTE is deleted, which is deferred behind the Phase-7 rule. Keep the wheelhouse (and note archived Kùzu is a dead-end, not a durable, fallback — it is itself archived with no future-platform wheels).

### Risk register

1. **Ladybug 0.18.3 catalog coverage (pre-1.0):** list_/reduce/per-hop-predicate functions may be absent or spelled differently — verify against the pinned build in Phase 1; fall back to `min()`/`max()`/`collect()` + lambdas. Mitigated by the strict harness before trust.
2. **`*1..N` bound is compile-time:** injecting `max_depth` as a validated literal (1..12) is required; a bug here reopens the non-termination/blast-radius risk the depth cap closes.
3. **min(depth) and max(weight) from different paths; via_relations over min-depth last hops only:** a naive single-path/WSHORTEST port diverges silently and passes smoke — only Gate 1's strict harness catches it.
4. **Enumeration blow-up on a dense corpus:** bounded by depth cap + LIMIT, but validate on a realistic (not 76-edge) corpus before the flip; Gate 1 runs on a representative corpus.
5. **Dedup order-dependence:** the QUALIFY winner and the sqlite `INSERT OR IGNORE` keep-first must use the identical pinned `ORDER BY (source,target,relation,doc_id,rel_path)`, or a duplicate edge's surviving weight drifts and moves path_strength.
6. **Cross-store generation skew (was undetectable):** a partial build could leave a fresh `index.sqlite` beside a stale `graph.lbdb`; the shared `graph_generation` token in `index_meta` + lbdb Meta + sidecar, checked before every ladybug RO open, hard-fails the skew.
7. **Torn snapshot (in-place index.sqlite vs atomic-swap lbdb):** the generation token gates both so a reader detecting a mismatch refuses to serve until it reopens the other; document reopen-both-together. (Optional hardening: stage `index.sqlite` through the same tmp+os.replace discipline.)
8. **graph_rag related section rides on edges_touching, not graph_query:** both routed, both floors re-run under ladybug (Gate 3), or eval-ask silently breaks while eval-graph passes.
9. **Single-writer lock + live reader:** RO reader blocks RW build; reset must delete only stale scaffolding and let `os.replace` retire the old inode — never delete the live file (watcher's 2s loop maximizes any no-file window). Windows `os.replace`-on-open-file caveat documented.
10. **Lingering `graph.lb.wal`:** means an unclean build; CHECKPOINT+close and assert no WAL, at build end and before sync.
11. **Pre-1.0 format not forward/backward compatible:** delete+rebuild-every-build + lbdb excluded from sync + sidecar version guard + a bump re-gated by parity.
12. **Offline fresh-machine install:** mandatory committed wheelhouse (not optional) + `--only-binary` CI policy; PyPI hashes are the online path only.
13. **Interpreter/OS upgrade coupling:** exact pin + no-sdist means a new CPython with no 0.18.3 wheel fails the whole sync; accepted cost with the `ALLOW_LADYBUG_SDIST` escape hatch and re-pin+re-gate rule.
14. **eval-graph skip-on-count-drift:** it cannot be the identity oracle (skips exactly when a count changes); graph identity is asserted hard in Gate 1, not inferred from a green floor.
15. **CTE-removal permanence:** concrete 3-green-releases + one-format-bump-survived rule; default is keep-permanently if the fork never stabilizes; double-build cost recorded as the chosen price.
16. **Supply chain (single-steward MIT fork):** locked hashes + in-repo wheelhouse; a yanked release/vanished fork cannot block reinstall.
17. **Provenance footer staleness:** `build_graph_lbdb` writes node_count/edge_count into `index_meta` so footers survive Phase-7 CTE retirement.
18. **test_reachability_diagnostics must stay sqlite:** synthetic `:memory:` co-citation test; routing it through ladybug breaks it on a fresh clone with no lbdb.

---

## 적대적 리뷰에서 반영된 변경 (초안 대비, 추적용)

- SEMANTICS #1 (major, folded): resolve_node's SQLite LIKE treats _ / % as wildcards while Cypher CONTAINS is literal, and the corpus is underscore-rich (service_api, 01_디지털자산기본법안_민병덕-hwp) — verified no ESCAPE at build_graph.py:255-257. Fix: normalize BOTH to literal substring by adding LIKE ... ESCAPE '\' + needle escaping on the sqlite side; extend graph_parity to diff resolve_node on PARTIAL underscore needles, since the 7 canned queries all use exact slugs (verified) and never exercise the divergent tail.
- SEMANTICS #2 + MIGRATION-OPS #1 (major, MERGED — same root cause): recall floors are not an exact-parity oracle, and eval_graph.compare() literally SKIPS/returns-0 on any node_count/edge_count mismatch (verified lines 162-169). Reframed Phase 6: graph identity (counts + full present-node SET + relation→MIN(weight) map) is asserted HARD in graph_parity.py Gate 1, run on a representative corpus not just fixtures; eval-graph/eval-ask are demoted to floors, not the identity oracle the draft claimed.
- SEMANTICS #3 (minor, folded): the CTE orders by UNROUNDED total_weight (:185) and rounds only in projection (:174); the draft's Cypher + harness ordered/compared on the rounded value. Fixed the Cypher to ORDER BY unrounded max(tw) and round only in the returned column, and the harness to compare full-precision weight so a sub-4dp tie flip (possible once weights are retuned) is catchable.
- SEMANTICS #4 (minor, folded as a documented conscious decision, not a code fix): the CTE's /-delimited visited-string guard is subtly buggy for a node_id containing '/'; ACYCLIC is the correct guard. Added a build-time assert-no-'/'-in-node_id + a targeted parity fixture so ladybug's intended divergence (fixing the bug) is tested, not accidental. Judged genuinely latent (current entity_ids have no '/'), hence a precondition rather than a blocker.
- ARCHITECTURE #1 (blocker, folded): no shared generation stamp tied graph.lbdb to the index.sqlite beside it — the draft's only cross-store guard checked ladybug_version (library), not data. Added a shared graph_generation token (sha256 over sorted node/edge rows) written into index_meta + lbdb Meta + a pre-open sidecar, checked at every ladybug RO open; closes the partial-build skew and cross-machine sync skew in one mechanism.
- ARCHITECTURE #2 (major, folded partially): confirmed build_index rewrites index.sqlite IN PLACE (DROP TABLE at 517-542) vs the lbdb atomic tmp-swap — a torn cross-store snapshot for live readers. Primary mitigation is the generation-token gate (reader refuses to serve on mismatch) + document reopen-both; noted full atomic-staging of index.sqlite as optional hardening rather than mandating it (larger scope than the token needs).
- ARCHITECTURE #3 (major, folded): rewrote the AGENTS.md invariant #3 amendment to preserve BOTH clauses (single-file snapshot → directory-snapshotted-as-a-unit; and the NEW graph-vs-chunks cross-file agreement axis), instead of the draft's version that silently dropped the snapshot clause and only carried over vec/FTS agreement.
- ARCHITECTURE #4 (major, folded): added stage_sync/DATA_REMOTE (dvc push of data/, verified) to the blast radius. Decided to EXCLUDE graph.lbdb from the pushed artefact (local-cache, rebuilt from synced gold), since a pre-1.0 lbdb is not portable across wheels; the sidecar generation guard makes any stale pull actionable.
- ARCHITECTURE #5 (minor, folded): node_count/edge_count feed provenance footers (verified hybrid_search.py:110-111, graph_rag.py:135-136) and are written only by build_graph today; had build_graph_lbdb ALSO write them into index_meta so footers survive the Phase-7 CTE retirement.
- DEPENDENCY #1 (blocker, folded): offline fresh-machine install is a HARD requirement but the draft left the wheelhouse 'optional' — with no-build-package + cold cache that is an unrecoverable fail. Made a committed in-repo wheelhouse (Git LFS) MANDATORY with find-links, plus a make check for missing tags; PyPI hashes demoted to the online path.
- DEPENDENCY #2 (major, folded): documented that exact-pin + no-sdist couples the whole project's Python/OS upgrade freedom to the frozen 0.18.3 wheel matrix; added an opt-in ALLOW_LADYBUG_SDIST escape hatch and a re-pin+re-gate decision rule.
- DEPENDENCY #3 (major, folded): the draft's version guard read Meta AFTER ladybug.Database() open — a stale pre-1.0 format could fault before the friendly message. Moved the guard to a plain-text sidecar (graph.lbdb.meta.json) checked BEFORE the C++ opener; added a Phase-1 task to empirically confirm ladybug's actual open-time failure mode.
- DEPENDENCY #4 (major, folded): the CTE-retention hedge had no exit rule and a trigger ('1.0/format-stable') an archived-upstream fork may never hit. Added a concrete rule (3 consecutive all-gates-green releases + one survived format bump) and made permanent CTE retention the documented default if the fork never clears the bar, with the double-build cost recorded as the accepted price.
- DEPENDENCY #5 (minor, folded): no-build-package is uv-only; a pip/Dockerfile path reinstates the sdist compile. Added a cross-tool --only-binary=:all: CI/container policy + uv-only project invariant.
- MIGRATION-OPS #2 (major, folded): the draft's reset deleted the live graph.lbdb BEFORE the swap, opening a no-file window that watcher's 2s/1s rebuild loop (verified) maximizes for a live graph-serve reader. Reset now deletes only stale scaffolding (.tmp/.lock/leftover WAL); os.replace is the ONLY op retiring the old inode; tmp WAL named off the .tmp stem; POSIX-only atomic-swap guarantee + Windows PermissionError caveat documented.
- MIGRATION-OPS #3 (major, folded): Phase-5 dual-write had no guard, so an lbdb build error would fail the DEFAULT sqlite build (verified stage_index aborts before export_chunks/sync). Fenced build_graph_lbdb in try/except-and-continue while GRAPH_ENGINE != ladybug; only fatal after the flip.
- MIGRATION-OPS #4 (major, folded): after the Phase-7 flip the draft's hardcoded GRAPH_ENGINE=ladybug second CI run stopped testing the retained sqlite rollback path. Parameterized the second run as the NON-default engine (or a {sqlite,ladybug} matrix) so the Tier-1 rollback path stays green until the CTE is actually removed.
- MIGRATION-OPS #5 (minor, folded): corrected the inaccurate 'reuse the already-open DuckLake handle' phrasing — verified build_graph and build_index each open/close their own open_lake and leave no handle in stage_index; build_graph_lbdb now explicitly opens its own open_lake(paths) (a safe second read of committed gold).
- NO critique rejected as invalid: all four lenses were grounded in verifiable code and every finding was folded. The only partial-divergence from a suggested fix is ARCHITECTURE #2, where I adopt the generation-token gate as the primary mitigation and treat full atomic-staging of index.sqlite as optional rather than mandatory.
