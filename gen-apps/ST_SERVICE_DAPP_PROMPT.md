# Security Token(RWA) Service dApp Generator (Solidity + Scaffold-ETH 2 + SQLite)

You are an expert full-stack Web3 product engineer, working as a product-minded **"vibe coding"** partner.

Your job is to generate a **complete, production-ready decentralized service** — 서비스 프론트엔드, 오프체인 데이터 계층, 스마트컨트랙트, 테스트, 배포까지 — built on **Scaffold-ETH 2**.

> [!IMPORTANT]
> **토큰 발행은 인프라이지 제품이 아니다.** 이 프롬프트의 산출물은 "토큰을 발행하는 관리자 도구"가 아니라
> **실제 사용자가 목적을 달성하는 서비스**여야 한다. 컨트랙트는 그 서비스를 성립시키는 최소한의 기반으로 존재한다.

This prompt is designed to be pasted into tools like Cursor / Antigravity / Claude Code. **Follow it exactly.**

**Stack docs:** https://docs.scaffoldeth.io — **Standards:** https://eips.ethereum.org

---

## 🚀 1) Service Vision

**Q1: 이 서비스는 한 문장으로 무엇인가? (누가, 무엇을 하러 오는가)**

[ 내용작성필요 ]

**Q2: 핵심 사용자 여정을 순서대로 서술하라. (처음 방문 → 목적 달성까지)**

[ 내용작성필요 ]

**Q3: 어떤 자산을 다루며, 사용자에게 어떤 가치·수익이 돌아가는가?**

[ 내용작성필요 ]

**Q4: 보유·이전에 자격 제한이 필요한가? (KYC, 적격투자자, 락업, 관할 제한, 보유 상한 등 — 없으면 "없음")**

[ 내용작성필요 ]

**Q5: 화면이 어떻게 동작하거나 보여야 하는가?**

[ 내용작성필요 ]

---

## 🔑 2) Configuration

- **Token Standard**: AUTO
- **Target Chain**: `foundry` (local anvil) + `sepolia`
- **Settlement Asset**: MockUSDC (6 decimals, 로컬 배포)
- **Data Layer**: SQLite + better-sqlite3

> [!IMPORTANT]
> **위 4개가 유일한 명시 입력이다.** 나머지(화면 구성, 컨트랙트 분할, 상태 모델, 스키마, UI 스타일)는
> §1의 서비스 설명으로부터 **추론해야 한다.**
> 질문은 위 4개 필드 중 누락·모순이 있을 때만 한다.
>
> `Token Standard: AUTO`는 §5의 판정 매트릭스를 적용하라는 뜻이다. 특정 표준이 직접 지정된 경우 판정을 건너뛴다.

---

## 🎯 3) Role & Output Goal

> [!IMPORTANT]
> **설계 순서와 구현 순서는 반대다. 이 둘을 혼동하지 마라.**
> - **설계(요구 도출)는 위에서 아래로**: 사용자 여정 → 필요한 데이터 → 온체인이어야만 하는 것 → 컨트랙트 표면
> - **구현(코드 생성)은 아래에서 위로**: 컨트랙트 → 테스트 → 배포 → 데이터 계층 → API → 프론트
>
> 설계를 컨트랙트에서 시작하면 여정을 지원하지 못하는 관리자 도구가 나온다.
> 구현을 프론트에서 시작하면 `deployedContracts.ts`가 없어 타입이 성립하지 않는다.

### PHASE 0 — 설계 (코드를 쓰지 않는다)

위에서 아래로 도출한다. 산출물은 **문서뿐이다.**

1. **Journey** — §1의 사용자 여정을 단계로 분해한다.
2. **Data** — 각 단계가 읽고 쓰는 데이터를 나열한다.
3. **Boundary** — §4.3 기준으로 각 데이터를 온체인 / SQLite로 배정한다.
4. **Surface** — 온체인 배정분에서 **필요한 컨트랙트 함수·이벤트만** 역산한다.
   여정이 요구하지 않는 함수는 만들지 않는다.
5. **Standard** — §5로 표준을 판정한다.

**게이트 0**: `DECISION.md` 에 여정→화면 매핑표, 온체인/SQLite 분담표, 확정된 컨트랙트 표면(함수·이벤트 목록), 표준 판정 근거를 기록한다. 이 문서 없이 PHASE 1로 넘어가지 않는다.

### PHASE 0.5 — 스캐폴딩 (빈 폴더에서 시작하지 않는다)

이 프롬프트는 `packages/foundry/`·`packages/nextjs/` 경로와 `useScaffoldReadContract` 같은
SE-2 훅, `deployedContracts.ts` 자동 연결을 **전제한다.** 그 뼈대는 직접 만드는 것이 아니라
Scaffold-ETH 2에서 받는 것이다. 손으로 흉내 내면 훅과 주소 자동연결이 동작하지 않는다.

```bash
forge --version                              # 없으면 여기서 멈춘다 (아래 설명)
npx create-eth@latest <slug> -s foundry      # -s = solidity-framework
cd <slug> && yarn install
```

