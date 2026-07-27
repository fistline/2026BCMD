# 26bmdc — 에이전트 가이드 (루트)

세 영역으로 구성된 프로젝트다. 이 파일은 **지도와 영역 간 규약만** 담는다 —
각 영역의 세부 규칙은 그 영역의 문서가 지배하며, 여기에 반복하지 않는다.

| 영역 | 무엇 | 지배 문서 |
|---|---|---|
| `data-platform/` | 색인 코퍼스(한국 법안·법률 원문)와 파이프라인 | `data-platform/AGENTS.md` |
| `gen-docs/` | STO 증권신고서 생성 (산출물: `st_prospectus/<slug>/`) | `gen-docs/st_prospectus/sto-filing/SKILL.md` |
| `gen-apps/` | Security Token(RWA) 서비스 dApp 생성 (산출물: `gen-apps/<slug>/`) | 각 스킬의 SKILL.md |

## Skills

루트 스킬의 정본 위치는 `.agents/skills/`이고 `.claude/skills/`가 같은 대상을 미러한다
(개방 SKILL.md 표준 — Codex·Claude Code·Antigravity 공용. data-platform과 동일한 관습).

| 스킬 | 소스 | 하는 일 |
|---|---|---|
| `sto-filing` | `gen-docs/st_prospectus/sto-filing/` | 증권 유형 판정 → 증권신고서 작성 → 자기심사 |
| `st-service-dapp` | `gen-apps/st-service-dapp/` | Q1~Q5 확정 → Scaffold-ETH 2 dApp 생성 |
| `filing-to-dapp` | `gen-apps/filing-to-dapp/` | 증권신고서 → dApp 브리지 (매핑 + 게이트 승계) |
| `corpus-lookup` | `.agents/skills/corpus-lookup/` | 루트 cwd에서 코퍼스를 조회하는 얇은 래퍼 |

data-platform 자체 스킬(corpus-search·corpus-graph 등)은 `data-platform/.agents/skills/`에 있고
**cwd=data-platform을 전제**한다. 루트에서 코퍼스가 필요하면 `corpus-lookup`(래퍼)을 쓴다.

## 영역 간 규약

- **공유 슬러그** — 증권신고서와 dApp은 같은 kebab-case ASCII 슬러그로 묶인다:
  `gen-docs/st_prospectus/<slug>/`(문서) ↔ `gen-apps/<slug>/`(dApp). 발행사+증권명에서 파생.
  **예약어**(슬러그로 금지): `st-service-dapp`, `filing-to-dapp`, `sto-filing`.
  이 슬러그는 data-platform의 node-id 슬러그(한글·언더스코어 유지)와 **별개 체계**다.
- **루트에서 코퍼스 조회** — `make -C data-platform query Q="..."`. 색인이 빌드돼 있어야 하며,
  검색 규칙·랭킹 해석의 정본은 `data-platform/AGENTS.md`와 corpus-search 스킬이다(여기 재서술 금지).
- **sto-filing 패키징** — 정본 1벌(`sto-filing/` + `prompt-templates/`)에서 단독 실행
  프롬프트 3종(`dist/`)을 생성한다. **소스 수정 후 반드시 `make prompts`**(= `python3 build_prompts.py`)로
  재생성하고, `dist/`는 손으로 고치지 않는다. 절차는 `gen-docs/st_prospectus/PACKAGING.md`.
- **커밋 전에 `make check`** (루트, 수 초). `ruff` 린트 + 스킬 프론트매터를 **YAML 파서로**
  검증하고(`name`↔디렉터리 일치 포함) `dist/` 최신성을 본다. 자동수정은 `make fmt`. 이 둘은 어느 영역에도 속하지 않아
  지금까지 게이트가 없던 자리다. 전체 게이트는 `make verify`(data-platform 빌드 포함).
