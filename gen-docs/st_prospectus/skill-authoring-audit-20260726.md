# 스킬 저작 규격 대조 및 개선계획

**작성일** 2026-07-26
**대조 기준** Agent Skills 공개 표준 + Claude Code 스킬 문서
**대상** 스킬 14개 + 대형 프롬프트 4종

---

## 1. 근거 규격 (원문 확인)

| 출처 | URL |
|---|---|
| Agent Skills 명세 | https://agentskills.io/specification |
| 스킬 저작 베스트프랙티스 | https://agentskills.io/skill-creation/best-practices |
| Claude Code 스킬 문서 | https://code.claude.com/docs/en/skills |

### 하드 규격 (명세, MUST)

| 필드 | 제약 |
|---|---|
| `name` | **필수.** 1–64자. 소문자·숫자·하이픈만. 하이픈으로 시작/종료 불가. 연속 하이픈(`--`) 불가. **상위 디렉터리명과 일치해야 함** |
| `description` | **필수.** 1–1024자. 무엇을 하고 **언제 쓰는지** 모두 기술 |
| `license` · `compatibility`(≤500자) · `metadata` · `allowed-tools` | 선택 |

### 소프트 규격 (SHOULD / 권장)

- **`SKILL.md`는 500줄 · 5,000토큰 미만**으로 유지. 넘으면 `references/`로 분리
- 파일 참조는 **SKILL.md에서 1단계 깊이**. *"Avoid deeply nested reference chains."*
- 참조 파일은 **언제 읽을지를 조건으로 명시.** *"Read `references/api-errors.md` if the API returns a non-200 status code"* 가 *"see references/ for details"* 보다 낫다
- **Gotchas 절을 SKILL.md에 둔다.** *"The highest-value content in many skills is a list of gotchas"* — 에이전트가 상황을 만나기 **전에** 읽어야 하므로 참조 파일이 아니라 SKILL.md
- **에이전트가 이미 아는 것은 뺀다.** 각 문장에 *"이 지시가 없으면 에이전트가 틀리는가?"* 를 묻고 아니면 삭제
- **기본값을 주고 메뉴를 나열하지 않는다**
- **선언이 아니라 절차를 가르친다**
- **evals로 검증한다.** 트리거되는 것과 의도대로 동작하는 것은 다르다

### Claude Code 추가 사항

- 리스팅에서 `description` + `when_to_use` 합계가 **1,536자에서 잘린다.** 핵심 용례를 앞에
- 리스팅 예산은 컨텍스트의 **1%**. 초과하면 덜 쓰는 스킬부터 description이 잘림
- 스킬 본문은 **로드 후 턴을 넘어 컨텍스트에 남는다** — 모든 줄이 반복 비용

---

## 2. 하드 규격 대조 — **14/14 통과**

| 항목 | 결과 |
|---|---|
| `name` ↔ 디렉터리 일치 | 14/14 ✓ |
| `description` ≤ 1024자 | 14/14 ✓ (최장 `corpus-search` 750자) |
| `SKILL.md` ≤ 500줄 | 14/14 ✓ (최장 `sto-filing` 280줄) |
| 리스팅 총량 | 6,842자 — 1% 예산(≈10,000자) 내 ✓ |

**규격 위반은 없다.** 아래는 전부 권장사항 미충족이다.

---

## 3. 소프트 규격 대조 — 위반 5종

### A. `SKILL.md` 토큰 초과 (권장 <5,000)

| 스킬 | 글자 수 | 추정 토큰 | 판정 |
|---|---|---|---|
| `sto-filing` | 9,413 | ≈6,723 | ✗ **34% 초과** |
| `st-service-dapp` | 7,312 | ≈5,222 | ✗ 4% 초과 |
| `filing-to-dapp` | 5,306 | ≈3,790 | ✓ |
| `corpus-lookup` | 1,479 | ≈1,056 | ✓ |