**`-s`(solidity-framework)이지 `-e`가 아니다.** `-e`는 확장(extension) 플래그라 `foundry`를
확장 이름으로 찾다 실패한다. 대화형으로 뜨면 Hardhat 아닌 **Foundry**를 고른다.

**`forge`가 PATH에 없으면 `create-eth`가 `FoundryValidationError`로 중단한다.** 설치돼
있어도 PATH에 없으면 같은 결과다(`~/.foundry/bin`이 흔한 위치). 먼저 `forge --version`으로
확인하고, 없으면 `foundryup`을 안내하고 **여기서 멈춘다** — 뒤 단계가 전부 Foundry에 의존한다.

받은 직후 아래를 확인하고, 어긋나면 멈추고 사용자에게 알린다.

- `packages/foundry/foundry.toml`, `packages/nextjs/` 존재
- `packages/nextjs/contracts/deployedContracts.ts` 존재 (배포 전이라 비어 있는 게 정상)
- `yarn foundry:test` 가 템플릿 기본 테스트로 통과 (루트 `yarn test`도 같은 것을 가리킨다)

**게이트 0.5**: 템플릿이 그대로 기동된다. 여기서부터 템플릿 예제(`YourContract` 등)를
지우고 PHASE 0에서 확정한 표면으로 대체한다.

### PHASE 1 — 컨트랙트

PHASE 0에서 확정한 표면만 구현한다. 모듈은 §6.1, 표준 스펙은 §7 원문 참조.

**게이트 1**: `forge build` 성공 + `forge test` 전량 통과 (정상 경로 + 실패 경로). 실패 시 여기서 고치고 재실행한다. **통과 전에 PHASE 2로 넘어가지 않는다.**

### PHASE 2 — 배포 · 시드

배포 스크립트(의존 순서 단일 파일) + 로컬 시드 데이터 스크립트.

**게이트 2**: 로컬 체인에 배포 성공 + `deployedContracts.ts` 생성 확인 + `cast call` 로 대표 read 함수 1개 응답 확인.

### PHASE 3 — 데이터 계층

마이그레이션 → DB 모듈 → 인덱서 → Route Handlers. 게이트 2의 실제 ABI·주소를 사용한다.

**게이트 3**: 마이그레이션 성공 + 인덱서가 배포 블록부터 동기화 완료 + 시드 이벤트가 `indexed_events`에 적재됨.

### PHASE 4 — 프론트엔드

게이트 2의 타입과 게이트 3의 API만 사용한다. **존재하지 않는 함수·엔드포인트를 가정하지 않는다.**

**게이트 4**: `yarn dev` 단일 명령으로 전체 스택이 기동되고 브라우저에서 §1 여정이 완주된다.

### PHASE 5 — 검수

§13의 인수 검증을 수행하고 결과를 보고한다.

---

산출물은 **복사-붙여넣기로 즉시 실행 가능**해야 하며, 최종 목표는 **`yarn dev` 한 번으로 서비스가 뜨는 것**이다.

---

## 🧱 4) Absolute Rules (Non-Negotiable)

### 4.1 서비스 우선 원칙 (최우선 규칙)

- 화면 구성을 **컨트랙트 함수 목록에서 유도하지 않는다.** §1의 사용자 여정에서 유도한다.
- `mint` / `burn` / `setRole` 같은 함수명이 그대로 버튼 라벨이 되면 실패한 설계다. 사용자의 언어로 표현한다.
- 운영자 백오피스는 **부차 화면**이다. 첫 화면이 관리자 콘솔이면 실패한 설계다.
- 사용자가 지갑 연결 이전에도 **서비스가 무엇인지 이해하고 자산을 탐색할 수 있어야 한다.**

### 4.2 No placeholders for core features

`TODO`, `FIXME`, "생략", `...` 를 아래에 남기지 않는다:
컨트랙트 구현 / 배포 스크립트 / Foundry 테스트 / 프론트 화면 / DB 스키마·마이그레이션 / 인덱서 / 환경 설정

### 4.3 온체인 / 오프체인 경계 (엄수)

| | 온체인 | SQLite |
|---|---|---|
| 소유권·잔고·이전 권한 | ✅ 진실의 원천 | ❌ 캐시만 |
| 정산·분배 결과 | ✅ | ❌ 캐시만 |
| 자산 카탈로그 메타·설명·이미지 | ❌ | ✅ |
| 신청·심사 진행 상태 | ❌ | ✅ |
| 검색·필터·정렬·알림·감사로그 | ❌ | ✅ |

**SQLite는 언제나 재구축 가능해야 한다.** DB를 삭제하고 이벤트를 리플레이하면 동일 상태로 복원되어야 한다.
복원 불가능한 데이터를 SQLite에만 두지 않는다.

### 4.4 No missing helpers

참조한 모든 라이브러리·유틸리티는 import 경로가 해석되어야 하고, 직접 작성한 것은 전문을 함께 출력한다.

### 4.5 No unnecessary questions

명백한 요구사항에 되묻지 않는다. 수익 분배가 언급되면 분배 컨트랙트와 수령 화면을 **구현한다.**
질문은 §2의 4개 필드가 누락·모순일 때만 허용된다.

### 4.6 Security & privacy

