# 개선계획 — 형태소·조사 미스 (particle_glue) + kiwi-dict-authoring 스킬

- 상태: **개정본(2026-07-25)**. 초안이 적대적 리뷰에서 **blocking(착수금지)** 판정을 받아 재작성.
  리뷰 지적을 코드/커밋으로 자체검증한 뒤, 시퀀싱을 뒤집고 근거 수치를 재기준화했다.
- 범위: `data-platform` 검색 품질. Query 빌더 레버(alias · Kiwi 사전 · char-bigram)에 **reranker 축** 추가.
- 관련: `pipeline/build_rag.py`(expand_cjk/build_fts_query/kiwi_lemmas/index_signature),
  `pipeline/reranker.py`(opt-in cross-encoder), `pipeline/aliases.tsv`, `pipeline/eval_queries.json`,
  `pipeline/eval_baseline.json`(+진행중 `eval_rerank_baseline.json`), 스킬 `correction-harvesting`.

> ⚠️ **개정 사유(리뷰에서 확인된 blocking 결함):**
> 1. 성공지표 ≥0.70이 현재 particle_glue 3케이스에서 **산술적으로 도달 불가**(상한 0.6667).
> 2. 초안의 근거 수치가 **폐기된 baseline**을 인용(0.6603/0.75/vector0.3 → 실제 0.6474/0.7083/vector1.0).
> 3. 유일한 관련 실측이 **가설을 반증**: 명사 lemma 추가가 particle_glue를 **−0.056 회귀**시킨 전례.
> 4. **reranker 축을 통째로 누락**. cross-encoder가 q07을 사전보다 먼저 고칠 수 있음.
> 5. 착수 랜드마인: kiwipiepy 사전 파일 포맷, `_get_kiwi` 경로, `smoke_test` 대칭 단언.

---

## P0 실행 결과 (2026-07-26) — 측정 결론: **Kiwi 사전은 이 문제의 레버가 아니다**