> 한국어는 토큰 밀도가 높아 줄 수로는 안 걸리고 토큰으로 걸린다.
> 500줄 기준만 보면 통과지만 5,000토큰 기준에서 두 개가 초과한다.

**원인** — 이번 세션에서 `sto-filing/SKILL.md`에 게이트 설명·실물 대조 지시·코퍼스 보유 표를
계속 얹었다(219→280줄). 판정 로직은 `classification.md`에 있는데 SKILL.md에도 요약이 있어
**중복**이다.

### B. Gotchas 절 — **14/14 전부 없음** ★ 최대 기회

공식 문서가 *"많은 스킬에서 가장 가치 높은 콘텐츠"* 로 지목한 항목이 하나도 없다.
그리고 *"에이전트가 틀려서 바로잡아야 했던 것을 gotchas에 추가하라 — 스킬을 개선하는
가장 직접적인 방법"* 이라고 한다. **우리는 이번 세션에서 정확히 그 상황을 12번 겪고
하나도 기록하지 않았다.**

기록되지 않은 실제 gotcha:

| # | Gotcha | 어느 스킬 |
|---|---|---|
| 1 | `better-sqlite3` v11은 Node 26 프리빌트가 없어 소스 빌드가 실패한다. **v13 이상** 필요 | st-service-dapp |
| 2 | RainbowKit의 지갑 목록이 `@coinbase/cdp-sdk → @x402/*`(미배포)를 끌고 와 **번들이 깨진다.** injected 커넥터를 `@wagmi/core`에서 직접 가져오고 `IgnorePlugin` 적용 | st-service-dapp |
| 3 | FTS5 **contentless 테이블은 UPSERT/DELETE 미지원.** 자체 저장형으로 만들고 delete-then-insert | st-service-dapp |
| 4 | anvil은 **유휴 시 블록이 전진하지 않는다.** 확정 지연(confirmations)을 1 이상 두면 마지막 이벤트가 영원히 미인덱싱. 로컬은 0 | st-service-dapp |
| 5 | `--block-time`을 두면 배포 스크립트 트랜잭션마다 블록을 기다려 **기동이 3분+**. 즉시 채굴 + 확정지연 0이 맞다 | st-service-dapp |
| 6 | `useReadContracts`는 이종 호출 배열에서 결과 타입이 `never`가 된다. 접근자 헬퍼로 감싼다 | st-service-dapp |
| 7 | yarn 1 workspaces는 workspace 패키지의 `postinstall`을 항상 실행하지 않는다 | st-service-dapp |
| 8 | 코퍼스에 **기업공시서식·정정요구 사례집·신탁형 실물 신고서가 없다.** 있는 것은 법령·법안·투자계약증권 실물 4건 | sto-filing |
| 9 | 실물 빈도 측정 시 **문서 전수 grep ≠ 소제목 존재.** 소제목 기준으로만 세야 한다 | sto-filing |
| 10 | 실물 신고서는 편차가 크다 — II절 11~42%, 유의사항 10~18개. **분량을 심사 기준으로 쓰지 않는다** | sto-filing |
| 11 | `git check-ignore`는 **부정 규칙(`!`)도 출력**한다. 무시 여부는 반드시 **종료코드**로 판정 | (신규 후보) |
| 12 | 스킬은 심볼릭 링크라 미러가 자동 반영된다. 생성물(`dist/`)만 수동 재생성 | sto-filing |

### C. 참조 파일 간 순환 참조

명세: *"Keep file references one level deep from `SKILL.md`. Avoid deeply nested reference chains."*

`sto-filing/references/` 6개가 서로를 가리킨다.

```
common-core ──→ reference-filings ──→ common-core   (순환)
            ──→ investment-contract ──→ common-core (순환)
            ──→ trust-beneficiary  ──→ common-core  (순환)
reference-filings ──→ sources ──→ reference-filings (순환)
```

SKILL.md → references는 1단계라 명세 위반은 아니지만, **에이전트가 연쇄 로딩할 유인**이 있다.
실제로 작성 시 `common-core`(468줄) + 델타 + `reference-filings`가 함께 로드된다.

