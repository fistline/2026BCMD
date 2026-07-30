# 26bmdc — 토큰증권 담당자를 위한 업무환경

2026년 토큰증권 제도 시행으로 조각투자·STO 발행 경로가 열린 지금, 이 저장소는
**그 규제를 다루는 개발자·기획자**가 법령 근거 조회 → 증권신고서 초안 작성 →
프로토타입 dApp 연결을 하나의 흐름으로 해내도록 돕습니다.

> 약어: **STO**(Security Token Offering, 토큰증권 공모) · **RWA**(Real-World Asset,
> 실물연계자산) · **dApp**(탈중앙화 애플리케이션) · **DART**(금융감독원 전자공시시스템) ·
> **혁신금융서비스**(금융규제 샌드박스).
>
> 기술 용어: **코퍼스**(검색용으로 색인해 둔 문서 모음) · **색인/인덱스**(빨리 찾도록
> 미리 정리해 둔 데이터) · **하이브리드 검색**(뜻이 비슷한 문서를 찾는 *벡터* 검색과
> 정확한 단어를 찾는 *키워드* 검색을 합친 방식) · **의존 그래프**(법안이 어떤 법률에
> 위임·참조하는지 나타낸 문서 사이의 관계망) · **슬러그**(파일·폴더 이름에 쓰는 짧은 영문 식별자).

![26bmdc 전체 개요 — 토큰증권 담당자를 위한 업무환경](docs/overview.svg)

주요 서비스는 **독립적으로도** 쓰이고, 하나로 이으면 "규제 지식 → 공시 문서 →
배포 가능한 서비스"가 한 흐름이 됩니다.

**이 저장소는 AI 에이전트로 구동됩니다.** `data-platform`은 `make` CLI를 제공하지만,
`gen-docs`·`gen-apps`는 **Agent Skill**로 동작합니다 — 사람이 직접 실행하는 CLI가 아니라,
에이전트(Claude Code·Codex·Antigravity)가 요청 내용에 맞게 자동으로 불러오는 절차
묶음입니다. 즉 사용자는 에이전트에게 자연어로 요청하고, 에이전트가 스킬을 로드해 문서나
dApp을 만듭니다.

---

## 주요 서비스 지도

| 영역 | 무엇을 하나 | 산출물 | 구동 방식 |
|---|---|---|---|
| **`data-platform/`** | 한국 법안·법률 원문과 금융위원회 가이드라인·감독규정, 증권신고서 등을 색인해 하이브리드 검색(벡터+키워드)과 의존 그래프로 조회 | `data/serving/index.sqlite` | `make` CLI |
| **`gen-docs/`** | 유형 판정 → 작성 → 자기심사를 거쳐 조각투자·STO 증권신고서 초안을 자동 작성. `data-platform` 코퍼스를 근거로 인용·자기검열 | `gen-docs/st_prospectus/<slug>/` | Agent Skill (`sto-filing`) |
| **`gen-apps/`** | 작성한 증권신고서와 `data-platform` 코퍼스를 근거로 토큰증권(RWA) 서비스 dApp 초안을 자동 생성 | `gen-apps/<slug>/` | Agent Skill (`st-service-dapp`, `filing-to-dapp`) |

위 표의 "코퍼스를 근거로"는 **색인을 갖춰 둔 경우**(직접 빌드하거나 `fetch-index`로 받아 둔
경우)에 해당합니다. `gen-docs`·`gen-apps`는
**코퍼스가 없어도 끝까지 동작**하며, 그때는 스킬이 자체 기준으로 작성하고 자기심사합니다.
즉 코퍼스는 근거를 조문 단위까지 짚어 주는 **선택 요소**이면서, 그 자체로 법령을
검색하는 도구이기도 합니다.

