# 개선계획 — 형태소·조사 미스 (particle_glue) + kiwi-dict-authoring 스킬

- 상태: 초안(2026-07-25). 미착수 — 측정 기반으로 단계 진행.
- 범위: `data-platform` 검색 품질. Query 빌더 3레버(alias · Kiwi · char-bigram).
- 관련: `pipeline/build_rag.py`(expand_cjk/build_fts_query/kiwi_lemmas), `pipeline/aliases.tsv`,
  `pipeline/eval_queries.json`, `pipeline/eval_baseline.json`, 스킬 `correction-harvesting`.

---

## 1. 문제 (측정된 사실, 추측 아님)

한국어는 명사에 조사(의/를/에)·어미가 붙는다. 질의 `"가상자산사업자의 신고"`가 문서의
`"가상자산사업자"`와 깔끔히 정렬되지 않으면 정답 조문이 덜 검색된다.

`eval_baseline.json` 기준 현재 상태:

| 지표 | 값 |
|---|---|
| **particle_glue MRR@10** | **0.4167** (전체 fused 0.6603 중 최약체, synonym_gap 0.4444와 동급) |
| 케이스별 | q07 `가상자산사업자의 신고 요건` → **rank 4** · q08 `디지털자산사업자는 어떤 신고를…` → rank 1 · q09 `증권` → **miss(top10 밖)** |
| KIWI_MORPH | **=1 (이미 ON)** · EMBEDDING onnx_int8 · weighted RRF(vector 0.3) |
| eval 케이스 | particle_glue **3개뿐** → MRR이 0.33 단위로 튐(측정 노이즈 큼) |

**핵심 진단:** Kiwi가 켜진 상태에서도 약세다. 즉 "Kiwi를 켜면 해결"이 아니라,
**Kiwi가 켜진 채로도 (a) 법령 복합어를 과분절하고 (b) 조사 유래 bigram이 과매칭**해서
정답 복합명사가 상위로 못 올라온다. q07(rank 4)은 `가상자산사업자`가 통복합어로 안 잡혀 보임.
q09(`증권`)는 조사 문제라기보다 1토큰 과매칭/모호성 — 별도 취급.

## 2. 목표 & 성공지표

- **particle_glue MRR@10: 0.42 → ≥0.70**, 그리고 **타 kind 무회귀**
  (synonym_gap 0.44 / vocabulary_match 0.75 / cross_bill 1.0 / negative 유지).
- 모든 변경은 `make eval` **per-kind 표**로 측정하고 `make verify`
  (arm −0.02 / kind −0.08 게이트) 통과. baseline 재기록은 측정 근거 + `source_sha`와 함께만.

## 3. 현재 메커니즘 (Query 빌더)

- **char bigram backbone** — `expand_cjk`(write)와 `build_fts_query`(query)가 CJK run을 동일한
  문자 n-gram으로 확장(FTS_NGRAM_SIZE). **write=query 대칭**이 깨지면 keyword arm이 조용히 0 →
  `index_signature`가 이를 loud error로 잡음. 부분매치의 안전망이라 함부로 못 건드림.
- **Kiwi additive noun lemma** — `kiwi_lemmas()`가 NNG/NNP/XR·≥2자·CJK 명사 원형을 추출해
  write/query 양쪽에 **OR 항으로 추가**(대체 아님). `사업자의 → 사업자` 조사 브리지. KIWI_MORPH off면 no-op.
  `index_signature`에 kiwi 슬롯 있음(Kiwi-off 인덱스를 Kiwi-on으로 질의 방지).
- **weighted RRF** — vector 0.3 / keyword. (코드 주석: 과거 45문항 측정에서 vector 0.3이 MRR
  0.7063→0.7251, particle 0.9048→1.0. 지금 15문항 세트·확장 코퍼스에선 particle 0.4167 — 측정 세트 차이.)
- **alias/excluded** — `aliases.tsv` 23행(코퍼스에서 마이닝, invented 금지) / `excluded.tsv` 9행(기각 사유 기록).

