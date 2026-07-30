.DEFAULT_GOAL := help
SHELL := /bin/bash
UV := uv
PY := $(UV) run python
# The extras this deployment uses (onnx_int8 embedder, KIWI_MORPH keyword
# morphology, scrapling web fetch). ONE list on purpose: `uv sync` REMOVES
# packages that are not requested, so any target that syncs with a shorter list
# silently uninstalls the extras the .env-configured provider needs. That is not
# hypothetical -- reset gained these extras first, setup caught up a commit
# later (69af01e), and verify's bare sync kept stripping them until it was
# pointed at this variable. Keep in step with .env.
CPU_EXTRAS := --extra onnx-embed --extra kiwi --extra web

# The GPU axis then re-introduced that exact trap, because the accelerator
# extras REPLACE onnx-embed (they are the same onnxruntime module, declared in
# `[tool.uv] conflicts`) while only `setup-gpu` knew about them. `reset` and
# `verify` kept expanding the CPU list, so every gate uninstalled the wheel
# `setup-gpu` had installed -- and then re-encoded all 12,643 vectors inside the
# gate, because the vector cache is keyed on the provider.
#
# So the list is a function of THE MACHINE, not of the target: `make setup-gpu`
# records the choice in `.gpu-stack` (git-ignored) only AFTER proving the wheel
# actually landed, and every target expands the same SYNC_EXTRAS.
#   make setup-gpu STACK=cuda        Linux/Windows + NVIDIA
#   make setup-gpu STACK=directml    Windows, any DX12 GPU (NVIDIA/AMD/Intel)
#   make setup-gpu STACK=qnn         Windows on ARM (Snapdragon X NPU) -- UNVERIFIED
# The first `make verify` after a successful setup-gpu re-encodes the corpus once
# (~24 min) as the cache flips from CPU-signed to GPU-signed. That is scheduled
# here rather than discovered inside a gate.
GPU ?= $(shell cat .gpu-stack 2>/dev/null)
ifeq ($(strip $(GPU)),)
SYNC_EXTRAS := $(CPU_EXTRAS)
else
SYNC_EXTRAS := --extra gpu-$(strip $(GPU)) --extra kiwi --extra web
endif

.PHONY: help quickstart corpus-id snapshot restore chunk-ceiling index-canonical fetch-index warm-cache publish-index setup setup-gpu gpu-probe bench-ep bench-concurrent ep-equiv warm-models reset seed ingest transform index build sync watch smoke query ask collections impact graph graph-serve clean clean-index verify sync-skills export gate triage dupes dupes-twins dupes-controls eval eval-baseline eval-graph eval-graph-baseline eval-ask eval-ask-baseline warm-rerank eval-rerank eval-rerank-baseline ocr-compare

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

quickstart: ## Clone -> queryable -> ready to add documents, in one command
	# The five steps a fresh clone needs, in the only order that works. Each is
	# idempotent, so re-running this is cheap and safe.
	@echo "== 1/5 dependencies =="
	@$(MAKE) setup
	@echo
	@echo "== 2/5 .env: the published index is refused unless the embedder matches =="
	# The default is `hashing` on purpose (no model download, so `make build` works
	# offline), which is exactly why a fresh clone cannot install the published
	# index until this runs. An existing .env is reported on, never overwritten.
	@$(PY) tools/bootstrap_env.py
	@echo
	@echo "== 3/5 embedder (one-time online fetch) =="
	@$(MAKE) warm-models
	@echo
	@echo "== 4/5 index =="
	# A fork that has never published has nothing to fetch, and steps 4 and 5 would
	# then fail in sequence with messages pointing at each other. The fallback is a
	# build, and saying so is the whole reason this target exists.
	@if [ ! -f index_release.json ]; then \
	  echo "[quickstart] no index_release.json: no index has been published for this repo."; \
	  echo "[quickstart] Encode one here instead:"; \
	  echo "[quickstart]     make build        # ~32 min, no network after setup"; \
	  echo "[quickstart] Everything else is ready; skipping steps 4 and 5."; \
	  exit 0; \
	fi; \
	$(MAKE) fetch-index && \
	echo && \
	echo "== 5/5 vector cache, so adding a document does not re-encode the corpus ==" && \
	$(PY) tools/warm_cache.py --if-missing && \
	echo && \
	echo "Ready. Ask the corpus something:" && \
	echo "    make query Q=\"예치금 분리보관 의무\"" && \
	echo "    make ask   Q=\"스테이블코인 발행자 준비자산\"" && \
	echo && \
	echo "To add a document: drop it in source/<collection>/, add its row to" && \
	echo "source/CORPUS_MANIFEST.tsv (sha256 + bytes + where it came from), then" && \
	echo "\`make build\` -- only the new chunks are encoded."

