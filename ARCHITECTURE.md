# 26bmdc 아키텍처

주요 서비스(법률 코퍼스 → 증권신고서 → dApp)와 그 사이를 잇는 스킬·프롬프트 구조를
다이어그램으로 정리한다. 각 영역의 세부 규칙은 그 영역의 문서가 지배하며(README·AGENTS·
각 SKILL.md), 이 문서는 **연결과 흐름**을 보여준다.

---

## 1. 전체 흐름 — 지식 → 문서 → 서비스

```mermaid
flowchart LR
  subgraph DP["data-platform — 법률 지식 베이스 (make CLI)"]
    direction TB
    src["법안·법률 원문<br/>(source/ · hwp·pdf)"] --> idx["index.sqlite<br/>벡터 + FTS5 + 의존그래프"]
    idx --> tl["tools<br/>hybrid_search · graph_query · graph_rag"]
  end
  subgraph GD["gen-docs — 증권신고서 (Agent Skill)"]
    direction TB
    sf["sto-filing<br/>유형판정 → 작성 → 자기심사"] --> fl["증권신고서<br/>st_prospectus/&lt;slug&gt;/"]
  end
  subgraph GA["gen-apps — 서비스 dApp (Agent Skill)"]
    direction TB
    fd["filing-to-dapp<br/>신고서→사양 매핑 + 게이트 승계"] --> sd["st-service-dapp<br/>Q1~Q5 → 표준판정 → PHASE 0–5"]
    sd --> dp2["dApp (Scaffold-ETH 2)<br/>gen-apps/&lt;slug&gt;/"]
  end
  tl -. "법령 근거 인용 (선택)" .-> sf
  tl -. "법령 근거 인용 (선택)" .-> sd
  fl ==>|"신고서 이어받기"| fd
  fl <-. "공유 슬러그로 결속" .-> dp2
```

- 실선 굵은 화살표(`신고서 이어받기`)가 주요 서비스를 하나로 잇는 핵심 경로다.
- 점선(`법령 근거 인용`)은 **선택** 의존이다 — gen-docs·gen-apps는 코퍼스 없이도 완주한다.
- 신고서와 dApp은 **같은 슬러그**로 결속된다: `st_prospectus/<slug>/` ↔ `gen-apps/<slug>/`.

---

## 2. gen-docs — 증권신고서 (`sto-filing` 스킬)

```mermaid
flowchart TD
  req["사용자: &quot;증권신고서 써줘&quot;"] --> cls["PHASE 1 — 유형 판정<br/>references/classification.md (STEP 0–5)"]
  cls --> t1["투자계약증권<br/>사전 인허가 불요"]
  cls --> t2["비금전신탁 수익증권<br/>혁신금융서비스 지정 필요"]
  cls --> t3["집합투자증권<br/>집합투자업 인가 필요"]
  t1 --> core["작성: common-core.md (공통 ~70%)<br/>+ 유형별 델타<br/>investment-contract.md / trust-beneficiary.md"]
  t2 --> core
  t3 --> core
  core --> mode{"실행 모드"}
  mode -->|"기획: 가정치 + 워터마크(_시뮬)"| rev["자기심사<br/>(금융감독원 심사 관점)"]
  mode -->|"제출: 실측치만 · 없으면 [미확정]"| rev
  rev --> out["증권신고서<br/>st_prospectus/&lt;slug&gt;/"]
```

- **판정이 먼저다:** 유형이 갈리면 문서 품질과 무관하게 발행 가능 여부 자체가 갈린다.
- 문서 골격의 ~70%는 `common-core.md` 하나로 관리하고, 유형별 파일은 **차이(델타)만** 담는다.
- 근거 조문은 코퍼스(`make -C data-platform query`)에서 인용하되, 없으면 `sources.md`로 대체.

---

## 3. gen-apps — 서비스 dApp (`st-service-dapp` + `filing-to-dapp`)