### D. `evals/` — 0/14

공식 문서: *"스킬이 트리거되는 것을 보는 것은 Claude가 찾았다는 뜻이지 의도대로 했다는 뜻이 아니다."*
`skill-creator` 플러그인이 `evals/evals.json` + 격리 실행 + 채점 + with/without 벤치마크를 자동화한다.

우리는 지난 개정에서 **적대적 리뷰로 결함 11건**을 잡았는데, 그건 수동이었고 재발 방지가 없다.

### E. 기본값 대신 메뉴 나열

`ST_SERVICE_DAPP_PROMPT.md` §7 3순위가 6개 링크를 **동등하게** 나열한다
(uRWA20/uRWA1155 레퍼런스, Centrifuge 3종, ERC-3643). 공식 문서는
*"기본을 하나 고르고 대안은 짧게 언급하라"* 고 한다.

---

## 4. 미사용 필드

| 필드 | 사용 | 쓰면 좋은 곳 |
|---|---|---|
| `when_to_use` | **0/14** | 트리거 문구를 여기로 옮기면 `description`이 짧아진다. 합계 1,536자 한도는 공유하지만 **역할 분리로 가독성**이 오른다 |
| `compatibility` | **0/14** | 우리 스킬은 실제 환경 요구가 있다 — `corpus-*`는 `uv`+색인, `st-service-dapp`은 Node ≥22·foundry, `sto-filing`은 코퍼스(선택) |
| `license` | 0/14 | 외부 배포 시 필요 |
| `allowed-tools` | 5/14 | `corpus-*`·`graph-viz`·`hitl-review`만 사용 |

---

## 5. 개선계획

### P1 — Gotchas 절 신설 (최우선)

근거가 확실하고(직접 겪음) 효과가 즉각적이다.

| 대상 | 작업 |
|---|---|
| `st-service-dapp/SKILL.md` | gotcha 1~7 추가 (환경·의존성·체인 함정) |
| `sto-filing/SKILL.md` | gotcha 8~10·12 추가 (코퍼스 보유 범위·측정 방법·편차) |
| 나머지 12개 | 겪은 사례가 쌓이면 추가. **지금 만들지 않는다** — 실제 근거 없이 쓰면 공식 문서가 경고하는 "generic 조언"이 된다 |

> 규칙: **에이전트가 틀려서 바로잡아야 했던 것만** 넣는다. 일반론은 넣지 않는다.

### P2 — `sto-filing/SKILL.md` 토큰 감량 (6,723 → <5,000)

중복을 걷어낸다. **내용을 지우는 게 아니라 위치를 옮긴다.**

| 현재 SKILL.md에 있는 것 | 조치 |
|---|---|
| 게이트 4종 표 + §110 제도 변화 설명 (약 25줄) | `classification.md`로 이동. SKILL.md는 "게이트는 classification.md §게이트" 한 줄 |
| 코퍼스 보유/미보유 표 (약 12줄) | `sources.md`로 이동 (이미 참조 우선순위가 거기 있다) |
| PHASE 3 골격 조립 상세 | `common-core.md`가 이미 갖고 있음 — SKILL.md는 순서만 |

감량 목표 약 2,000토큰. 대신 **Gotchas 절(P1)이 들어오므로 순증은 크지 않다.**

### P3 — 순환 참조 정리

`common-core.md`를 **허브**로 두고 단방향으로 만든다.

```
SKILL.md ──→ reference-filings (실물 정본, 참조 없음)
         ──→ common-core (허브: 델타·실물정본을 가리킴)
         ──→ 델타 2종 (common-core만 역참조)
         ──→ sources (참조 없음)
```

- `reference-filings.md`에서 `common-core`·델타로 나가는 링크 제거 → 순수 데이터 파일로
- `sources.md`에서 `reference-filings` 링크 제거 → 우선순위 계층만
- 델타는 `common-core` 역참조 1개만 유지