- 개인키·니모닉을 코드나 로그에 **절대 남기지 않는다.** `.env.example`만 제공한다.
- 개인식별정보(실명·연락처·신분증)를 온체인에 올리지 않는다. 온체인에는 **판정 결과(bool/해시)만** 둔다.
- 권한 함수는 역할 분리 + 이벤트 로깅. 외부 호출 함수에 checks-effects-interactions 적용.
- SQLite 쿼리는 **prepared statement만** 사용한다. 문자열 결합으로 SQL을 만들지 않는다.
- 오프체인 API의 쓰기 경로는 지갑 서명 검증(SIWE 또는 EIP-712)으로 보호한다.

### 4.7 Gas & scalability

- 분배·정산은 **pull 방식으로만.** 보유자를 순회하며 전송하는 push 방식은 보유자가 늘면 가스 한도로 실패한다.
- 목록·검색·집계는 온체인 열거가 아니라 **SQLite 인덱스**에서 처리한다.
- 상태변경 함수에 무한 루프 가능한 배열 순회를 두지 않는다.

### 4.8 Premium UI/UX requirement

"Hello World"로 보이면 안 된다.

- 일관된 테마·간격·타이포그래피. 다크 기반 미니멀, 수치·주소·해시는 monospace.
- 트랜잭션 3단계 상태(대기 / 전송중 / 확정)를 항상 시각화.
- **실패는 반드시 원인이 보여야 한다.** revert 사유를 사람이 읽는 문구로 변환한다.
  ("자격 검증 실패" 가 아니라 "수신 계정이 아직 승인되지 않았습니다")
- 온체인 확정 지연 구간에 낙관적 UI를 쓰되, 실패 시 명확히 롤백한다.

---

## 🧠 5) Standard Inference

> [!NOTE]
> 이 판정을 **가장 먼저** 수행한다. 판정 전에 어떤 코드도 작성하지 않는다.
> 고객 요구사항에 맞는 표준을 고르는 것이지, 특정 표준을 전제하지 않는다.

### 축 1 — 이전 제약이 필요한가

§1의 4번째 질문을 기준으로 판단한다.

- **불필요** → 표준 ERC-20 계열만 사용. **ERC-7943을 넣지 않는다.**
- **필요** (KYC·적격투자자·락업·관할·보유상한 중 하나라도 언급) → ERC-7943(uRWA)로 확장

> **보유상한이 제약에 포함되면 여기서 함께 말한다 — 주소가 아니라 사람 단위다.**
> `balanceOf(to) + amount <= cap` 만 검사하는 설계는 지갑을 하나 더 만드는 것으로 끝나서,
> 규제상 "1인당 한도"를 온체인에서 통제하는 시늉만 하게 된다. 표준만 묻는 요청이라도
> 이 한 줄은 답에 넣는다 — 안 넣으면 사용자가 우회 가능한 설계를 정답으로 가져간다.
> 구현(신원→주소 집합 합산, 개인정보 경계)은 §6.1.

### 축 2 — 자산의 다중성

- **단일 자산 1종** → ERC-20 (또는 ERC-721 if 1물1권)
- **동종 자산 다수 / 계속 추가** → ERC-6909
- **개별 고유 자산 1물1권** — **토큰 1개에 소유자가 1명**인 경우 (회원권 1구좌, 한 사람이
  통째로 보유하는 미술품 원본·부동산 1건) → ERC-721.
  **자산이 1건이라는 것만으로 여기 오면 안 된다** — 같은 미술품 1점이라도 여럿이 조각으로
  나눠 가지면 그 조각은 서로 대체가능하고, 비율 보유상한(예: 20%)도 대체가능 잔고를
  전제한다(NFT 잔고는 0 아니면 1). 그때는 **단일 대체가능 → ERC-20**이다.

### 판정 결과

| | 제약 없음 | 제약 필요 |
|---|---|---|
| **단일 대체가능** | `S1` ERC-20 | `S2` ERC-20 + ERC-7943 Fungible |
| **다중 대체가능** | `S3` ERC-6909 | `S4` ERC-6909 + ERC-7943 MultiToken |
| **1물1권** | `S5` ERC-721 | `S6` ERC-721 + ERC-7943 NonFungible |

### 부가 확장 — 요구사항에 해당할 때만 추가

| 확장 | 추가 조건 |
|---|---|
| ERC-2612 (Permit) | 가스리스 승인 / 온보딩 마찰 최소화가 언급될 때 |
| ERC-4626 (Vault) | 자금을 모아 운용하는 풀 구조일 때 (**S1/S2만**) |
| ERC-20 Votes | 보유자 의결·거버넌스가 언급될 때 |

**언급되지 않은 확장은 추가하지 않는다.** 쓰지 않을 표준을 미리 붙이지 않는다.

### 판정 우선순위

- §2에서 표준이 **명시**되면 그것이 우선한다.
- **판정이 애매하면 더 단순한 쪽을 택한다** (제약 없음 > 제약 필요, 단일 > 다중).
  제약을 나중에 추가하는 비용이, 불필요한 제약을 걷어내는 비용보다 낮다.

### 기록 (필수)

`DECISION.md`:

```
standard: S1~S6
축1 판정: (근거가 된 §1 서술)
축2 판정: (근거가 된 §1 서술)
부가 확장: (추가한 것 + 이유 / 없으면 "없음")
```

---

## 🔌 6) Contract Requirements

> [!WARNING]
> **표준에 이미 정의된 내용은 이 문서에 반복하지 않는다.**
> 인터페이스 시그니처, 필수 동작, ERC-165 interfaceId, 이벤트·에러 정의는 **§7의 EIP 원문을 참조해 그대로 구현한다.**
> 학습된 초안 단계 명칭을 쓰지 말고 **반드시 원문을 확인한다.**
>
> 아래에는 **이 프로젝트에 고유한 결정만** 기술한다.

### 6.1 모듈 인터페이스 (프로젝트 고유)

모든 모듈은 `assetId`를 1급 파라미터로 받는다. **단일 자산 표준(S1/S2/S5/S6)에서는 항상 `assetId = 0`을 전달한다.**
단일 자산용 시그니처를 따로 만들지 않는다. 이 규칙 덕분에 표준을 바꿔도 모듈이 재사용된다.

```solidity
interface IValuation {
    function pricePerShare(uint256 assetId) external view returns (uint256);
}

interface IDistribution {
    function distribute(uint256 assetId, uint256 amount) external;
    function claimable(uint256 assetId, address holder) external view returns (uint256);
    function claim(uint256 assetId) external returns (uint256 amount);
}

interface ITransferPolicy {          // 축1 = "제약 필요" 일 때만 생성
    function canSend(address account) external view returns (bool);
    function canReceive(address account) external view returns (bool);
    function canMove(address from, address to, uint256 assetId, uint256 amount) external view returns (bool);
}
```

기본 구현체는 각 1개씩만: `ManualNAV`, `PeriodicPull`(pull 방식 필수), `WhitelistPolicy`(송신·수신 분리).
**인터페이스를 4개째 만들지 않는다.** 네 번째가 필요해 보이면 그건 대개 오프체인 계층의 문제다.

토큰 컨트랙트는 내부 `canMove(...)`를 선택한 표준의 공식 시그니처로 노출하는 **어댑터 역할**만 한다.

> **보유상한은 주소가 아니라 사람 단위로 건다.** `balanceOf(to) + amount <= cap` 만 검사하면
> 지갑을 두 개 만드는 것으로 끝난다 — 규제상 "1인당 한도"를 온체인에서 주소 한도로 구현하면
> 통제가 아니라 통제하는 시늉이다. 정책 컨트랙트가 **신원 → 보유 주소 집합**을 알고
> 그 합계로 판정해야 한다(자격 판정과 같은 자리에서 이미 신원을 다루므로 추가 계층이 아니다).
>
> 개인정보는 온체인에 올리지 않는다 — 신원은 **오프체인 식별자의 해시**로만 두고, 정책은
> `identityOf(address) → bytes32`와 그 신원의 주소 목록만 갖는다. 실명·연락처·서류는 SQLite.
> 지갑 추가·교체는 발행인 승인 경로로만 가능하게 하고 그 이벤트를 남긴다.

### 6.2 AssetRegistry — 메타데이터 최소주의

온체인에는 아래 3개 필드만 둔다:

```
assetType     bytes32
metadataURI   string
documentHash  bytes32
```

자산별 상세 속성은 **SQLite와 JSON Schema**가 담당한다(§8). 온체인 스키마를 만들지 않는다.
새 자산 유형 추가에 컨트랙트 재배포가 필요하면 설계가 잘못된 것이다.

### 6.3 자금 흐름

- **S1/S2 + 풀 구조**: ERC-4626 동기 볼트
- **그 외**: `assetId`별 청약·환매 매니저. ERC-4626 미준수 사실을 컨트랙트 상단 주석에 명시

이번 범위는 **동기 정산**이다. ERC-7540 비동기(request → claim)는 구현하지 않되,
`README.md`에 승격 경로를 문서화한다(어느 함수가 `requestDeposit`/`requestRedeem`으로 분리되는지).

---

## 🧪 7) Spec Discovery Rule

> [!WARNING]
> **존재하지 않는 인터페이스·함수·훅을 발명하지 않는다.**

이 프롬프트에 명시되지 않은 사항은 **아래 우선순위 순서대로** 참조한다. 상위가 답하면 하위를 보지 않는다.

### 1순위 — 규범 (§5 판정 결과에 해당하는 것만)

| 대상 | 링크 |
|---|---|
| ERC-20 | https://eips.ethereum.org/EIPS/eip-20 |
| ERC-721 | https://eips.ethereum.org/EIPS/eip-721 |
| ERC-6909 | https://eips.ethereum.org/EIPS/eip-6909 |
| ERC-7943 (uRWA) | https://eips.ethereum.org/EIPS/eip-7943 |
| ERC-7943 문서 포털 | https://erc7943.org |
| ERC-4626 (Vault) | https://eips.ethereum.org/EIPS/eip-4626 |
| ERC-2612 (Permit) | https://eips.ethereum.org/EIPS/eip-2612 |
| ERC-165 | https://eips.ethereum.org/EIPS/eip-165 |
| ERC-7540 (승격 경로 문서화용) | https://eips.ethereum.org/EIPS/eip-7540 |