```mermaid
flowchart TD
  direct["사용자: &quot;dApp 만들어줘&quot;"] --> ssd
  subgraph BR["filing-to-dapp — 브리지 (얇은 오케스트레이터)"]
    fdoc["완성된 증권신고서"] --> gate{"게이트 승계<br/>발행 불가 / 미확정 / 저신뢰?"}
    gate -->|"YES"| stop["dApp 생성 차단"]
    gate -->|"NO"| map["매핑: 신고서 → Q1~Q5 + 표준 초안<br/>references/mapping.md"]
  end
  map --> ssd
  subgraph SSD["st-service-dapp — ST_SERVICE_DAPP_PROMPT.md 실행"]
    ssd["§1 Service Vision<br/>Q1 서비스 · Q2 여정 · Q3 자산·수익<br/>Q4 이전제약 · Q5 화면 (사람이 채움)"] --> std["§5 표준판정<br/>축1 이전제약 필요? · 축2 자산 다중성"]
    std --> mtx["S1~S6 매트릭스<br/>ERC-20 / ERC-6909 / ERC-721<br/>(제약 필요 시 + ERC-7943)"]
    mtx --> ph["PHASE 0–5 (게이트 순서)<br/>0 설계(DECISION.md) → 1 컨트랙트(forge test)<br/>→ 2 배포·시드 → 3 데이터계층 → 4 프론트 → 5 검수"]
  end
  ph --> out2["dApp (Scaffold-ETH 2 + SQLite)<br/>gen-apps/&lt;slug&gt;/"]
```

- `filing-to-dapp`는 **복제하지 않는다** — 문서는 `sto-filing`이, 생성은 `st-service-dapp`이
  그대로 하고, 이 스킬은 **매핑 + 게이트 승계**만 새로 담는다.
- **전매제한 → 표준 자동 확정:** 신고서 토큰은 규제상 전매제한이 있어 축1이 "제약 필요"로
  수렴 → ERC-7943(S2/S4/S6). 적격투자자·전매제한이 그대로 온체인 `TransferPolicy`가 된다.
- **발행 불가 증권은 dApp도 막는다:** sto-filing이 중단시킨 건 여기서도 중단한다.
- §2 설정(Token Standard=AUTO · MockUSDC · SQLite)은 고정이라 사람이 채우지 않는다.

---

## 4. data-platform — 코퍼스 색인 파이프라인

```mermaid
flowchart LR
  inbox["data/inbox/<br/>(드롭 존)"] --> raw["data/raw/<br/>(불변 존)"]
  raw --> mel["Meltano<br/>(EL)"]
  mel --> lake["DuckLake<br/>(lakehouse)"]
  lake --> sm["SQLMesh<br/>bronze → silver → gold<br/>(+ 차단 감사)"]
  sm --> idx["index.sqlite<br/>chunks_vec (sqlite-vec)<br/>chunks_fts (FTS5)<br/>nodes · edges (그래프)"]
  idx --> q["make query · ask · impact · graph"]
```

- 단일 SQLite 파일에 **벡터 · 키워드 · 그래프**가 함께 산다 — 세 소스가 서로 어긋나지 않는다.
- `hybrid_search`(벡터+FTS5 RRF 융합) · `graph_query`(의존 그래프) · `graph_rag`(검색 +
  그래프로 연결된 문서의 관련 조문). 읽기 경로에 네트워크 호출이 없다(오프라인).

---

## 5. 스킬 구조 — 정본과 미러

```mermaid
flowchart TD
  std["개방 SKILL.md 표준<br/>Codex · Claude Code · Antigravity 공용"]
  std --> SRC
  subgraph SRC[".agents/skills/ — 정본 (편집은 여기서만)"]
    r1["sto-filing"]
    r2["st-service-dapp"]
    r3["filing-to-dapp"]
    r4["corpus-lookup (루트 코퍼스 래퍼)"]
  end
  SRC ==>|"심링크 미러"| MIR
  subgraph MIR[".claude/skills/ — 미러 (심링크, 드리프트 없음)"]
    m["같은 대상을 Claude Code에 재노출"]
  end
  subgraph DPS["data-platform/.agents/skills/ (cwd=data-platform 전제)"]
    d1["corpus-search · corpus-graph"]
    d2["document-drafting · code-impact-analysis"]
    d3["doctype-profile-authoring · legal-schema-authoring"]
    d4["source-onboarding · hitl-review · correction-harvesting · graph-viz"]
  end
  r4 -. "루트에서 코퍼스 조회 시" .-> d1
```

- `.agents/skills/`가 **정본**, `.claude/skills/`는 그 심링크 미러 — 두 벌이 드리프트하지 않는다.
- 루트 스킬은 루트 cwd 전제, data-platform 스킬은 **cwd=data-platform** 전제. 루트에서
  코퍼스가 필요하면 원본 대신 `corpus-lookup` 래퍼를 쓴다.

---

> 이 다이어그램들은 프로젝트 구조의 **하한**이다 — 코드·프롬프트가 선언한 연결만 담는다.
> 각 절차의 정본은 해당 SKILL.md와 `data-platform/README.md`다.