### P4 — `when_to_use` + `compatibility` 도입

```yaml
name: sto-filing
description: 조각투자·토큰증권(STO) 증권신고서를 작성하고 금융감독원 심사 관점에서 검증한다.
  증권 유형(투자계약증권 / 비금전신탁 수익증권 / 집합투자증권)을 먼저 판정한 뒤 유형별로 분기한다.
when_to_use: 증권신고서·투자계약증권·신탁수익증권·조각투자·STO·토큰증권·공모·청약·기초자산·
  증권성 판단·혁신금융서비스·DART 발행공시 언급 시. "이게 증권에 해당하나요" 같은 증권성 문의,
  조각투자 상품의 규제 경로·공시 서류·투자자 보호 요건 질문, 신고서 초안 검토 요청에도 적용.
compatibility: 코퍼스 조회는 선택 — data-platform 색인이 있으면 근거 조문·실물 신고서를 인용한다
---
```

### P5 — `ST_SERVICE_DAPP_PROMPT.md` 기본값 명시

§7 3순위에 기본을 정한다 — *"모듈 분리는 **Centrifuge liquidity-pools를 먼저 본다**.
제약형 토큰 구현은 **uRWA20 레퍼런스**. ERC-3643은 완결형 대안 비교용으로만"*.
(이미 "먼저 참조" 표기가 하나 있으나 표 전체가 동등하게 읽힌다.)

### P6 — evals 도입 (선택, 마지막)

`skill-creator` 플러그인을 설치해 `sto-filing`부터 시험한다.

```
/plugin install skill-creator@claude-plugins-official
```

테스트 케이스 후보는 이미 있다 — 이번 적대적 리뷰의 결함 11건이 그대로
"이 프롬프트에서 이 오탐이 나오면 안 된다"는 assertion이 된다.

**우선순위는 낮다.** P1~P3이 산출물 품질에 훨씬 크게 기여한다.

---

## 6. 실행 순서와 예상 효과

| 순서 | 작업 | 효과 | 위험 |
|---|---|---|---|
| 1 | **P1 Gotchas** (st-service-dapp, sto-filing) | 재실행 시 같은 함정 재발 방지. 공식 문서가 최고 가치로 지목 | 없음 (순증 콘텐츠) |
| 2 | **P2 토큰 감량** | 권장선 복귀. 매 실행마다 반복 절감 | 이동 중 참조 깨짐 → 링크 검사 필요 |
| 3 | **P3 순환 참조** | 연쇄 로딩 억제 | 낮음 |
| 4 | **P4 frontmatter** | 트리거 정확도·가독성 | 낮음 |
| 5 | **P5 프롬프트 기본값** | 에이전트가 6개 옵션 사이에서 헤매지 않음 | 낮음 |
| 6 | P6 evals | 회귀 방지 자동화 | 플러그인 설치 필요 |

1~3을 하면 `sto-filing/SKILL.md`가 **≈6,700 → ≈5,000토큰**(Gotchas 순증 포함)이 되고,
`st-service-dapp`은 gotcha가 들어가 ≈5,200 → ≈5,900이 되므로 **거기서도 감량이 필요**하다
(§3-A 대상에 포함).

---

## 7. 이 감사의 한계

- **토큰 수는 추정치다.** 한국어를 `chars/1.4`로 환산했다. 실제 토크나이저 값이 아니므로
  경계선(5,000 부근)에 있는 `st-service-dapp`은 판정이 뒤집힐 수 있다.
  정확한 값은 `/context`의 Skills 행이나 `/doctor`로 확인한다.
- **Gotchas 후보 12개는 이 세션에서 겪은 것만**이다. 다른 세션·다른 사용자가 겪은 함정은
  포함되지 않았다.
- **evals를 실제로 돌려보지 않았다.** P6의 효과는 문서 기반 추정이다.
- 나머지 10개 스킬(data-platform)은 **하드 규격만 검사**했다. 본문 품질(중복·일반론·기본값)은
  대조하지 않았다.