setup: ## Resolve dependencies (incl. configured extras) and install Meltano plugins
	$(UV) sync $(SYNC_EXTRAS)
	$(UV) run meltano install

setup-gpu: ## Install an accelerator wheel. Usage: make setup-gpu STACK=cuda|directml|qnn (macOS needs none)
	# STACK, not GPU: GPU is now seeded from .gpu-stack, so reusing it as the
	# argument would make the request indistinguishable from the recorded state.
	@test -n "$(STACK)" || (echo "usage: make setup-gpu STACK=cuda|directml|qnn   (macOS: CoreML ships in the default wheel; just run make gpu-probe)" && exit 1)
	@case "$(STACK)" in cuda|directml|qnn) ;; *) echo "STACK must be cuda, directml or qnn; got '$(STACK)'" && exit 1 ;; esac
	# The wheels are marker-gated, so an impossible pairing does NOT fail to
	# resolve -- it resolves to nothing and leaves the venv with no onnxruntime at
	# all while exiting 0. Measured on this Mac: a dry-run sync of the gpu-cuda
	# extra exits 0 and reports "Would uninstall onnxruntime". So the sync is
	# only kept if a wheel PROVABLY landed; otherwise the CPU stack is restored
	# and .gpu-stack is not written.
	$(UV) sync --extra gpu-$(STACK) --extra kiwi --extra web
	@if $(PY) -c "from pipeline import runtime; import sys; sys.exit(0 if 'onnxruntime-$(STACK)' in runtime.installed_wheel() or ('$(STACK)' == 'cuda' and 'onnxruntime-gpu' in runtime.installed_wheel()) else 1)" 2>/dev/null; then \
	  echo "$(STACK)" > .gpu-stack; \
	  echo "[setup-gpu] recorded .gpu-stack=$(STACK); every target now syncs this stack."; \
	  echo "[setup-gpu] NOTE: the next \`make verify\` re-encodes the corpus once (~24 min) as the vector cache flips from CPU-signed to GPU-signed."; \
	else \
	  echo "[setup-gpu] FAIL: no onnxruntime-$(STACK) wheel landed on this platform ($$($(PY) -c 'from pipeline import runtime; print(runtime.installed_wheel())'))."; \
	  echo "[setup-gpu] restoring the CPU stack; .gpu-stack was NOT written."; \
	  $(UV) sync $(CPU_EXTRAS); \
	  exit 1; \
	fi
	$(UV) run meltano install
	@echo
	@echo "== does it actually work? =="
	$(PY) -m pipeline.runtime --save

corpus-id: ## Which documents is this? A hash over the manifest, refused if it disagrees with the files
	@python3 tools/corpus_id.py

gpu-probe: ## What execution provider will this machine use, and would fp16 help? Writes the device profile.
	$(PY) -m pipeline.runtime --save

bench-ep: ## Measured throughput per execution provider x asset. Decides GPU-or-not with numbers, not guesses.
	$(PY) tools/bench_ep.py

ep-equiv: ## Cross-hardware contract: same asset on two providers must agree (skips on a CPU-only box)
	$(PY) tools/check_ep_equivalence.py

bench-concurrent: ## Do two stages running at once help, or starve each other? Measures the interference before enabling it
	RERANK=1 $(PY) tools/bench_concurrency.py

warm-models: ## One-time online fetch of the configured embedder asset (and the reranker when RERANK=1)
	$(PY) -c "from pipeline import get_settings; from pipeline.build_rag import describe_embedder, get_embedder; s=get_settings(); print('[warm] ' + describe_embedder(get_embedder(s, allow_download=True)))"
	@if [ "$$RERANK" = "1" ]; then $(MAKE) warm-rerank; fi

reset: ## Rebuild every regenerable artefact after a checkout move/rename: venv, Meltano plugins, data plane. The venv and .meltano hardcode absolute paths that no project code controls, so a rename strands them; this recreates them. The DuckLake DATA_PATH is already relative and survives on its own.
	rm -rf .venv .meltano
	$(UV) sync $(SYNC_EXTRAS)
	$(UV) run meltano install
	$(MAKE) clean
	$(MAKE) build