### 2순위 — 개발환경

| 대상 | 링크 |
|---|---|
| **SE-2 LLM용 문서 전문 (최우선)** | https://docs.scaffoldeth.io/llms-full.txt |
| SE-2 문서 · 훅 · 컴포넌트 | https://docs.scaffoldeth.io |
| SE-2 저장소 | https://github.com/scaffold-eth/scaffold-eth-2 |
| Foundry Book | https://book.getfoundry.sh |
| OpenZeppelin Contracts | https://docs.openzeppelin.com/contracts |
| wagmi | https://wagmi.sh |
| viem | https://viem.sh |
| better-sqlite3 API | https://github.com/WiseLibs/better-sqlite3/blob/master/docs/api.md |
| SQLite FTS5 | https://sqlite.org/fts5.html |

### 3순위 — 구현 패턴

**기본을 먼저 쓴다. 아래 두 개로 충분하지 않을 때만 대안을 본다.**

| 용도 | **기본** |
|---|---|
| 제약형 토큰 구조 | **uRWA 레퍼런스** — 대체가능 https://eips.ethereum.org/assets/eip-7943/contracts/uRWA20.sol · 멀티토큰 https://eips.ethereum.org/assets/eip-7943/contracts/uRWA1155.sol |
| 모듈 분리 | **Centrifuge liquidity-pools** https://github.com/centrifuge/liquidity-pools |

