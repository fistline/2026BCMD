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
SYNC_EXTRAS := --extra onnx-embed --extra kiwi --extra web

# GPU execution provider for this machine, chosen ONCE here (see `make setup-gpu`).
# The onnxruntime wheels are mutually exclusive -- gpu-cuda / gpu-directml REPLACE
# onnx-embed rather than adding to it, which is why this is a substitution and not
# another `--extra`. macOS needs nothing: CoreML ships in the default wheel.
#   make setup-gpu GPU=cuda        Linux/Windows + NVIDIA
#   make setup-gpu GPU=directml    Windows, any DX12 GPU (NVIDIA/AMD/Intel)
#   make setup-gpu GPU=qnn         Windows on ARM (Snapdragon X NPU) -- UNVERIFIED, see runtime.py
GPU ?=
ifeq ($(GPU),)
GPU_SYNC_EXTRAS := $(SYNC_EXTRAS)
else
GPU_SYNC_EXTRAS := --extra gpu-$(GPU) --extra kiwi --extra web
endif

.PHONY: help setup setup-gpu gpu-probe bench-ep bench-concurrent ep-equiv warm-models reset seed ingest transform index build sync watch smoke query ask collections impact graph graph-serve clean clean-index verify sync-skills export gate triage dupes dupes-twins dupes-controls eval eval-baseline eval-graph eval-graph-baseline eval-ask eval-ask-baseline warm-rerank eval-rerank eval-rerank-baseline ocr-compare

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Resolve dependencies (incl. configured extras) and install Meltano plugins
	$(UV) sync $(SYNC_EXTRAS)
	$(UV) run meltano install

setup-gpu: ## Install an accelerator wheel. Usage: make setup-gpu GPU=cuda|directml|qnn (macOS needs none)
	@test -n "$(GPU)" || (echo "usage: make setup-gpu GPU=cuda|directml|qnn   (macOS: CoreML ships in the default wheel; just run make gpu-probe)" && exit 1)
	$(UV) sync $(GPU_SYNC_EXTRAS)
	$(UV) run meltano install
	@echo
	@echo "== does it actually work? =="
	$(PY) -m pipeline.runtime --save

gpu-probe: ## What execution provider will this machine use, and would fp16 help? Writes the device profile.
	$(PY) -m pipeline.runtime --save

bench-ep: ## Measured throughput per execution provider x asset. Decides GPU-or-not with numbers, not guesses.
	$(PY) tools/bench_ep.py

ep-equiv: ## Cross-hardware contract: same asset on two providers must agree (skips on a CPU-only box)
	$(PY) tools/check_ep_equivalence.py

bench-concurrent: ## Do two stages running at once help, or starve each other? Measures the interference before enabling it
	RERANK=1 $(PY) tools/bench_concurrency.py

warm-models: ## One-time online fetch of the configured embedder asset (and the reranker when RERANK=1)
	$(PY) -c "from pipeline import get_settings; from pipeline.build_rag import get_embedder; s=get_settings(); e=get_embedder(s, allow_download=True); print(f'[warm] embedder {e.name}/{e.precision} on {e.provider} cached')"
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

sync-skills: ## Windows fallback: copy .agents/skills -> .claude/skills (symlinks are the default)
	@rm -rf .claude/skills
	@mkdir -p .claude/skills
	@cp -R .agents/skills/. .claude/skills/
	@echo "copied .agents/skills -> .claude/skills. Re-run after editing any skill."
	@echo "On macOS/Linux prefer symlinks instead, which never drift:"
	@echo "  rm -rf .claude/skills && mkdir -p .claude/skills"
	@echo "  for d in .agents/skills/*/; do ln -s \"../../\$$d\" \".claude/skills/\$$(basename \$$d)\"; done"

clean-index: ## Delete only the serving index
	rm -f data/serving/index.sqlite

clean: ## Delete every regenerable artefact. data/raw and data/inbox are untouched.
	rm -rf data/processed data/serving .meltano/run .meltano/logs
	@echo "cleaned. data/raw and data/inbox are immutable and were left alone."

# The evasion-token pattern is assembled at runtime rather than written out, so
# this Makefile does not match its own check. `[.]{3}` is the same regex as the
# literal three-dot form.
EVASION_PATTERN = $(shell printf 'TO%s|FI%s|%s for brevity|[.]{3}' DO XME omitted)

ocr-compare: ## Do the paddle and onnx OCR backends read a page the same way? Usage: make ocr-compare FILE=scan.pdf
	@test -n "$(FILE)" || (echo "usage: make ocr-compare FILE=<scan.pdf|page.png>" && exit 1)
	$(PY) tools/ocr/compare_backends.py "$(FILE)"

verify: ## Run the full verification gate
	@echo "== execution provider and asset =="
	$(PY) -m pipeline.runtime --save
	@echo "== cross-hardware provider contract (skips on a CPU-only box) =="
	$(PY) tools/check_ep_equivalence.py
	@echo "== no evasion tokens =="
	@! grep -rInE '$(EVASION_PATTERN)' pipeline transform agent .agents Makefile meltano.yml || (echo "FAIL: evasion token found" && exit 1)
	@echo "OK"
	@echo "== document-type profile gate =="
	$(PY) -m pipeline.doctypes.gate
	@echo "== toolchain =="
	$(UV) sync $(SYNC_EXTRAS)
	$(UV) run meltano --version && $(UV) run sqlmesh --version
	@echo "== fuzzy twin-detector controls =="
	# Before the build, not after: it reads source/ and the model file only (no
	# lake), so a detector that started merging non-twins fails in seconds
	# instead of after a full rebuild. After the toolchain sync, because it
	# extracts PDFs and so needs the synced deps.
	$(PY) -m pipeline.report_dupes --controls
	@echo "== build =="
	$(MAKE) build
	@test -f data/serving/index.sqlite || (echo "FAIL: no index.sqlite" && exit 1)
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
	@echo "== Claude Code sees the same skills =="
	@for d in .agents/skills/*/; do n=$$(basename "$$d"); cmp -s "$$d/SKILL.md" ".claude/skills/$$n/SKILL.md" || { echo "FAIL: .claude/skills/$$n differs from the source. Run: make sync-skills"; exit 1; }; done; echo OK