```
26bmdc/
├── README.md · ARCHITECTURE.md · LICENSE   # 저장소 전체를 아우르는 문서 (MIT 라이선스)
├── AGENTS.md              # 영역 간 규약 정본 (에이전트 지침, 지도)
├── CLAUDE.md              # @AGENTS.md 포인터 (Claude Code 진입점)
├── docs/                  # 아키텍처 다이어그램 (overview.svg · architecture.svg · system.svg)
├── .agents/skills/        # 루트 스킬 정본 (sto-filing·st-service-dapp·filing-to-dapp·corpus-lookup)
│                          #   ↳ .claude/skills/ 는 `make skills` 로 만드는 연결용 폴더 — 추적 안 함
├── data-platform/         # ── 코퍼스(문서 색인) 파이프라인
│   ├── README.md          #    셋업·아키텍처·근거 (이 영역의 정본)
│   ├── Makefile           #    make build / query / ask / impact / graph 등
│   ├── source/            #    실제 코퍼스 70건 — git 추적 대상 (구성은 아래 라이선스 절)
│   └── pipeline/ transform/ agent/ 등
├── gen-docs/
│   └── st_prospectus/
│       ├── sto-filing/    #    스킬 소스 = 정본 (SKILL.md + references/)
│       ├── prompt-templates/  # 프롬프트 골조 = 정본
│       ├── build_prompts.py   # 생성기 (PACKAGING.md 참조)
│       ├── dist/          #    생성물: 단독 실행 프롬프트 3종
│       └── <slug>/        #    생성된 증권신고서가 쌓이는 곳
└── gen-apps/
    ├── ST_SERVICE_DAPP_PROMPT.md  # dApp 생성 대형 프롬프트 (Q1~Q7만 사람이 채움)
    ├── st-service-dapp/   #    스킬 소스
    ├── filing-to-dapp/    #    신고서→dApp 브리지 스킬 소스
    └── <slug>/            #    생성된 dApp이 쌓이는 곳
```

**data-platform 파이프라인 상세** — 코퍼스가 색인되는 전체 경로(수집 → 랜딩 → Meltano
EL(수집·적재) → SQLMesh(변환) → 서빙 빌드):

![data-platform 파이프라인](docs/architecture.svg)

**색인·질의 런타임** — 위 파이프라인이 만든 gold를 DuckLake에 저장한 뒤, Vector(의미)·
FTS(키워드)·Graph(관계) **세 갈래로 나눠 `index.sqlite`에 색인**합니다. 질의는 하이브리드 검색
(벡터+키워드) + 그래프 컨텍스트로 답하며, 각 단계의 대표 실패 지점도 함께 표시했습니다:

![data-platform 색인·질의 시스템](docs/system.svg)

> 각 영역 내부 흐름을 담은 mermaid 다이어그램 5종은 [`ARCHITECTURE.md`](ARCHITECTURE.md) 참조.

---

## 빠른 시작

### 전제

- **AI 에이전트** — Claude Code, Codex, 또는 Antigravity. `gen-docs`·`gen-apps`는
  에이전트가 스킬을 로드해 구동합니다.