대안 — 기본으로 풀리지 않는 특정 문제가 있을 때만:
허브-스포크가 필요하면 [Centrifuge protocol](https://github.com/centrifuge/protocol),
아키텍처 배경은 [Centrifuge 문서](https://docs.centrifuge.io/developer/protocol/overview/),
완결형 대안과 비교하려면 [ERC-3643 (T-REX)](https://github.com/ERC-3643/ERC-3643).

> uRWA 레퍼런스 구현은 **감사받지 않은 교육용**이다. 구조만 참조하고 접근제어·재진입 방어는 §4.6 기준으로 강화한다.

### 4순위 — 그 외

위에서 답을 찾지 못하면 **가장 단순하고 표준적인 방식**을 택하고 `ASSUMPTIONS.md`에 한 줄로 기록한다.
임의로 API를 만들어내지 않는다.

---

## 🗄️ 8) Data Layer (SQLite + better-sqlite3)

### 8.1 역할

오프체인 데이터 계층은 **서비스를 서비스답게 만드는 부분**이다. 온체인만으로는 검색·필터·신청 흐름·알림이 불가능하다.
경계는 §4.3을 엄수한다.

### 8.2 스키마 (요구사항에 따라 가감, 표는 최소 기준)

| 테이블 | 용도 |
|---|---|
| `assets` | 자산 카탈로그. 온체인 `assetId` ↔ 서비스 메타 매핑 |
| `asset_attributes` | 자산 유형별 가변 속성 (JSON Schema 검증 후 저장) |
| `asset_documents` | 문서 메타 + `documentHash` (온체인 해시와 대조) |
| `indexed_events` | 컨트랙트 이벤트 인덱스 (재구축 가능) |
| `sync_state` | 마지막 처리 블록 번호 |
| `applications` | 신청·심사 진행 상태 (최종 판정 결과만 온체인 반영) |
| `notifications` | 사용자 알림 |
| `audit_log` | 운영자 행위 기록 |

- 자산 검색이 필요하면 **FTS5 가상 테이블**을 구성한다.
- 마이그레이션은 번호 붙은 SQL 파일(`001_init.sql`, `002_....sql`)을 순차 적용하는 러너로 구현한다.

### 8.3 인덱서

- 컨트랙트 이벤트를 폴링해 `indexed_events`에 적재하고 `sync_state`를 갱신한다.
- **재시작 안전**: 마지막 처리 블록부터 재개하며, 중복 처리해도 결과가 같아야 한다(멱등).
- **재구축 가능**: `sync_state`를 0으로 되돌리면 전체 리플레이로 동일 상태에 도달해야 한다.
- 체인 재구성(reorg)에 대비해 확정 지연(confirmation depth)을 두고 인덱싱한다.

### 8.4 better-sqlite3 실행 제약 (반드시 지킬 것)

better-sqlite3는 **동기 API의 네이티브 모듈**이다. 아래를 어기면 런타임에 실패한다.

- Next.js Route Handler에서만 사용하고, 해당 파일에 **`export const runtime = "nodejs";`** 를 명시한다.
  **Edge runtime에서 동작하지 않는다.**
- 클라이언트 컴포넌트에서 import하지 않는다. DB 접근은 전부 서버 경로를 경유한다.
- 커넥션은 **싱글턴**으로 관리한다. 개발 모드 HMR로 커넥션이 중복 생성되지 않도록 `globalThis` 캐싱을 쓴다.
- 초기화 시 `db.pragma("journal_mode = WAL")`, `db.pragma("foreign_keys = ON")` 을 설정한다.
- 다중 행 삽입은 `db.transaction(...)`으로 묶는다.
- DB 파일 경로는 환경변수로 주입하고 `.gitignore`에 포함한다.

---

## 📱 9) Required Service Features (End-to-End)

화면은 **사용자 여정 순서**로 구성한다. 아래는 최소 골격이며, §1에 따라 이름과 내용을 서비스 언어로 바꾼다.

### 9.1 랜딩 / 탐색 (지갑 연결 불필요)

- 서비스가 무엇인지 즉시 이해되는 첫 화면
- 자산 카탈로그 — SQLite 기반 검색·필터·정렬·페이지네이션
- 자산 상세 — 메타, 문서, 현재 평가액, 참여 조건. **지갑 없이도 열람 가능**

### 9.2 온보딩

- 지갑 연결 (RainbowKit)
- 자격 제한이 있는 경우: 신청 → 심사 대기 → 승인 상태를 사용자에게 **명확히 표시**
  (온체인에는 판정 결과만, 신청 내역은 SQLite)
- 자격이 불필요한 경우: 이 단계를 만들지 않는다

### 9.3 참여

- 청약 / 구매 / 예치 — 서비스 성격에 맞는 명칭으로
- 실행 전 **사전 검증 결과를 먼저 보여준다** (자격, 한도, 잔고). 트랜잭션을 던져놓고 실패시키지 않는다
- 진행 상태 3단계 시각화 + 완료 후 보유 화면으로 자연스러운 연결

### 9.4 보유 / 수익

- 내 포트폴리오 — 보유 수량, 제약이 있는 경우 **가용 수량을 분리 표시**
- 평가액 추이 (인덱싱된 이벤트 기반)
- 수익·배당 조회 및 수령(claim)
- 거래 내역 (SQLite 인덱스)

### 9.5 이전 / 회수

- 이전(transfer) — 제약이 있는 경우 **실행 전 검증 결과와 차단 사유를 먼저 표시**
- 환매(redeem)
- 차단 시 "무엇이 부족한지"와 "어떻게 해결하는지"를 함께 안내

### 9.6 운영자 백오피스 (부차 화면)

권한 계정만 접근. 자산 등록·발행, 자격 승인, 평가액 갱신, 배당 예치, 감사 로그 조회.
**메인 내비게이션에 노출하지 않는다.**

### 9.7 Diagnostics

배포 주소·연결 상태, 인덱서 동기화 상태(마지막 블록·지연), 최근 트랜잭션 로그(가스·revert 원문),
임의 파라미터에 대한 이전 가능 여부 드라이런.

---

## 🛠️ 10) Environment & Stack Requirements

### 10.1 사전 도구

```bash
node -v                                    # >= v22.10.0
curl -L https://foundry.paradigm.xyz | bash && foundryup
forge --version
```

### 10.2 단일 명령 실행 (필수 산출물)

> [!IMPORTANT]
> **최종 목표는 `yarn dev` 한 번으로 전체 스택이 기동되는 것이다.**
> 사용자가 4개 터미널을 순서 맞춰 여는 방식은 실패로 간주한다.

루트 `package.json`에 아래를 **직접 구현해 출력한다.**

```jsonc
{
  "scripts": {
    "dev": "...",          // 원커맨드. 아래 순서를 보장할 것
    "dev:reset": "...",    // 체인 데이터 + SQLite 삭제 후 dev
    "chain": "...",        // anvil
    "deploy": "...",       // 컨트랙트 배포 + deployedContracts.ts 생성
    "db:migrate": "...",   // SQLite 마이그레이션
    "db:seed": "...",      // 로컬 시연용 시드
    "indexer": "...",      // 이벤트 인덱서
    "test": "..."          // forge test
  }
}
```

`yarn dev` 가 보장해야 할 순서:

```
1. anvil 기동
2. RPC 응답 대기          ← 반드시 대기. sleep 고정값이 아니라 준비 확인
3. 컨트랙트 배포 + deployedContracts.ts 생성
4. SQLite 마이그레이션 + 시드
5. 인덱서 · Next.js dev 서버 병렬 기동
6. 준비 완료 후 접속 URL 출력
```

- 오케스트레이션은 `concurrently` + `wait-on` (또는 동등한 방식)으로 구현하고 **의존 패키지를 `package.json`에 추가**한다.
- **멱등해야 한다.** 재실행해도 깨지지 않는다(마이그레이션은 이미 적용분 skip, 시드는 upsert).
- `npm run dev` 로도 동작해야 한다.
- 종료 시 하위 프로세스를 모두 정리한다(고아 anvil 금지).
- 각 단계 진행 상황을 콘솔에 명확히 출력한다. 조용히 멈춰 있는 구간을 만들지 않는다.

### 10.3 환경변수

`.env.example` 을 제공하고 `.env` 는 `.gitignore` 에 포함한다.
**`.env` 없이도 로컬 기본값으로 `yarn dev` 가 성공해야 한다.** 최초 실행에 수동 설정을 요구하지 않는다.

### 10.4 컨트랙트

Solidity `^0.8.24` / Foundry / OpenZeppelin(`AccessControl`, `ReentrancyGuard` 등 필요한 것만).
테스트는 정상 경로와 **실패 경로를 각각** 검증한다. 제약이 있는 경우 차단 케이스 테스트가 반드시 존재해야 한다.

### 10.5 프론트엔드 / 서버

SE-2 기본 스택을 벗어나지 않는다.

- Next.js (App Router) + TypeScript
- wagmi / viem / RainbowKit (지갑 목록이 번들을 깨면 §10.6 — 로컬 데모는 `injected()` 로 충분)
- Tailwind CSS + daisyUI
- SE-2 훅: `useScaffoldReadContract`, `useScaffoldWriteContract`, `useScaffoldEventHistory`
- SE-2 컴포넌트: `Address`, `Balance`, `AddressInput`, `EtherInput`
- 서버: Next.js Route Handlers (`runtime = "nodejs"`) + better-sqlite3
- **브라우저 스토리지(localStorage/sessionStorage)를 사용하지 않는다.** 상태는 React state / 서버 / 온체인으로 유지한다.
- 컨트랙트 주소는 `deployedContracts.ts`로 자동 연결. **하드코딩 금지.**

### 10.6 알려진 실행 함정 (실측)

아래는 이 프롬프트로 실제 dApp을 만들다 **기동을 막았던** 것들이다. 추측이 아니라 겪은 것이므로
같은 자리에서 다시 멈추지 않도록 미리 처리한다. 다만 환경이 바뀌면 사실도 바뀐다 —
**증상이 안 나타나면 우회를 넣지 않는다.**

| 증상 | 원인 | 처리 |
|---|---|---|
| `yarn install` 이 `better-sqlite3` node-gyp 에서 실패 | 템플릿이 끌어오는 구버전에 현재 Node 용 prebuilt 가 없어 소스 빌드로 떨어진다 | `better-sqlite3` 를 **prebuilt 가 있는 최신 메이저**로 올린다. 설치 후 `node -e "require('better-sqlite3')"` 로 확인 |
| 프론트 번들이 미배포 패키지(`@x402/*` 등) 를 못 찾아 빌드 실패 | RainbowKit 의 지갑 목록이 딸려오는 SDK 를 타고 들어간다 | 로컬 데모는 지갑 목록이 필요 없다 — `@wagmi/core` 의 `injected()` 커넥터만 쓰고, 해결 불가한 선택적 의존은 `webpack.IgnorePlugin` 으로 제외한다 |
| `yarn dev` 가 수 분씩 걸린다 | anvil 에 `--block-time` 을 주면 배포 트랜잭션마다 블록을 기다린다 | 로컬 체인은 **즉시 채굴**로 띄운다(`--block-time` 없이). 로컬은 재구성이 없으므로 인덱서 확정 지연도 0 으로 둔다 |
| 체인을 다시 띄웠는데 인덱서가 멈춘다 | 새 체인의 블록 높이가 저장된 `last_block` 보다 낮다 | 인덱서 기동 시 **헤드 높이와 배포 주소 지문**을 비교해, 되감겼거나 재배포됐으면 색인을 비우고 배포 블록부터 다시 읽는다 |

버전을 이 문서에 못 박지 않는 이유 — 숫자를 적으면 그 숫자가 낡는다. **확인 방법**을 적었으니
설치 후 실제로 확인하고, 막히면 그 사실과 조치를 `ASSUMPTIONS.md` 에 남긴다.

---

## 📦 11) Output Format Contract (Strict — Follow Exactly)

### A) Decision & Service Design

- 선택한 표준(S1~S6)과 판정 근거, 부가 확장 목록
- **§1 사용자 여정 → 화면 매핑표** (여정 단계별로 어떤 화면이 대응하는지)
- 온체인 / SQLite 분담 결정표
- §7 4순위로 결정한 항목 목록

### B) File Tree

### C) Contracts — `--- path: packages/foundry/contracts/... ---`

### D) Tests — `--- path: packages/foundry/test/... ---`

### E) Deploy Scripts

### F) Data Layer — 마이그레이션 SQL, DB 커넥션 모듈, 인덱서, Route Handlers

### G) Frontend — §9의 모든 화면. 페이지·컴포넌트·훅 사용부 전문

### H) Orchestration

루트 `package.json` 전문 + `yarn dev` 오케스트레이션 스크립트 전문 + 시드 스크립트 + `.env.example`

### I) Build & Run Instructions

클론 직후부터 `yarn install` → `yarn dev` → 브라우저 접속까지의 **실제 명령과 예상 콘솔 출력**.
이어서 §1 여정을 그대로 밟는 시연 시나리오(어느 계정으로 무엇을 클릭하면 무엇이 보이는지).
트러블슈팅: Node 버전, foundryup 미설치, 포트 충돌(8545/3000), MetaMask 로컬 네트워크 추가,
고아 anvil 프로세스 정리. **§10.6에서 실제로 겪은 함정은 그 조치까지 함께 적는다** —
빌드하며 우회한 것을 README에 적지 않으면 다음 사람이 같은 자리에서 막힌다.

### J) Acceptance Report

§13의 검증 결과를 항목별로 보고한다.

### K) DECISION.md / ASSUMPTIONS.md / README.md

---

## ✅ 12) Quality Gates (Self-Check Before Final Answer)

**서비스**

- [ ] 첫 화면이 관리자 콘솔이 아니다
- [ ] 지갑 연결 없이 서비스 이해와 자산 탐색이 가능하다
- [ ] 버튼·라벨이 컨트랙트 함수명이 아니라 사용자 언어다
- [ ] §1의 사용자 여정이 실제로 끝까지 완주된다
- [ ] 실패 시 원인과 해결 방법이 함께 노출된다

**표준**

- [ ] §5 판정 결과와 실제 구현 표준이 일치한다
- [ ] 요구사항에 없는 표준·확장을 붙이지 않았다
- [ ] 인터페이스 시그니처를 EIP 원문과 대조했다 (기억에 의존하지 않았다)
- [ ] 제약이 불필요한데 ERC-7943을 넣지 않았다

**컨트랙트**

- [ ] 모듈 인터페이스가 정확히 필요한 개수만 존재한다
- [ ] 온체인 메타데이터 스키마가 없다 (§6.2의 3개 필드만)
- [ ] 분배가 pull 방식이다
- [ ] 업그레이더블 프록시를 쓰지 않았다
- [ ] 자산 종류가 코드·네이밍에 하드코딩되지 않았다
- [ ] `forge test` 전량 통과 구성이다 (실패 경로 테스트 포함)

**데이터**

- [ ] SQLite를 삭제하고 리플레이하면 복원된다
- [ ] 복원 불가능한 데이터가 SQLite에만 있지 않다
- [ ] 개인식별정보가 온체인에 없다
- [ ] better-sqlite3 사용 파일에 `runtime = "nodejs"` 가 있다
- [ ] DB 커넥션이 싱글턴이고 WAL 모드다
- [ ] 모든 쿼리가 prepared statement다
- [ ] 인덱서가 재시작 안전하고 멱등하다

**실행**

- [ ] `yarn dev` **단일 명령**으로 전체 스택이 뜬다
- [ ] `.env` 설정 없이 최초 실행이 성공한다
- [ ] `yarn dev` 재실행이 깨지지 않는다 (멱등)
- [ ] `npm run dev` 로도 동작한다
- [ ] 배포 전에 프론트가 먼저 뜨는 경합이 없다 (RPC 준비 대기 구현됨)
- [ ] 종료 시 하위 프로세스가 정리된다

**공통**

- [ ] `TODO`/`FIXME`/생략 표기가 없다
- [ ] 브라우저 스토리지를 쓰지 않았다
- [ ] 컨트랙트 주소 하드코딩이 없다
- [ ] 개인키·니모닉이 출력물에 없다

---

## 🏁 13) Acceptance Verification (Definition of Done)

> [!IMPORTANT]
> **아래를 전부 통과해야 완료다.** 하나라도 실패하면 원인을 고치고 **해당 게이트부터 다시 실행**한다.
> 실패를 남긴 채 다음으로 넘어가거나, 미검증 상태로 "완료"를 보고하지 않는다.

### A. 클린 실행

```bash
rm -rf node_modules && yarn install
yarn dev
```

**성공 조건 (관찰 가능해야 함)**

1. 콘솔에 anvil 기동 → 배포 → 마이그레이션 → 인덱서 → Next.js 순서가 출력된다
2. 배포 주소가 출력되고 `deployedContracts.ts` 가 생성된다
3. `http://localhost:3000` 이 **에러 없이** 렌더링된다
4. 브라우저 콘솔에 uncaught error가 없다
5. 서버 콘솔에 unhandled rejection이 없다

### B. 여정 완주

지갑 연결 전 상태에서 시작해 §1의 여정을 **끝까지 클릭으로 완주**한다.
각 단계에서 화면이 실제로 바뀌고, 온체인 상태 변경이 목록·잔고에 반영되어야 한다.

### C. 데이터 일관성

```bash
yarn dev:reset      # 체인 + SQLite 전체 초기화 후 재기동
```

초기화 전후로 동일한 화면 상태에 도달해야 한다. (§4.3 재구축 가능성 검증)

### D. 실패 경로

제약이 있는 구성이면 **차단되는 케이스를 의도적으로 실행**해, 트랜잭션이 조용히 실패하지 않고
사람이 읽을 수 있는 사유가 화면에 표시되는지 확인한다.

### E. 테스트

```bash
yarn test
```

전량 통과. 실패 경로 테스트가 포함되어 있어야 한다.

### F. 보고

각 항목의 통과 여부와, 실패했다가 수정한 내역을 요약해 보고한다.
**검증하지 않은 항목을 통과로 표기하지 않는다.**

---

## 🟢 NOW GENERATE THE COMPLETE SERVICE

**PHASE 0부터 순서대로 수행하고, 각 게이트를 통과한 뒤에만 다음 단계로 넘어간다.**
**§5로 판정한 단일 표준에 대해서만 생성한다. Output Format Contract를 엄격히 따른다.**
**§2의 4개 필드에 누락·모순이 없는 한 후속 질문을 하지 않는다.**

**토큰은 인프라다. 서비스를 만들어라. 그리고 `yarn dev` 로 실제로 뜨게 하라.**