P0 진단을 실측(fused + reranked, 모델 캐시 사용)한 결과, 계획의 핵심 가설("q07은 `가상자산사업자`가
과분절돼 실패")이 **의미 있는 수준에서 반증**됐다. 사전을 짓지 않는다(P2 보류).

**q07 `가상자산사업자의 신고 요건` 분해:**
- Kiwi는 실제로 과분절한다: `kiwi_lemmas("가상자산사업자의") = ['가상','자산','사업자']`.
- **그럼에도 keyword arm은 정답 청크(특금법 `제5조(가상자산사업자의 신고…)`)를 `keyword_rank 1`로 이미 최상위**에 둔다.
  bigram/lemma 조합이 `가상자산`을 `디지털자산` 방해청크와 정확히 구별한다 → **토크나이저는 문제의 원인이 아니라 해결자.**
- fused rank 4로 밀리는 원인은 **vector arm**: 상위 1~3위가 `디지털자산주문전송업의 신고`·`인가요건 유지`로 전부
  `keyword_rank: null`(벡터 전용). 벡터가 의미상 이웃 방해청크를 끌어올려 융합을 지배한다.
- **Kiwi 사전은 keyword arm만 바꾼다. 그 arm은 이미 rank 1 → 헤드룸 0.** 복합어를 통째 NNP로 등록해도 q07 융합은 안 움직인다.
- reranker는 이 방해를 실제로 완화(q07 rank 4→2, `by_kind_reranked` particle_glue 0.4167→0.5) — 사전이 아니라 **CE가 레버.**

**particle_glue 3케이스 전부 사전 헤드룸 없음:**

| 케이스 | 상태 | 사전이 고칠 수 있나 |
|---|---|---|
| q07 `가상자산사업자의 신고 요건` | keyword **이미 rank 1**, fused rank 4(벡터 방해) | ❌ (keyword arm 만점, 병목은 벡터/융합) |
| q08 `디지털자산사업자는…` | **이미 rank 1** | ❌ (헤드룸 없음) |
| q09 `증권` | 2자 단일 질의, 양 arm 0.0 | ❌ (`kiwi_lemmas('증권')=['증권']`, 복합어 무관) |

**추가 발견 (사전과 무관, 별건):**
1. **융합 이상** — 정답 청크(vector_rank 2 + keyword_rank 1)가 벡터 전용(rank 10) 청크보다 낮게 융합됨.
   `search_variants`의 variant-level 재융합(`build_rag.py:1036`, 랭크에 대한 무가중 RRF)이 양 arm 강한 청크를
   과소평가하는 것으로 보임 → **q07의 진짜 레버 후보**(별도 조사 필요).
2. **eval 케이스 미스앵커 의심** — 채점 정답이 실질 조문(특금법 `제7조(신고)`)이 아니라 `제5조(…경과조치)`에
   걸림(제목에 "가상자산사업자의 신고" 문자열이 있어서). P1(correction-harvesting) 재앵커 대상.

**결론 / 재라우팅:** 현재 코퍼스·eval에서 **kiwi-dict-authoring 스킬을 짓지 않는다**(측정 헤드룸 0 = default outcome 기각+기록).
실제 레버는 (a) **reranker**(이미 q07 개선; 기본화/`rerank_weight` 튜닝 검토), (b) **융합 재검토**(위 이상 #1),
(c) **P1 eval 재앵커·확대**. 스킬 설계(§6)는 유효하게 보존 — **향후 P1 확대에서 keyword arm이 과분절로 실패하는
케이스가 실제로 나타나면** 그때 착수한다. 아래 §1~§7은 그 재착수 시점을 위한 설계·가드레일로 유지한다.

### 융합 이상 조사 결과 (2026-07-26) — **트레이드오프이지 버그 아님. 변경 미채택.**

위 발견 #1(정답 청크 vrank2+krank1가 벡터 전용 rank10 청크보다 낮게 융합)을 코드 트레이스·계측했다.

**메커니즘:** `가상자산사업자의 신고 요건`이 alias 확장으로 **3 variant**(`가상자산…`/`디지털자산…`/`암호화폐…`)가 된다.
`multi_hybrid_search`(build_rag.py:1032-1046)는 SQL rrf_score를 버리고 **variant 위치 기반 무가중 RRF `Σ 1/(60+pos)`**로 재융합한다.
정답 청크는 자기가 속한 variant(v1·v3)에서 **각각 pos 1**이지만 `디지털자산` variant엔 부재 → 2개 variant 합산.
방해 청크(`디지털자산주문전송업의 신고`)는 3 variant 모두에 mediocre하게 존재 → 합산이 더 큼. **breadth가 depth를 이긴다.**

**측정한 후보 수정(전부 `make eval`/`RERANK=1` per-kind, 코드 env-gate로 가역 실험):**

| 스킴 | particle_glue | synonym_gap | 판정 |
|---|---|---|---|
| sum·alias 1.0 (현행) | 0.417 | 0.444 | baseline |
| sum·alias 0.3 | **0.667** | **0.278** | ❌ synonym −0.166 (게이트 −0.08 초과) |
| max-fusion | 0.333 | 0.500 | ❌ particle −0.084 |
| **reranker(현행 융합 위)** | **0.500** | **0.500** | ✅ 양쪽 무회귀 개선(vocab 0.708→0.833) |
| alias 0.3 + reranker | 0.333(reranked) | 0.500 | ❌ 축소된 pool이 CE에 나쁜 후보 공급 |

**결론:** 두 kind가 구조적으로 상충한다 — particle_glue는 원질의가 지배하길, synonym_gap은 alias variant가 만점으로
세어지길 원한다(정답이 alias variant에만 존재). **어떤 전역 융합 노브도 한 kind를 고치면 다른 kind를 게이트 밖으로 회귀시킨다.**
반면 **reranker는 현행 sum·alias 1.0 pool 위에서 particle_glue·synonym_gap·vocabulary_match를 모두 무회귀로 끌어올린다.**
→ **실험 패치 revert(변경 미채택).** q07의 올바른 레버는 융합 튜닝이 아니라 **reranker**(이미 커밋된 opt-in + floor).
잔여 후보: `가상자산↔디지털자산` alias가 과광범(다른 규제레짐 문서 유입)한지 correction-harvesting로 재검토(별건, 역시 타 kind 위험).

**적용 (2026-07-26): reranker를 이 배포의 기본으로 채택.** `.env`에 `RERANK=1` 추가(코드/`.env.example` 기본은
M1-8GB 위해 OFF 유지 — 병렬 세션의 의도적 설계 불변). 쿼리타임 변경이라 리빌드 불요. 검증: `eval_retrieval
--assert-baseline`이 `eval_rerank_baseline.json` 대비 **무회귀 통과**(arm 0.02 / kind 0.08). reranked 결과:
particle_glue 0.500 · synonym_gap 0.500 · vocabulary_match 0.833 · cross_bill 1.0 · overall 0.718(fused 0.647 대비).
되돌리기: `.env`의 `RERANK` 주석 처리(fused arm으로 복귀, 리빌드 불요). 코드 기본 on 전환(리포지토리 전역)은
모델 부재 시 read path가 hard-raise하므로 **fresh-checkout 안전장치(warm-in-setup 또는 graceful fallback) 선행 필요** — 별건.

---

## 1. 문제 (측정된 사실 — 커밋된 baseline 기준)

한국어는 명사에 조사(의/를/에)·어미가 붙는다. 질의 `"가상자산사업자의 신고"`가 문서의
`"가상자산사업자"`와 깔끔히 정렬되지 않으면 정답 조문이 덜 검색된다.

`eval_baseline.json`(source_sha 360aaac, 15문항) 기준 현재 상태:

| 지표 | 값 |
|---|---|
| **particle_glue MRR@10** | **0.4167** (전체 fused **0.6474** 중 최약체, synonym_gap 0.4444와 동급) |
| 케이스별 | q07 `가상자산사업자의 신고 요건` → **rank 4(MRR 0.25)** · q08 `디지털자산사업자는…` → **rank 1** · q09 `증권` → **miss(0.0)** |
| 참고 kind | synonym_gap 0.4444(3) · vocabulary_match **0.7083**(4) · cross_bill 1.0(3) · negative 2/2 |
| 설정 | KIWI_MORPH=**1(ON)** · EMBEDDING onnx_int8 · **vector_weight 1.0 / keyword 1.0** · rerank_enabled **false** |
| eval 케이스 | particle_glue **3개뿐** → MRR이 0.33 단위로 튐(측정 노이즈 큼) |

**산술 상한(리뷰가 짚고 내가 확인):** 3케이스 중 q08은 이미 rank1(1.0, 개선여지 0), q09는 조사문제가
아니라 2자 단일명사(`증권`) 모호성 — `kiwi_lemmas("증권")=['증권']`, **사전으로 해결 불가**.
따라서 사전이 q07을 rank1로 올려도 particle_glue 최대치 = (1.0+1.0+0.0)/3 = **0.6667 < 0.70**.
**즉 현재 3케이스 위에서는 목표 0.70이 산술적으로 불가능하다.** → 목표 재정의(§2) + eval 확대 선행(§4).

**핵심 진단(가설, P0에서 검증):** Kiwi가 켜진 상태에서도 약세다. q07(rank4)은 `가상자산사업자`가
통복합어로 안 잡혀 과분절(`가상`/`자산`/`사업자`)된 것으로 보인다. 다만 이 진단은 **미검증 가설**이며,
아래 반증 증거와 대안(reranker)을 먼저 배제해야 한다.

## 2. 목표 & 성공지표 (재정의)

초안의 "particle_glue ≥0.70"은 3케이스에서 산술 불가 → **폐기**. 대신:

- **선행: eval 확대** — particle_glue를 heading-anchored로 **3 → 8~10 케이스**로 늘려(§4 P1)
  0.33-단위 노이즈를 없앤 뒤에만 개선 목표를 수치화한다.
- **q07 교정** — `가상자산사업자의 신고` 정답 조문을 **fused arm AND reranked arm 양쪽에서** rank↑
  (rank4→rank1 지향). 단일 arm 개선은 불충분(§3 이중 arm).
- **무회귀(이중)** — (a) 타 kind 무회귀(synonym_gap/vocabulary_match/cross_bill/negative),
  그리고 (b) **particle_glue 자체 무회귀** — §3의 −0.056 전례를 반드시 반증할 것.
- 모든 변경은 `make eval` per-kind 표로 측정하고 `make verify`(arm −0.02 / kind −0.08 게이트) 통과.
  reranker 변경은 `make eval-rerank`(fused vs reranked). baseline 재기록은 측정 근거 + `source_sha`와 함께만.

## 3. 현재 메커니즘 (Query 빌더 + reranker)

- **char bigram backbone** — `expand_cjk`(write)와 `build_fts_query`(query)가 CJK run을 동일한
  문자 n-gram으로 확장(FTS_NGRAM_SIZE). **write=query 대칭**이 깨지면 keyword arm이 조용히 0 →
  `index_signature`가 loud error로 잡음. 부분매치 안전망이라 함부로 못 건드림.
  (사전을 통째 등록해도 `사업자`/`자산` 부분질의는 bigram으로 계속 매칭됨 — lemma는 대체 아닌 OR 추가.)
- **Kiwi additive noun lemma** — `kiwi_lemmas()`가 NNG/NNP/XR·≥2자·CJK 명사 원형을 write/query 양쪽에
  **OR 항으로 추가**(대체 아님). KIWI_MORPH off면 no-op. `index_signature`에 kiwi 슬롯 있음.
  ⚠️ **반증 증거(eval_retrieval.py:16-22, 실측 주석):** 현 onnx_int8/vector1.0 체제에서 additive
  Kiwi lemma는 keyword +0.039, fused 0.654→0.692로 **전반은 개선**하지만 *particle_glue는 −0.056 회귀*
  ("신고/자산 같은 빈출 lemma가 매치를 넓혀서"). **즉 명사 lemma 추가 자체가 이 kind를 악화시킨 전례가 있다.**
  계획의 기대("복합어는 rare/high-IDF라 broaden이 아니라 pin down")는 **미측정 가설** — P2에서 이 전례를
  명시적으로 반증하지 못하면 사전을 채택하지 않는다. (빈출 복합어일수록 IDF가 낮아 pin 효과가 약해지는 역설.)
- **weighted RRF** — **vector 1.0 / keyword 1.0**(등가; onnx_int8 벡터 arm이 강해 0.3→1.0으로 상향된 것,
  `.env:105-112`). *초안의 "vector 0.3"은 hashing embedder 시절 폐기 값이므로 삭제.*
  주의: 사전/lemma/bigram은 **키워드 arm(bm25)만** 바꾼다. 벡터 arm은 raw text를 트랜스포머로 인코딩하므로
  `expand_cjk`/lemma를 타지 않는다 → 사전의 효과 범위는 keyword arm에 국한(효과 규모 과대평가 금지).
- **opt-in cross-encoder reranker**(신규, `reranker.py`, 커밋 79c444e, default off) — `bge-reranker-v2-m3`가
  query-doc를 직접 채점해 후보를 재정렬. RERANK=1일 때 `hybrid_search`가 `fused`와 `reranked` 두 arm을
  동시 산출. **q07 같은 랭킹 오류는 CE가 지배 신호로 재정렬할 가능성이 높다** → 사전 착수 전 반드시 선측정(§4 P0.5).
  reranked arm의 per-kind floor는 별도(`by_kind_reranked`, `make eval-rerank-baseline`).
  🔧 현재 다른 세션이 이 floor를 위한 **`eval_rerank_baseline.json` 분리**를 작업 중(uncommitted) — 정착 후 활용.
- **alias/excluded** — `aliases.tsv`(코퍼스 마이닝, invented 금지) / `excluded.tsv`(기각 사유 기록).

## 4. 개선 단계 (측정 선행 — 시퀀싱 역전)

> 초안은 "P1 사전이 가장 값싸다"며 사전을 먼저 뒀으나, 3케이스·산술상한·−0.056 전례 위에서
> **측정 없이 사전 튜닝**은 계획 스스로 §7에서 경계한 안티패턴이다. eval 확대와 reranker 측정을 **앞으로** 옮긴다.

### Phase 0 — 진단 (코드변경 0, 측정만)
- 실패 원인 분해: `make query Q="가상자산사업자의 신고 요건" --candidates 40 --full-content`를
  **fused arm과 `RERANK=1` reranked arm 둘 다** 실행. 정답 조문의 후보 포함/랭크·keyword vs vector 기여 분해.
- `kiwi_lemmas("가상자산사업자의")` 실제 분절 직접 확인(현재 `['가상','자산','사업자']`로 관측됨).
- 산출: 실패를 **오분절 / bigram 과매칭 / 진짜 synonym gap / 나쁜 eval 케이스**로 분류.
  ⚠️ reranked arm에서 q07이 **이미 rank1**이면 사전 작업의 편익은 opt-in 경로에서 소멸 → 그 사실을 문서화하고 P2 보류.

### Phase 0.5 — 기준 재설정 + reranker floor (최우선, 코드변경 0~소)
- 이 계획의 모든 근거 수치를 커밋된 baseline(fused 0.6474 / vocab 0.7083 / vector 1.0)으로 고정(완료: §1).
- 다른 세션의 `eval_rerank_baseline.json` 분리가 커밋·정착하면 `make eval-rerank-baseline`으로
  **reranked per-kind floor 기록**. reranked particle_glue를 확인 — ≥ 목표면 P2 편익 소멸을 명문화.
- 산출: 이후 모든 단계가 fused **및** reranked 이중 게이트로 판정됨을 확정.

### Phase 1 — eval 확대 (correction-harvesting 규율, 사전보다 먼저)
- `correction-harvesting` 스킬로 각 미스를 **heading-anchored eval 케이스**로 고정,
  particle_glue **3 → 8~10개**로 확대(측정 신뢰도↑, 0.33-단위 노이즈 제거).
- 반드시 포함할 **회귀 프로브**: `증권`류 단일토큰 모호성 케이스, `사업자` 단독 부분질의(사전이 부분매치를
  깨지 않음을 지킴). `make eval-baseline` 재기록(+source_sha, 측정 근거).
- 이 단계가 끝나야 §2의 개선 목표를 비로소 수치화할 수 있다.

### Phase 2 — Kiwi 도메인 사전 보강 (오분절 교정 — **정당성 입증된 경우에만**)
- 전제(모두 충족 시에만 착수): (a) P0가 q07의 주원인을 **오분절**로 지목, (b) reranked arm이 q07을
  자동으로 고치지 **못함**, (c) P1으로 eval이 확대되어 측정이 신뢰 가능.
- 코퍼스에서 Kiwi가 과분절하는 **빈출** 법령 복합어를 마이닝(가상자산사업자·가상자산이용자보호법·
  디지털자산사업자·투자계약증권 등)해 Kiwi user-dictionary에 NNP 등록. **임의 창작 금지 — 코퍼스 근거·빈도만.**
  채굴 임계: **doctype-profile-authoring Step0 승계 — 대략 5회 이상 등장**하는 복합어만 승격, 그 외 스크래치패드/기각.
- write·query 동일 사전, `index_signature`에 **사전 해시** 포함 → 사전 변경 시 리빌드 강제.
- 측정: 각 후보 전/후 **fused 및 reranked** per-kind. particle_glue↑ **AND** 무회귀(§2 이중)만 채택.
  **−0.056 전례를 명시적으로 반증**(사전 lemma가 broaden이 아니라 pin으로 작동함을 수치로). 실패분은 `kiwi_excluded.tsv`.
- ⭐ 이 단계를 **`kiwi-dict-authoring` 스킬**로 거버넌스화(§6).

### Phase 3 — 조사 정규화 / bigram 과매칭 억제 (최후, 신중)
- 조사 char가 만드는 bigram(자의·자는) 노이즈 완화. Kiwi가 josa(JKS/JKO/JKB)를 이미 분리하므로,
  Kiwi lemma가 있을 때 원토큰 꼬리 bigram 기여를 낮추는 실험 등.
- ⚠️ bigram backbone은 synonym/부분매치 안전망 → **제거 금지**. write=query 대칭 깨지면 금지.

---

## 5. 시퀀싱 & 수용기준

P0 진단(fused+reranked) → **P0.5 기준재설정+reranker floor** → **P1 eval 확대** →
(오분절이 주원인 & reranker가 못 고칠 때만) **P2 Kiwi 사전** → 필요 시 P3.

각 단계 수용: **fused AND reranked 양 arm에서 particle_glue↑ AND 타 kind 무회귀 AND particle_glue 자체
무회귀(−0.056 반증) AND `make verify` 통과.** 아니면 롤백 + `excluded` 기록(default outcome = 기각+기록).

## 6. kiwi-dict-authoring 스킬 설계

### 6.1 왜 스킬인가 — 기존 선례와 동형
프로젝트 원칙: **LLM이 커밋 데이터를 오프라인 저작 → 게이트 측정 → 리뷰 → 커밋. 빌드는 모델 호출 안 함.**
Kiwi는 **빌드 안의 결정론적 라이브러리**(LLM 아님), 사전은 **커밋 데이터** → 완전 정합.
correction-harvesting은 miss-반응형(default=기각)이라 **범주가 다름** → 흡수 금지, 별도 스킬이 맞다.

| 기존 스킬 | 저작 대상 | 게이트 | 산출물 |
|---|---|---|---|
| `doctype-profile-authoring` | 파싱 정규식 프로파일 | `make gate` | `pipeline/doctypes/*.py` |
| `legal-schema-authoring` | 관계-가중치·노드-kind | `make eval-graph` | committed diff |
| `correction-harvesting` | 검색 miss→eval, alias | `make eval` | `aliases.tsv`/`excluded.tsv` |
| **`kiwi-dict-authoring`(신규)** | **법령 복합어 사전** | **`make eval`+`make eval-rerank`(particle_glue)** | **`pipeline/kiwi_userdict.tsv`** |

### 6.2 저작 워크플로우 (거버넌스)
1. **채굴(mine)** — 코퍼스에서 Kiwi가 과분절하는 **≥5회 등장** 복합어 후보 추출(빈도 + 현재 `kiwi_lemmas` 분절 대조).
   임의 창작 금지 — 코퍼스 근거·빈도만(aliases.tsv 원칙 승계). 임계 미달은 스크래치패드 일회성/기각.
2. **제안(propose)** — 각 후보에 "코퍼스 등장 위치(rel_path)·빈도 + 현재 분절 + 등록 기대효과" 첨부.
   사인오프는 **git PR diff 리뷰**(이 프로젝트 authoring 산출물의 표준). hitl-review 서버는 *선택 부가*(권장 아님 —
   ~10-20행 사전에 브라우저 서버는 과설계; doctype/legal-schema/correction-harvesting 모두 git-diff로 사인오프).
3. **측정(gate)** — 후보 전/후 `make eval` **및** `make eval-rerank` per-kind. particle_glue↑ AND 무회귀(§2 이중)만 채택.
   나머지는 `kiwi_excluded.tsv`에 사유 기록(**default = 기각 + 기록**).
4. **커밋 + 리빌드** — 채택분을 `pipeline/kiwi_userdict.tsv`에 커밋. 사전 해시를 `index_signature`에
   포함 → 리빌드 강제(write=query 대칭 보장).

### 6.3 필요한 코드 훅 (작음, 1회) — 착수 전 확정할 랜드마인 포함
- `_get_kiwi()`: `Kiwi()` 뒤 `kiwi.load_user_dictionary(<path>)`로 사전 로드.
  🔧 **경로:** 상대경로 금지(cwd 의존으로 tools/agent 경로에서 깨짐). `Path(__file__).parent / "kiwi_userdict.tsv"`
  절대경로 사용. **사전 부재 시 graceful**(로드 스킵·빈 해시) — 안 그러면 KIWI-on 기본 경로가 죽는다.
- `_kiwi_signature()`: 현재 `kiwi-add-{model_version}`뿐 → **사전 파일 해시 추가**(동일 절대경로 기준).
  사전 변경이 시그니처에 안 잡히면 리빌드 누락.
- 🔧 **`smoke_test.py:471-479` 대칭 단언 처리:** `queried ⊆ indexed`는 **bigram 채널에서만 참**이고 lemma
  채널에서는 거짓(Kiwi 분절은 문맥의존적). 채굴이 `가상자산`(4자)을 NNP 통등록하면 `kiwi_lemmas("가상자산")=['가상자산']`가
  query 항에 들어가 subset 단언이 red가 된다(검색은 무해 — OR 추가라). 착수 전 (a) 그런 통등록 회피 규칙을
  6.2에 명문화, 또는 (b) 단언을 bigram 채널로 한정하도록 수정. **테스트를 무심코 삭제 금지.**

### 6.4 산출 파일 (다른 authoring 스킬과 구조 일치)
```
.agents/skills/kiwi-dict-authoring/SKILL.md   (+ .claude/skills 심링크)
pipeline/kiwi_userdict.tsv     # load_user_dictionary가 직접 먹는 파일
pipeline/kiwi_excluded.tsv     # 등록 거부한 후보 + 사유 (excluded.tsv 철학)
```
🔧 **파일 포맷(kiwipiepy 0.23.2 검증 필요):** `load_user_dictionary` 표준 포맷은 `형태<TAB>품사<TAB>점수`
(3열, `#` 주석 허용). **provenance(rel_path)를 4번째 데이터 열로 넣으면 로더가 거부/오파싱할 개연성** →
provenance는 **`#` 주석 열** 또는 **사이드카 파일**(`kiwi_excluded.tsv` 패턴)로 분리. 실기기에서 로드 검증 후 확정.

### 6.5 대안(기타방법)과 비교
- **순수 자동 채굴 스크립트**(`make kiwi-mine`) — 빠르나 측정·리뷰 없는 invented 금지 위반, 과다등록→회귀
  위험 → **스킬 안의 채굴 단계로만** 쓰고 단독 채택 금지.
- **hitl-review 연동** — 선택 부가(권장 아님, §6.2). git PR diff가 표준 사인오프.
- **correction-harvesting에 흡수** — 범주 오류(그건 miss-반응형·default=기각). **형태소류 miss → 이 신규 스킬로
  라우팅**하게 correction-harvesting SKILL에 한 줄 추가(각 레버 = 스킬 하나, 프로젝트 관습).

## 7. 가드레일 & 실행 전제

- **이중 arm 게이트** — 사전/쿼리 변경은 **fused 및 reranked** 양쪽에서 particle_glue↑ AND 무회귀.
  reranked floor는 `by_kind_reranked`(`make eval-rerank-baseline`)로 별도 기록.
- **write=query 대칭·리빌드**(`index_signature`) — 쿼리 로직/사전 바꾸면 리빌드. bigram backbone 제거 금지.
  smoke_test 대칭 단언(§6.3) 준수.
- **negative 케이스 유지**(오답 안 나와야). **baseline은 측정+SHA로만** 갱신(폐기 baseline 인용 금지 — §3).
- **거버넌스**: alias/사전은 코퍼스 마이닝·측정·리뷰만. invented 금지, default outcome = 기각+기록.
- **동시성**: 측정 루프(`make query`/`make eval`/`make eval-rerank`)·리빌드·코드 훅 커밋은
  **파이프라인이 조용할 때(단일 세션)**. 두 세션이 같은 `.venv`·DuckLake·SQLMesh를 공유하면 충돌.
  (현재 다른 세션이 `eval_rerank_baseline.json`을 작업 중 — 정착 전 측정/리빌드 금지.)

## 8. 참고
- 규범: `AGENTS.md`("Synonymy is not a tokenizer problem", "Measuring retrieval", "Document-type profiles").
- 스킬: `.agents/skills/{correction-harvesting,doctype-profile-authoring,legal-schema-authoring,hitl-review}/`.
- 코드: `pipeline/build_rag.py`(`_cjk_ngrams`/`expand_cjk`/`kiwi_lemmas`/`build_fts_query`/`index_signature`),
  `pipeline/reranker.py`, `pipeline/eval_retrieval.py`(header 실측 주석), `pipeline/smoke_test.py`(471-479).
- 실측 근거: `eval_baseline.json`(fused 0.6474 / particle q07 0.25·q08 1.0·q09 0.0 / vocab 0.7083 / vector 1.0),
  `.env:105-112`(VECTOR_WEIGHT=1.0), `eval_retrieval.py:16-22`(additive lemma particle_glue −0.056).