## 4. 개선 단계 (각 단계에 측정 게이트)

### Phase 0 — 진단 (코드변경 0, 측정만)
- 각 실패의 원인 분해: `make query Q="가상자산사업자의 신고 요건" --candidates 40 --full-content`로
  정답 조문의 후보 포함/랭크·keyword vs vector arm 기여 확인. `kiwi_lemmas("가상자산사업자의")`가
  실제 어떻게 쪼개는지 직접 확인.
- 산출: 실패를 **오분절 / bigram 과매칭 / 진짜 synonym gap / 나쁜 eval 케이스**로 분류 → 우선순위 확정.

### Phase 1 — Kiwi 도메인 사전 보강 (오분절 교정, 가장 직접적)
- 코퍼스에서 **Kiwi가 과분절하는 빈출 법령 복합어**를 마이닝(가상자산사업자·가상자산이용자보호법·
  디지털자산사업자·투자계약증권 등)해 Kiwi user-dictionary에 NNP 등록. **임의 창작 금지 — 코퍼스 근거·빈도만.**
- write·query 동일 사전, `index_signature`에 **사전 해시** 포함 → 사전 변경 시 리빌드 강제.
- 측정: 사전 전/후 per-kind. particle_glue↑ **AND** 무회귀만 채택.
- ⭐ 이 단계를 **`kiwi-dict-authoring` 스킬**로 거버넌스화(§6).

### Phase 2 — 조사 정규화 / bigram 과매칭 억제 (신중)
- 조사 char가 만드는 bigram(자의·자는) 노이즈와 짧은 질의 과매칭 완화. Kiwi가 josa(JKS/JKO/JKB)를
  이미 분리하므로, Kiwi lemma가 있을 때 원토큰 꼬리 bigram 기여를 낮추는 실험 등.
- ⚠️ bigram backbone은 synonym/부분매치 안전망 → **제거 금지**. per-kind로 synonym_gap 무회귀 확인.
  write=query 대칭 깨지면 금지.

### Phase 3 — eval 확대 + alias 타겟 (correction-harvesting 규율)
- `correction-harvesting` 스킬로 각 미스를 **heading-anchored eval 케이스**로 먼저 고정,
  particle_glue 세트 3→N개로 확대(측정 신뢰도↑).
- **alias는 synonym gap이 증명될 때만** — 조사 문제는 tokenizer 문제지 synonym 아님(AGENTS.md).
  대부분 P1/P2로 해결, 안 되는 것만 alias, 나머지는 `excluded.tsv`에 사유 기록(default outcome).

---

## 5. 시퀀싱 & 수용기준

P0 진단 → (오분절이 주원인 확인 시) **P1 Kiwi 사전이 가장 값싸고 효과 큼** → P3 eval 확대 → 필요 시 P2.
각 단계 수용: **particle_glue↑ AND 타 kind 무회귀 AND `make verify` 통과.** 아니면 롤백 + `excluded` 기록.

## 6. kiwi-dict-authoring 스킬 설계

### 6.1 왜 스킬인가 — 기존 선례와 동형
프로젝트 원칙: **LLM이 커밋 데이터를 오프라인 저작 → 게이트 측정 → 리뷰 → 커밋. 빌드는 모델 호출 안 함.**
Kiwi는 **빌드 안의 결정론적 라이브러리**(LLM 아님), 사전은 **커밋 데이터** → 완전 정합.

| 기존 스킬 | 저작 대상 | 게이트 | 산출물 |
|---|---|---|---|
| `doctype-profile-authoring` | 파싱 정규식 프로파일 | `make gate` | `pipeline/doctypes/*.py` |
| `legal-schema-authoring` | 관계-가중치·노드-kind | `make eval-graph` | committed diff |
| `correction-harvesting` | 검색 miss→eval, alias | `make eval` | `aliases.tsv`/`excluded.tsv` |
| **`kiwi-dict-authoring`(신규)** | **법령 복합어 사전** | **`make eval`(particle_glue)** | **`pipeline/kiwi_userdict.tsv`** |