seed: ## Materialise tracked seeds (source/ corpus + pipeline/fixtures) into data/inbox
	$(PY) -c "from pipeline import get_paths; from pipeline.watcher import seed_inbox; p=get_paths().ensure(); n=seed_inbox(p); print(f'[seed] {n} new file(s) -> {p.inbox}')"

ingest: ## Promote data/inbox -> data/raw, then Meltano EL into DuckLake
	$(PY) pipeline/run.py --only ingest

transform: ## SQLMesh bronze -> silver -> gold, with blocking audits
	$(PY) pipeline/run.py --only transform

index: ## Build data/serving/index.sqlite (vectors + FTS5 + graph)
	$(PY) pipeline/run.py --only index

SNAPSHOT ?= .snapshot

snapshot: ## Save data/serving + data/processed before an expensive rebuild (APFS clone: instant, no extra disk)
	# A chunk-size experiment costs a rebuild to run and ANOTHER to undo. Cloning
	# the state first makes the undo free -- measured 0.045 s each way against the
	# ~105 min a rebuild-to-restore cost, which is the difference between an
	# experiment being worth running and not.
	@rm -rf "$(SNAPSHOT)" && mkdir -p "$(SNAPSHOT)"
	@cp -Rc data/serving data/processed "$(SNAPSHOT)"/ 2>/dev/null \
	  || { echo "[snapshot] no clonefile on this filesystem; copying"; cp -R data/serving data/processed "$(SNAPSHOT)"/; }
	@echo "[snapshot] saved to $(SNAPSHOT) -- restore with \`make restore\`"

restore: ## Put back the state saved by `make snapshot`
	@test -d "$(SNAPSHOT)/serving" || (echo "no snapshot at $(SNAPSHOT); nothing to restore" && exit 1)
	@rm -rf data/serving data/processed
	@cp -Rc "$(SNAPSHOT)"/serving "$(SNAPSHOT)"/processed data/ 2>/dev/null \
	  || cp -R "$(SNAPSHOT)"/serving "$(SNAPSHOT)"/processed data/
	@echo "[restore] state restored. Re-run 'make eval' to confirm it reproduces."

chunk-ceiling: ## Largest MAX_CHUNK_CHARS that truncates nothing, computed from the token distribution (no rebuild)
	$(PY) tools/chunk_ceiling.py

index-canonical: ## Rebuild the index with EVERY vector re-encoded. Required before recording an eval baseline.
	# The graph is dynamically quantised, so a cached vector was encoded beside
	# different neighbours than a cold build would give it (cosine 0.9904, enough
	# to move R@10 by 0.15). An incrementally-built index is fine to QUERY and
	# wrong to record a floor from.
	rm -f data/processed/vector_cache.sqlite
	$(PY) pipeline/run.py --only index

eval: ## Retrieval regression floor: P@1 / R@5 / MRR@10 per arm and per query kind
	$(PY) -m pipeline.eval_retrieval

eval-baseline: ## Record the current retrieval numbers as the floor
	$(PY) -m pipeline.eval_retrieval --record-baseline

warm-rerank: ## One-time online fetch of the cross-encoder reranker model (opt-in; the read path never downloads)
	RERANK=1 $(PY) -c "from pipeline import get_settings; from pipeline.reranker import get_reranker; get_reranker(get_settings(), allow_download=True); print('[warm-rerank] reranker model cached')"

eval-rerank: ## OPT-IN reranker floor: fused vs reranked per kind (needs warm-rerank). Default `make verify` does NOT cover this.
	RERANK=1 $(PY) -m pipeline.eval_retrieval --assert-baseline

eval-rerank-baseline: ## Record the reranked arm/per-kind numbers as the opt-in floor
	RERANK=1 $(PY) -m pipeline.eval_retrieval --record-baseline

eval-graph: ## Graph-reachability floor: reach / p@1 / mrr / path_strength per judgment kind
	$(PY) -m pipeline.eval_graph

eval-graph-baseline: ## Record the current graph-reachability numbers as the floor
	$(PY) -m pipeline.eval_graph --record-baseline

eval-ask: ## Related-section floor for make ask: does the graph arm surface the connected article
	$(PY) -m pipeline.eval_graph_rag

eval-ask-baseline: ## Record the current related-section numbers as the floor
	$(PY) -m pipeline.eval_graph_rag --record-baseline

gate: ## Acceptance gate for document-type profiles
	$(PY) -m pipeline.doctypes.gate

triage: ## Report which profile claims each document in data/raw
	$(PY) -m pipeline.doctypes.gate --report

