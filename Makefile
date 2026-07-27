# 26bmdc — 저장소 전체 검증
#
# 각 영역은 자기 게이트를 갖는다: data-platform 은 `make -C data-platform verify`(빌드·
# 회귀 바닥까지 도는 무거운 게이트), gen-docs·gen-apps 는 문서와 스킬이라 빌드가 없다.
# 그래서 **루트에 걸칠 것만** 여기 둔다 — 어느 영역에도 속하지 않아 지금까지 아무도
# 검사하지 않던 것들이다(루트 스킬 4개, dist/ 최신성).
#
#   make check    빠르다(수 초). 커밋 전에 돌린다.
#   make verify   check + data-platform 전체 게이트. 느리다(빌드 포함).

PY ?= python3
SKILL_CHECK := data-platform/tools/check_skills.py
# 버전을 박아 둔다 — ruff 는 기본 규칙셋이 릴리스마다 바뀌어서, 핀이 없으면 어제 통과한
# 코드가 오늘 실패한다. 올릴 때는 올리고 나서 한 번 돌려보고 커밋한다.
RUFF := uvx ruff@0.16.0

.PHONY: help check lint fmt verify prompts

help: ## 이 목록
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

check: lint ## 루트에 걸친 것만 빠르게 (린트 + 스킬 프론트매터 + dist 최신성 + 하드웨어 seam)
	@echo "== hardware seam (pipeline/ 은 runtime.py 를 통해서만 가속기를 안다) =="
	@$(PY) data-platform/tools/check_seam.py
	@echo "== skill frontmatter (루트 + data-platform) =="
	@$(PY) $(SKILL_CHECK) .agents/skills data-platform/.agents/skills
	@echo "== sto-filing dist/ 가 스킬 정본과 일치하는가 =="
	@cd gen-docs/st_prospectus && $(PY) build_prompts.py --check

lint: ## ruff (설정·제외 대상은 ruff.toml)
	@echo "== ruff =="
	@$(RUFF) check .

fmt: ## ruff 자동수정 (import 정렬·현대화 등). 의미를 바꾸는 것은 손대지 않는다
	@$(RUFF) check --fix .

verify: check ## check + data-platform 전체 게이트 (빌드 포함, 느림)
	@echo
	@$(MAKE) -C data-platform verify

prompts: ## sto-filing dist/ 재생성 (references 를 고쳤으면)
	@cd gen-docs/st_prospectus && $(PY) build_prompts.py
