# 26bmdc — 에이전트 가이드 (루트)

주요 서비스 세 갈래로 구성된 프로젝트다. 이 파일은 **지도와 영역 간 규약만** 담는다 —
각 영역의 세부 규칙은 그 영역의 문서가 지배하며, 여기에 반복하지 않는다.

| 영역 | 무엇 | 지배 문서 |
|---|---|---|
| `data-platform/` | 색인 코퍼스(한국 법안·법률 원문)와 파이프라인 | `data-platform/AGENTS.md` |
| `gen-docs/` | STO 증권신고서 생성 (산출물: `st_prospectus/<slug>/`) | `gen-docs/st_prospectus/sto-filing/SKILL.md` |
| `gen-apps/` | Security Token(RWA) 서비스 dApp 생성 (산출물: `gen-apps/<slug>/`) | 각 스킬의 SKILL.md |

## Skills

루트 스킬의 정본 위치는 `.agents/skills/`이고, **git이 추적하는 것은 이것뿐이다**
(개방 SKILL.md 표준 — Codex·Claude Code·Antigravity 공용. data-platform과 동일한 관습).
`.claude/skills/`는 Claude Code가 읽는 경로일 뿐 내용이 없어 **`make skills`로 만드는
생성물**이다 — 커밋하면 벤더 이름이 형상관리에 남고, `core.symlinks=false`로 클론한
Windows에서 링크가 경로 문자열이 담긴 텍스트 파일로 떨어져 스킬이 **오류 없이** 로드되지
않는다. 스킬은 `.agents/skills/`에서만 편집하고, 어댑터를 손으로 만들지 않는다.

| 스킬 | 소스 | 하는 일 |
|---|---|---|
| `sto-filing` | `gen-docs/st_prospectus/sto-filing/` | 증권 유형 판정 → 증권신고서 작성 → 자기심사 |
| `st-service-dapp` | `gen-apps/st-service-dapp/` | Q1~Q7(서비스 비전 + 운영 범위 + 발행 단위) 확정 → Scaffold-ETH 2 dApp 생성 |
| `filing-to-dapp` | `gen-apps/filing-to-dapp/` | 증권신고서 → dApp 브리지 (매핑 + 게이트 승계) |
| `corpus-lookup` | `.agents/skills/corpus-lookup/` | 루트 cwd에서 코퍼스를 조회하는 얇은 래퍼 |

data-platform 자체 스킬(corpus-search·corpus-graph 등)은 `data-platform/.agents/skills/`에 있고
**cwd=data-platform을 전제**한다. 루트에서 코퍼스가 필요하면 `corpus-lookup`(래퍼)을 쓴다.

## 영역 간 규약

- **공유 슬러그** — 증권신고서와 dApp은 같은 kebab-case ASCII 슬러그로 묶인다:
  `gen-docs/st_prospectus/<slug>/`(문서) ↔ `gen-apps/<slug>/`(dApp). 발행사+증권명에서 파생.
  **예약어**(슬러그로 금지): `st-service-dapp`, `filing-to-dapp`, `sto-filing`.
  이 슬러그는 data-platform의 node-id 슬러그(한글·언더스코어 유지)와 **별개 체계**다.
- **루트에서 코퍼스 조회** — `make -C data-platform query Q="..."`. 색인이 있어야 하며, 없으면
  `make -C data-platform fetch-index`(발행된 색인을 받아 설치, 약 1분)나 `build`(직접 인코딩,
  약 32분) 중 하나가 선행된다 — **둘 다 사용자 확인 후에** 돌린다(전자는 네트워크, 후자는 무겁다).
  검색 규칙·랭킹 해석의 정본은 `data-platform/AGENTS.md`와 corpus-search 스킬이다(여기 재서술 금지).
- **sto-filing 패키징** — 정본 1벌(`sto-filing/` + `prompt-templates/`)에서 단독 실행
  프롬프트 3종(`dist/`)을 생성한다. **소스 수정 후 반드시 `make prompts`**(= `python3 build_prompts.py`)로
  재생성하고, `dist/`는 손으로 고치지 않는다. 절차는 `gen-docs/st_prospectus/PACKAGING.md`.
- **clone 후 한 번 `make quickstart`** — 훅·스킬을 세우고 코퍼스를 질의 가능 상태까지
  올린다(약 88초. 색인을 직접 빌드하면 약 32분이고, quickstart는 발행본을 받아 설치한다).
  전부 멱등이라 다시 돌려도 안전하다. 훅·스킬만 필요하면 `make hooks && make skills`다 —
  둘 다 git이 clone에 실어 보내지 않는
  것을 각 사본에서 세우는 일이다. `make hooks`는 `core.hooksPath`를 가리켜 커밋 시
  `make check`가 돌게 하고, `make skills`는 `.agents/skills/`에서 벤더 어댑터
  (`.claude/skills/`)를 만든다. 어댑터가 없으면 `make check`는 **막지 않고 알려만 준다** —
  없는 것은 규약 위반이 아니라 아직 안 만든 상태이고, 여기서 막으면 clone 직후 첫 커밋이
  불가능해진다. 있는데 정본과 어긋나면 실패다.
- **커밋 전에 `make check`** (루트, 수 초). `ruff` 린트 + 스킬 프론트매터를 **YAML 파서로**
  검증하고(`name`↔디렉터리 일치 포함) `dist/` 최신성을 본다. 자동수정은 `make fmt`. 이 둘은 어느 영역에도 속하지 않아
  지금까지 게이트가 없던 자리다. 전체 게이트는 `make verify`(data-platform 빌드 포함).