dupes: ## Report content-duplicate renditions collapsed by the fingerprint dedup
	$(PY) -m pipeline.report_dupes

dupes-twins: ## Do source/ twin renditions (X.hwp beside X.pdf) share a fingerprint? Reads no lake state
	$(PY) -m pipeline.report_dupes --twins

dupes-controls: ## Fuzzy-twin detector controls: 10 positives caught, 정정본·타문서·모법/시행령 rejected
	$(PY) -m pipeline.report_dupes --controls

export: ## Rewrite data/processed/chunks/*.json from the gold layer
	$(PY) -m pipeline.export

sync: ## Push the data plane to DATA_REMOTE (no-op when local_only)
	$(PY) pipeline/run.py --only sync

fetch-index: ## Install the published index instead of encoding one (~92 MB against ~32 min), verified against index_release.json
	# Explicitly a target and never part of `build` or a query: invariant 7 keeps
	# the read path offline, and this reaches the network. It still needs the
	# embedder for the signature check, so a fresh clone runs `make warm-models`
	# first -- the 32 minutes become ~2, not zero.
	$(PY) tools/index_release.py fetch

warm-cache: ## Rebuild the vector cache FROM the installed index, so a later build re-encodes only what changed
	# No second download: the vectors are already in `chunks_vec` and the cache key
	# is a pure function of columns `chunks` holds [M:cache-from-index]. Only from a
	# published index -- a locally built one does not record which provider encoded it.
	$(PY) tools/warm_cache.py

publish-index: ## Upload data/serving/index.sqlite as a release asset. Prints a dry run unless YES=1
	# Refuses an incrementally-built index, an unverified corpus, uncommitted
	# pipeline/ or source/, a reused tag, or a failing eval floor. See the tool.
	$(PY) tools/index_release.py publish $(if $(YES),--yes,)

build: ## Full local pipeline: ingest -> transform -> index -> sync, then smoke test
	$(PY) pipeline/run.py
	$(PY) pipeline/smoke_test.py

watch: ## Watch data/inbox/documents and rebuild on every change
	$(PY) pipeline/watcher.py --watch

smoke: ## Assert hybrid_search and graph_query work against the built index
	$(PY) pipeline/smoke_test.py

query: ## Hybrid search. Usage: make query Q="<query>" [COLLECTION=sto]
	$(PY) -m agent.tools.hybrid_search "$(Q)" $(if $(COLLECTION),--collection $(COLLECTION),)

ask: ## Hybrid search + graph-related context. Usage: make ask Q="<query>" [DEPTH=1] [COLLECTION=sto]
	$(PY) -m agent.tools.graph_rag "$(Q)" $(if $(DEPTH),--depth $(DEPTH),) $(if $(COLLECTION),--collection $(COLLECTION),)

collections: ## List collections (inbox folders) in the index with counts
	$(PY) -m agent.tools.hybrid_search --list-collections

impact: ## Blast radius. Usage: make impact NODE=vector_store
	$(PY) -m agent.tools.graph_query "$(NODE)" --direction upstream

graph: ## Render the corpus graph to a standalone offline HTML (data/serving/graph.html). Usage: make graph [SOURCE=lake|index]
	$(PY) tools/viz/graph.py $(if $(SOURCE),--source $(SOURCE),)

graph-serve: ## Live graph viewer in the browser: auto-refresh + DB picker. Usage: make graph-serve [PORT=8777]
	$(PY) tools/viz/server.py $(if $(PORT),--port $(PORT),)

sync-skills: ## Build .claude/skills from .agents/skills (symlinks; copies where symlinks are unavailable)
	# One implementation, not a platform fork. The old target here always COPIED
	# and `rm -rf`d the directory first, so a macOS user who ran it silently traded
	# a mirror that cannot drift for two trees that can. tools/sync_skills.py picks
	# symlinks when the OS allows them and falls back to copying when it does not,
	# and `--check` catches a copy that has since drifted.
	@$(PY) tools/sync_skills.py .

clean-index: ## Delete only the serving index
	rm -f data/serving/index.sqlite

clean: ## Delete every regenerable artefact. data/raw and data/inbox are untouched.
	rm -rf data/processed data/serving .meltano/run .meltano/logs
	@echo "cleaned. data/raw and data/inbox are immutable and were left alone."