- **Python 3.12 권장(≥3.10) + [uv](https://docs.astral.sh/uv/)** — data-platform
  파이프라인용. 버전 플로어의 정본은 `data-platform/README.md`의 *Version floors*.
- **Node.js 22.10+** — gen-apps 산출물(Scaffold-ETH 2 dApp)을 실제로 띄울 때만.
  `.nvmrc`에 `22`만 적혀 있어 `nvm use`는 22 계열을 골라 주지만 **22.10 이상임을
  보장하지는 않습니다**(이 최소 버전은 `create-eth`가 요구합니다). `node -v`로 한 번
  확인합니다. 반대로 **너무 최신 Node를 쓰면 오히려 막힐 수 있습니다** — 네이티브
  모듈(`better-sqlite3`)에 그 버전용 사전 빌드본이 아직 없으면 직접 컴파일하다 실패합니다.
- **[Foundry](https://getfoundry.sh)** (`forge`·`anvil`·`cast`) — gen-apps에서만.
  **`forge`가 PATH에 없으면 스캐폴딩 자체가 중단됩니다**(`create-eth`가 검증합니다).
  설치돼 있어도 PATH에 없으면 같으니 `forge --version`으로 먼저 확인합니다
  (`~/.foundry/bin`이 흔한 위치).

### 0. 에이전트 열기 (스킬을 쓰는 법)

clone 직후 **한 번만** 실행합니다:

```bash
make skills     # 에이전트가 읽는 스킬 디렉터리를 만듭니다 (python3 하나면 되고, uv·venv 불필요)
```

스킬 원본은 `.agents/skills/`에 있고 clone할 때 함께 받습니다. 다만 **에이전트가 실제로
읽어 들이는 폴더**는 저장소에 들어 있지 않아 이 명령으로 만들어야 합니다. 건너뛰면
에이전트에 스킬이 하나도 보이지 않는데, 고장난 것이 아니라 아직 만들지 않은 것입니다.

코퍼스까지 한 번에 세우려면 `make quickstart`입니다(이 `make skills`를 포함합니다. uv가
필요하고, 캐시가 따뜻한 기계에서 실측 89초 — [3절](#3-선택-법률-코퍼스-색인조회-data-platform)).

그다음 저장소 루트(또는 작업할 하위 폴더)에서 에이전트를 엽니다 — Claude Code는 터미널에서
`claude`, Codex·Antigravity는 이 폴더를 작업 폴더로 엽니다. 그 세션 안에서 **자연어로
요청**하면 에이전트가 요청에 맞는 스킬을 자동 로드합니다. 아래 예시의 따옴표 문장이
그대로 입력입니다.

### 1. 증권신고서 만들기 (gen-docs) — 코퍼스 없이도 동작

에이전트에게:

> "이 조각투자 상품으로 증권신고서 써줘" · "이게 투자계약증권에 해당하나요?"

`sto-filing` 스킬이 **증권 유형(투자계약증권 / 비금전신탁 수익증권 / 집합투자증권)을
먼저 판정**한 뒤 유형별 기준으로 작성하고, 금융감독원 심사 관점으로 자기심사합니다.
유형이 갈리면 발행 가능 여부 자체가 갈리므로 판정을 건너뛰지 않습니다. 산출물은
`gen-docs/st_prospectus/<slug>/`에 쌓입니다.

### 2. 서비스 dApp 만들기 (gen-apps)

- **신고서에서 이어서** — `filing-to-dapp`: 완성된 신고서를 dApp 사양으로 역매핑하고
  신고서의 판정 결과를 그대로 물려받은 뒤 생성 단계로 넘깁니다.
  > "신고서 다 썼으니 이어서 앱 만들어줘"
- **직접** — `st-service-dapp`: 서비스 비전·운영 범위·발행 단위(Q1~Q7)를 확정한 뒤
  `ST_SERVICE_DAPP_PROMPT.md`를 실행해 Scaffold-ETH 2 기반 dApp을 끝까지 생성합니다.
  > "부동산 조각투자 dApp 만들어줘"

산출물은 `gen-apps/<slug>/`에 쌓입니다(신고서와 같은 슬러그로 짝지어집니다).

### 3. (선택) 법률 코퍼스 색인·조회 (data-platform)

코퍼스는 그 자체로 법령 검색 도구이면서, 위의 문서·dApp 생성에 근거를 더해 주는
선택 요소이기도 합니다. 색인을 빌드하면 조문을 직접 인용할 수 있습니다:

clone 직후 한 번, 루트에서:

```bash
make quickstart   # 훅·스킬 + 의존성 + .env + 임베더 + 색인 + 벡터캐시
```

빈 디렉터리에 clone해서 질의가 나오기까지 **실측 89초**입니다(clone 3초 + quickstart 84초 +
첫 질의 2초, 디스크 1.7 GB). 직접 빌드하면 약 32분입니다 — 색인을 새로 만들지 않고 발행된
것을 받아 설치하기 때문입니다. 다섯 단계 전부 멱등이라 다시 돌려도 안전합니다(재실행 7초).

단, 이 89초는 **uv 패키지 캐시와 모델 캐시가 이미 있는 기계**에서 잰 값입니다. 그 둘을
처음 받는 기계는 1단계(`make setup`)와 3단계(`make warm-models`)에서 더 걸립니다. 이
숫자에 포함된 네트워크 전송은 색인 96 MB 하나뿐입니다(`M:clone-to-query`).

색인 파일 자체는 여전히 커밋하지 않고 릴리스 자산으로 나르며, git이 나르는 것은 그
sha256(`data-platform/index_release.json`, 추적 대상)입니다. 받은 바이트가 이 해시와
다르면 설치되지 않습니다.

`.env`는 **없을 때만** 만들어 줍니다 — 이미 있으면 비밀이 들어 있을 수 있어 건드리지 않고,
발행자와 임베더가 다르면 어느 줄을 바꿔야 하는지 알려 줍니다(기본값 `hashing`으로는 발행
색인을 설치할 수 없습니다. 모델 없이도 빌드가 되도록 일부러 정해 둔 값입니다).

직접 빌드하려면:

```bash
cd data-platform
make setup      # uv sync + meltano install (최초 1회, 의존성 변경 시 재실행)
make build      # inbox → DuckLake → SQLMesh → index.sqlite, 그리고 smoke 테스트
```

`make build`는 멱등입니다 — 몇 번을 돌려도 행이 중복되지 않고 raw 영역도 건드리지 않습니다.

**문서를 추가하려면** `source/<컬렉션>/`에 파일을 넣고 `source/CORPUS_MANIFEST.tsv`에 행
(sha256 + 바이트 + 출처)을 더한 뒤 `make build`입니다. `quickstart`가 벡터캐시까지 세워 두므로
**새 청크만 인코딩됩니다** — 안 그러면 문서 하나 얹자고 20,344청크를 다시 인코딩합니다.

빌드 후:

```bash
make query Q="예치금 분리보관 의무"          # 하이브리드 검색 (벡터+키워드)
make ask   Q="스테이블코인 발행자 준비자산"    # 위 + 그래프로 연결된 문서의 관련 조문까지
make impact NODE=<node>                      # 의존 그래프 상 영향 범위
```

루트에서 코퍼스만 빠르게 조회하려면 `make -C data-platform query Q="<질의>"`. 셋업·
아키텍처·검색 원리의 정본은 **`data-platform/README.md`**입니다.

---

## 대표 워크플로 · 스킬

| 하고 싶은 것 | 영역 | 실행 |
|---|---|---|
| 법안이 무엇을 규정하는지 근거로 답 | data-platform | `make query`/`make ask`, 또는 `corpus-search`·`corpus-graph` 스킬 |
| 코퍼스에 근거한 문서 초안·요약 | data-platform | `document-drafting` 스킬 (cwd=data-platform) |
| 조각투자 상품의 증권신고서 | gen-docs | `sto-filing` 스킬 |
| 신고서를 온체인 서비스로 | gen-apps | `filing-to-dapp` 스킬 (sto-filing + st-service-dapp 오케스트레이션) |
| RWA/증권형 dApp 직접 | gen-apps | `st-service-dapp` 스킬 |

**엔드투엔드 예시:** 코퍼스로 규제 경로 확인(`make ask`) → 증권신고서 작성
(`sto-filing`) → 같은 슬러그로 dApp 생성(`filing-to-dapp`). 하나의 사업이 지식·문서·앱
세 계층으로 이어지고, 셋을 묶는 것은 슬러그 하나입니다.

**루트 스킬** (`.agents/skills/`): `sto-filing`, `st-service-dapp`, `filing-to-dapp`,
그리고 루트에서 코퍼스를 조회할 때 쓰는 래퍼 `corpus-lookup`.
**data-platform 스킬** (`data-platform/.agents/skills/`, **cwd=data-platform 전제**) —
코퍼스 조회(`corpus-search`·`corpus-graph`), 문서 작성, 코드 영향 분석, 새 출처 온보딩,
검색 품질 튜닝 등. 각각의 용도는 `data-platform/AGENTS.md`가 정본입니다. 루트에서 코퍼스가
필요하면 원본 대신 `corpus-lookup` 래퍼를 씁니다.

---

## 영역 간 규약

주요 서비스를 하나로 잇는 것은 아래 세 가지 규약뿐입니다. 정본은 루트 `AGENTS.md`이고,
여기에는 요약만 둡니다.

- **공유 슬러그** — 하나의 사업은 증권신고서와 dApp에서 **같은 kebab-case ASCII
  슬러그**를 씁니다: `gen-docs/st_prospectus/<slug>/`(문서) ↔ `gen-apps/<slug>/`(dApp).
  발행사+증권명에서 파생합니다. **예약어**(슬러그로 금지): `sto-filing`,
  `st-service-dapp`, `filing-to-dapp`. 이 슬러그는 data-platform의 node-id
  슬러그(한글·언더스코어 유지)와 **별개** 체계입니다.
- **루트에서 코퍼스 조회** — `make -C data-platform query Q="<질의>"`. 색인이 먼저
  빌드돼 있어야 합니다. 검색 규칙·랭킹 해석의 정본은 `data-platform/AGENTS.md`와
  `corpus-search` 스킬입니다.
- **gen-docs→gen-apps 브리지** — `filing-to-dapp`가 담당합니다: 완성된 신고서에서
  서비스 사실(기초자산·수익구조·전매제한·적격투자자 요건)을 뽑아 dApp의 Q1~Q7과 토큰
  표준으로 역매핑하고, 신고서의 **발행 불가 판정을 dApp 게이트로 승계**해 발행 불가한
  증권은 dApp 생성도 막습니다.

---

## 기여·유지보수 규약

- **AGENTS.md가 에이전트 지침의 정본입니다.** 루트 `AGENTS.md`는 지도와 영역 간 규약만
  담고, 각 영역의 세부는 그 영역 문서가 지배합니다. `CLAUDE.md`는 `@AGENTS.md` 한 줄
  포인터입니다(Claude Code만 `CLAUDE.md`를 읽으므로). 규약은 `AGENTS.md`에서만 고칩니다.
- **스킬은 `.agents/skills/`에서만 고칩니다.** git이 추적하는 것도 이 폴더뿐입니다.
  `.claude/skills/`는 `make skills`가 만들어 주는 **연결용 폴더**로, Claude Code가 읽는
  경로일 뿐 내용을 따로 갖지 않습니다. 커밋하지 않는 이유는 두 가집니다. 특정 도구의 이름이
  저장소 이력에 남고, `core.symlinks=false`로 clone한 Windows에서는 심링크가
  **경로만 적힌 텍스트 파일**이 되어 스킬이 **아무 오류도 없이 그냥 로드되지 않습니다.**
  `make skills`는 심링크를 우선 쓰고, 만들 수 없는 환경에서는 복사본을 둡니다. 그 복사본이
  원본과 달라지면 `make check`가 잡아냅니다.
- **clone 후 한 번 `make quickstart`.** 훅·스킬을 세우고 코퍼스를 바로 쓸 수 있게 합니다
  (실측 89초, 캐시가 따뜻한 기계 기준). 훅·스킬만 필요하면 `make hooks && make skills`입니다. 둘 다 git이 clone에 담아 보내지 못하는
  것이라 각 사본에서 한 번씩 만들어야 합니다 — 앞은 `core.hooksPath`를 설정해 커밋할 때
  `make check`가 돌게 하고, 뒤는 스킬 연결용 폴더를 만듭니다. 이 폴더가 없어도 검사는
  막지 않고 알려만 줍니다. 급할 때는 `git commit --no-verify`로 건너뜁니다.
- **커밋 전에 `make check`.** 저장소 루트에서 몇 초면 끝납니다. 린트·스킬 프론트매터·
  생성물 최신성부터 "측정치가 어느 코퍼스에서 나왔는지 밝히는가" 같은 이 저장소만의
  원칙까지, **어느
  영역에도 속하지 않아 지금까지 아무도 검사하지 않던 것들**을 봅니다. 무엇을 보는지는
  게이트마다 자기 이름을 출력하니 한 번 돌려보면 되고, 목록의 정본은 루트 `Makefile`입니다
  (여기 옮겨 적지 않는 이유는 게이트가 계속 늘기 때문입니다). 자동수정은
  `make fmt`(import 정렬·표기 현대화만 건드립니다). data-platform까지 포함한 전체 게이트는
  `make verify`(빌드가 돌아 느립니다). 린트 규칙은 `ruff.toml`에 얇게 두었습니다 — 줄 길이는
  강제하지 않고, 의도적인 광범위 `except`도 규칙으로 막지 않습니다.
- **sto-filing 패키징** — `sto-filing/` 또는 `prompt-templates/`를 고쳤으면
  `make prompts`(= `python3 build_prompts.py`)로 `dist/` 프롬프트 3종을 재생성합니다.
  잊으면 배포본에 옛 내용이 그대로 남는데, 이제 `make check`가 그것을 잡아냅니다.
  `dist/`는 손으로 고치지 않습니다. `gen-docs/st_prospectus/PACKAGING.md` 참조.
- **파이프라인이 만들어 낸 데이터는 절대 커밋하지 않습니다** — `data-platform/data/`는
  전부 git-ignore 대상입니다. 코퍼스 원본은 `data-platform/source/`에 두며(이곳은 git-ignore 대상이
  아니라 추적 대상입니다). 이렇게 나누는 이유는 `data-platform/README.md`의
  *control/data plane* 절에 있습니다.
- **실행이 만들어 낸 산출물도 커밋하지 않습니다** — `gen-docs/st_prospectus/<slug>/`와
  `gen-apps/<slug>/`에는 발행사의 증권신고서 초안과 투자자 데이터가 떨어집니다. 이 리모트는
  공개이고, push된 커밋은 지워도 히스토리에 남습니다. `.gitignore`가 1차로 막고, 패턴을
  넣는 것을 잊었을 때는 `make check`가 막습니다. 작업메모는 `docs/design/`에 둡니다.

> **저장소 경계·상태:** 루트 `26bmdc/` 전체가 **하나의 git 저장소**로
> `data-platform`·`gen-docs`·`gen-apps`를 함께 추적합니다(원격
> `github.com/fistline/2026BCMD`, branch `main`). `data-platform/source/`의 코퍼스
> 원본 70건은 추적·커밋되어 clone에 함께 실립니다. `data/`·`.venv`·`.meltano`는
> 재생성물이라 gitignore 대상이고, **색인은 그 안에 있어 clone으로 오지 않습니다.**
> 대신 릴리스 자산으로 받을 수 있으며(`make -C data-platform fetch-index`), 그 sha256을
> 담은 `data-platform/index_release.json`은 추적됩니다 — 바이트는 릴리스가, 해시는 git이
> 나릅니다.

---

## 트러블슈팅

clone 직후 실제로 자주 걸리는 것들입니다. 대부분은 **git으로 함께 오지 않는 것**을 아직
만들지 않은 상태이지, 고장난 것이 아닙니다.

| 증상 | 왜 | 조치 |
|---|---|---|
| 에이전트에 스킬이 안 보임 | `.claude/skills/`는 만들어 쓰는 폴더라 clone에 없습니다 | `make skills` |
| 커밋이 `make check`에서 멈춤 | 커밋 훅이 검사를 돌립니다 | 실패한 검사와 대응 명령이 함께 출력됩니다. ruff는 `make fmt`, `dist/`가 낡았으면 `make prompts`, 스킬 폴더는 `make skills` |
| `... does not exist. Build it with 'make build'` | **색인은 git으로 오지 않습니다** — `index.sqlite`는 `data/` 안에 있고, 이 폴더를 커밋하지 않는 것이 저장소의 첫 번째 원칙입니다 | `make -C data-platform setup` 후 둘 중 하나: `make -C data-platform fetch-index`(약 92 MB를 릴리스에서 받아 검증 후 설치, `.env`가 발행자와 같은 임베더를 써야 합니다) 또는 `build`(수십 분, 실측치는 `data-platform/MEASUREMENTS.md`의 `M:chunk-650`) |
| 검색은 되는데 품질이 문서의 수치와 다름 | `.env.example`의 기본값이 `EMBEDDING_PROVIDER=hashing`이기 때문입니다. **모델을 내려받지 않고도 빌드가 끝나도록 일부러 정해 둔 값**이지 오류가 아닙니다 | 문서에 적힌 품질을 쓰려면 `.env`에 `onnx_int8`과 `EMBEDDING_MODEL=Xenova/bge-m3`을 지정합니다. 값을 바꾸면 `index_signature`가 달라져 **다시 빌드해야 하며**, 그전까지는 질의가 조용히 옛 결과를 주는 대신 분명하게 실패합니다 |
| Windows에서 스킬이 열리지 않음 | 심링크를 만들 수 없는 환경입니다 | `make skills`가 심링크 대신 복사본을 만들어 둡니다. 단 **원본을 고치면 다시 실행**해야 하고, 잊더라도 `make check`가 잡아냅니다 |
| 폴더를 옮긴 뒤 전부 깨짐 | `.venv`·`.meltano`가 절대경로를 기억하고 있습니다 | `make -C data-platform reset` |

빌드 게이트·버전 플로어의 정본은 `data-platform/README.md`(*Verification*·*Version
floors*)와 `data-platform/AGENTS.md`입니다.

## 라이선스

**MIT** — 상업적 이용을 포함해 자유롭게 쓰고, 고치고, 재배포할 수 있습니다. 조건은 하나뿐입니다.
**사본에 아래 저작권 표시와 라이선스 전문을 함께 넣어야 합니다.** 전문은 `LICENSE`에 있습니다.

> Copyright (c) 2026 **aileaf (김정한 / Junghan Kim)**

**MIT가 적용되는 범위는 이 저장소가 직접 만든 코드와 문서까지입니다.**
`data-platform/source/`에 있는 코퍼스는 각 기관이 공개한 자료를 원문 그대로 옮겨 둔
것이라, 저작권이 그 기관에 있고 MIT의 적용을 받지 않습니다.

| 출처 | 건수 | 무엇 |
|---|---:|---|
| 국가법령정보센터 | 28 | 법령 · 행정규칙 |
| 국회 의안 | 20 | 법안 원문 (hwp/pdf) |
| 금융투자협회 | 6 | 모범규준 · 표준약관 |
| DART 전자공시 | 4 | 투자계약증권 증권신고서 |
| 금융위원회 · 금융보안원 | 3 | 가이드라인 |
| 혁신금융 사업자 플랫폼 | 2 | 신탁수익증권 공시 — **DART 대상이 아니다** |
| 기업 공개자료 | 3 | 표준약관 · 내부통제기준 사본 |
| *이 저장소가 쓴 요약* | *4* | *원문 아님* |
| **합계** | **70** | |

파일별 sha256과 출처는 `source/CORPUS_MANIFEST.tsv`가 **70건 전부** 기재합니다.

## 유의

> 생성되는 증권신고서·dApp은 **참고용 초안**이며 **법률자문·투자권유가 아닙니다.**
> 실제 제출·발행 전에는 법률의견서 확보와 금융감독원 사전협의를 권고합니다.