### 6.2 저작 워크플로우 (거버넌스)
1. **채굴(mine)** — 코퍼스에서 Kiwi가 과분절하는 빈출 복합어 후보 추출(빈도 + 현재 `kiwi_lemmas` 분절 대조).
   임의 창작 금지 — 코퍼스 근거·빈도만(aliases.tsv 원칙 승계).
2. **제안(propose)** — 각 후보에 "코퍼스 등장 위치 + 현재 분절 + 등록 기대효과" 첨부. (선택)
   **`hitl-review` 서버로 사람 사인오프** — 기존 인프라 재사용("새 producer 매니페스트 리뷰"에 해당).
3. **측정(gate)** — 후보 전/후 `make eval` per-kind. particle_glue↑ AND 무회귀만 채택.
   나머지는 `kiwi_excluded.tsv`에 사유 기록(default = 기각 + 기록).
4. **커밋 + 리빌드** — 채택분을 `pipeline/kiwi_userdict.tsv`에 커밋. 사전 해시를 `index_signature`에
   포함 → 리빌드 강제(write=query 대칭 보장).

### 6.3 필요한 코드 훅 (작음, 1회)
- `_get_kiwi()`: `Kiwi()` 뒤에 `kiwi.load_user_dictionary('pipeline/kiwi_userdict.tsv')`로 커밋 사전 로드.
- `_kiwi_signature()`: 현재 `kiwi-add-{model_version}`뿐 → **사전 파일 해시 추가**(안 그러면 사전 변경이
  시그니처에 안 잡혀 리빌드 누락 위험).

### 6.4 산출 파일 (다른 authoring 스킬과 구조 일치)
```
.agents/skills/kiwi-dict-authoring/SKILL.md   (+ .claude/skills 심링크)
pipeline/kiwi_userdict.tsv     # 커밋: word\tPOS\tscore\t근거(코퍼스 rel_path)
pipeline/kiwi_excluded.tsv     # 커밋: 등록 거부한 후보 + 사유 (excluded.tsv 철학)
```

### 6.5 대안(기타방법)과 비교
- **순수 자동 채굴 스크립트**(`make kiwi-mine`) — 빠르나 "측정·리뷰 없이 invented" 금지 위반, 과다등록→회귀
  위험 → **스킬 안의 채굴 단계로만** 쓰고 단독 채택 금지.
- **hitl-review 연동** — 제안 엔트리 사람 승인 후 커밋. **권장 옵션**.
- **correction-harvesting에 흡수** — 그 스킬은 alias/excluded 라우팅용. **형태소류 miss → 이 신규 스킬로
  라우팅**하게 correction-harvesting에 한 줄 추가(각 레버 = 스킬 하나, 프로젝트 관습).

## 7. 가드레일 & 실행 전제

- **write=query 대칭·리빌드**(`index_signature`) — 쿼리 로직/사전 바꾸면 리빌드. bigram backbone 제거 금지.
- **negative 케이스 유지**(오답 안 나와야). **baseline은 측정+SHA로만** 갱신.
- **거버넌스**: alias/사전은 코퍼스 마이닝·측정·리뷰만. invented 금지, default outcome = 기각+기록.
- **동시성**: 측정 루프(`make query`/`make eval`)·리빌드·코드 훅 커밋은 **파이프라인이 조용할 때(단일 세션)**.
  두 세션이 같은 `.venv`·DuckLake·SQLMesh를 공유하면 충돌.

## 8. 참고
- 규범: `AGENTS.md`("Synonymy is not a tokenizer problem", "Measuring retrieval", "Document-type profiles").
- 스킬: `.agents/skills/{correction-harvesting,doctype-profile-authoring,legal-schema-authoring,hitl-review}/`.
- 코드: `pipeline/build_rag.py` (`_cjk_ngrams`/`expand_cjk`/`kiwi_lemmas`/`build_fts_query`/`index_signature`).