# The evasion-token pattern is assembled at runtime rather than written out, so
# this Makefile does not match its own check. `[.]{3}` is the same regex as the
# literal three-dot form.
ocr-compare: ## Do the paddle and onnx OCR backends read a page the same way? Usage: make ocr-compare FILE=scan.pdf
	@test -n "$(FILE)" || (echo "usage: make ocr-compare FILE=<scan.pdf|page.png>" && exit 1)
	$(PY) tools/ocr/compare_backends.py "$(FILE)"

verify: ## Run the full verification gate
	@echo "== cheap invariants (no venv, no network, no hardware) =="
	# The same checks the root gate and CI run. They lived only in root `make
	# check`, so the gate AGENTS.md calls the full one did not run them.
	@python3 tools/check_seam.py
	@python3 tools/check_retired_numbers.py
	@python3 tools/check_knobs.py
	@python3 tools/check_sync_lists.py
	@python3 tools/check_lock_pin.py
	@python3 tools/index_release.py check
	@python3 tools/test_index_release.py
	@echo "== accelerator layer contract (no GPU, no model, no index needed) =="
	# The only thing that ran this before was the CI workflow, so `make verify` went
	# green through the EP report, the doctype gate, a full build and three eval
	# floors while the twelve invariants it all rests on were never checked locally.
	$(PY) tools/test_runtime_contract.py
	@echo "== no evasion tokens =="
	@python3 tools/check_evasion.py
	@echo "== tuning values cite a measurement, measurements name a corpus =="
	@python3 tools/check_citations.py
	@echo "== does the derived chunk ceiling still fit this corpus? =="
	$(PY) tools/check_chunk_fit.py
	@echo "== document-type profile gate =="
	$(PY) -m pipeline.doctypes.gate
	@echo "== toolchain =="
	$(UV) sync $(SYNC_EXTRAS)
	$(UV) run meltano --version && $(UV) run sqlmesh --version
	@echo "== execution provider and asset =="
	# AFTER the toolchain sync, not before: `--strict` fails on an install fault,
	# and "onnxruntime registered nothing" is also what a not-yet-synced tree looks
	# like. Running it first would make a fresh clone un-verifiable.
	$(PY) -m pipeline.runtime --save --strict
	@echo "== fuzzy twin-detector controls =="
	# Before the build, not after: it reads source/ and the model file only (no
	# lake), so a detector that started merging non-twins fails in seconds
	# instead of after a full rebuild. After the toolchain sync, because it
	# extracts PDFs and so needs the synced deps.
	$(PY) -m pipeline.report_dupes --controls
	@echo "== build =="
	$(MAKE) build
	@test -f data/serving/index.sqlite || (echo "FAIL: no index.sqlite" && exit 1)
	@echo "== can a cache still be derived from an index? (embed_text_of vs gold/chunks.sql) =="
	# AFTER the build, because it asks the lake. The recomputation in warm_cache.py is
	# a copy of a SQL expression, and a copy that drifts fails SILENTLY -- every key
	# changes, every lookup misses, the build just gets slow again.
	$(PY) tools/warm_cache.py --verify-against-lake
	@echo "== cross-hardware provider contract (skips on a CPU-only box) =="
	# AFTER the build, not before: `make clean` deletes data/serving, so running this
	# first meant that on a clean tree it always measured its 5-sentence fallback
	# corpus -- where "identical top-10" over 5 documents passes unconditionally.
	$(PY) tools/check_ep_equivalence.py
	@echo "== retrieval regression floor =="
	$(PY) -m pipeline.eval_retrieval --assert-baseline
	@echo "== graph-reachability floor =="
	$(PY) -m pipeline.eval_graph --assert-baseline
	@echo "== related-section floor (make ask) =="
	$(PY) -m pipeline.eval_graph_rag --assert-baseline
	@echo "== data plane is git-ignored =="
	@git status --porcelain data/ | grep . && (echo "FAIL: data staged" && exit 1) || echo "OK: data ignored"
	@echo "== agent guides =="
	@test -f AGENTS.md && test -f CLAUDE.md && echo "OK"
	@echo "== skill frontmatter (Agent Skills standard) =="
	$(PY) tools/check_skills.py .agents/skills
	@echo "== the vendor adapter agrees with the canon (.claude/skills/ is generated) =="
	# The loop this replaces compared SKILL.md with `cmp` against a directory that
	# is now git-ignored, so on a fresh clone it did not report "not built yet" --
	# it reported "differs from the source" and failed the whole gate. Absent is
	# not a violation; present-and-wrong is.
	@$(PY) tools/sync_skills.py --check .
