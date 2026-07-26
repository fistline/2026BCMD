# 이중색인 방지 — 인박스 쌍둥이 퍼지 탐지 (연구·설계·적대검증·실측)

- 상태: **탐지기 SQL 구현·검증 완료(2026-07-27). 착지(모델 배치+documents.sql 배선)는 다른 세션의 연속 빌드로 유휴 창이 없어 대기** — 자동-롤백 착지 스크립트가 32분간 안정 유휴를 못 찾고 무해 중단. 검증된 SQL은 §4/§5b에 turnkey. 착지엔 빌드 창(다른 세션 일시정지)만 필요.
- **doc_type=format 교정(중요)**: silver `doc_type`은 프로파일(bill/statute)이 아니라 **포맷**('hwp'/'pdf'/'txt')이라 "doctype동일" 게이트는 "포맷상이"와 자기모순 → **폐기**. 판별자는 포맷상이 + 길이비<0.15 + Jaccard≥0.85. 미래의 cross-format 다른서류(예: 증권신고서.pdf vs 투자설명서.hwp)는 프로파일 게이트 부재로 이론상 미차단이나, **비파괴(document_twins 기록 + bronze 보존)로 복원가능** + 현 코퍼스엔 cross-format 다른서류 쌍 부재(측정 최대 0.610).
- **검증 완료(합성 DuckDB)**: SQL 문법 정상, 쌍둥이 검출(loser=pdf→winner=hwp), 무관문서 오병합 0, **결정성 ✓(최종 ORDER BY doc_id)**, 집합 Jaccard라 hash() 미사용. 현 코퍼스 실빌드는 **no-op**(큐레이션 쌍둥이는 `_superseded_renditions`가 seed에서 pdf 미시딩 → bronze에 쌍둥이 없음; 탐지기는 인박스 직접투입에서만 발동). 성능 유계(대형 same-format 문서는 길이비 게이트로 후보 제외).
- 스테이징 파일(적용 대기): `scratchpad/document_twins.sql`(신규 모델), `scratchpad/documents.sql`(LEFT JOIN + `AND tw.doc_id IS NULL`). build_rag는 **미변경**(그들 reranker 작업 충돌 회피 — loser는 silver.documents에서 빠지면 downstream INNER JOIN으로 자동 제외).
- 흐름: 최신 방법론 조사 → 개선계획 → 적대적 검증 → **실측 교정** 순.
- 대상 갭: **인박스 직접투입 쌍둥이**(이름 불일치, 큐레이션 없음). `source/` 큐레이션 쌍둥이는 이미 `watcher._superseded_renditions`가 처리. `content_fingerprint`(정확 일치)는 HWP/PDF 추출 선형화 차이로 **0/10** 실패.

## 1. 연구 결론 (2024–2026 표준)
LLM 코퍼스 dedup(FineWeb·SlimPajama·datatrove) 표준 = **문자 w-shingle + Jaccard**. 이 코퍼스의 실패모드(추출기 블록순서 차이·공백불일치·한국어 비공백구분)에 정확히 부합:
- **문자 w-그램(w=10)을 NFC·전체공백제거 문자열에 적용** → 전역순서 무시(블록 재배치 무영향), 국소순서 유지(애너그램 거부).
- **MinHash/SimHash는 후보 blocker일 뿐**, 결정은 **정확 Jaccard**. (코퍼스가 작아 blocker 불필요 → 직접 검증 self-join.)
- **파괴적 MinHash는 폐기**(적대검증): DuckDB 1.5.4 affine 곱 오버플로 + `hash()` 버전 비안정 → uv sync 후 결과 변동. 집합 Jaccard는 순수 집합연산이라 **결정성 airtight**.

## 2. 적대적 검증이 기각한 것 (실측 근거)
설계 초안의 **파괴적 붕괴 + T≥0.999**는 FATAL 기각:
- **[기재정정] 증권신고서 vs 원본 = J 0.999**, 한 글자 정정 = 0.999 → 정정·개정본이 포맷 쌍둥이보다 **더 유사**. 파괴적 붕괴는 버전이력을 삭제.
- 증권신고서 vs 투자설명서(같은 회사·다른 법적서류) = 0.955 → 오병합 위험.
- 하니스가 `source/` stem-쌍둥이만 측정 = `_superseded_renditions`가 이미 제거하는 것 → **실제 갭 미측정**.

## 3. 실측 교정 (이 프로젝트 실데이터, `pipeline.extract` + 문자 w-shingle)
| 집합 | Jaccard |
|---|---|
| 양성: cross-format 같은문서 쌍둥이(hwp↔pdf) | **0.910 – 0.948** (n=10) |
| 음성: cross-format 다른문서(hwp_i × pdf_j) | **최대 0.610** (n=90) |
| **마진** | **+0.300** |

- **워크플로우의 T≥0.999는 틀렸다** — 진짜 쌍둥이가 0.91–0.95라 0/10 병합(no-op). 올바른 값은 **T≈0.85**(양성 하한 0.910 아래, 음성 상한 0.610 위): 양성 10/10 병합, 음성 0/90 오병합.
- **결정적 통찰: Jaccard 축만으로는 분리 불가**(정정 0.999 > 쌍둥이 0.93 > 다른서류 0.955, 역전·중첩). 그러나 FATAL 음성은 전부 **같은 포맷(.txt)** → **"포맷 상이"가 진짜 판별자**이고 임계값은 보조.

## 4. 확정 설계 — 하드게이트 + 비파괴 마킹

**탐지기**(silver, 직접 검증 self-join, MinHash 없음):
1. 정규화: `NFC + 전체공백제거`(대소문자 보존). *주의: `content_fingerprint`의 `normalize_for_fingerprint`(공백 축약)와 다른 키. DuckDB `nfc_normalize()+regexp_replace(...,'\s','')`가 Python과 바이트 일치하는지 smoke로 단언할 것.*
2. **하드게이트**(오병합 방지 — 임계값보다 먼저):
   - **포맷 상이** (rel_path 확장자 상이) — 정정·개정·다른서류는 같은 포맷이라 자동 제외. **load-bearing.**
   - **doctype 동일** (증권신고서≠투자설명서≠개정법률안).
   - 길이비 `|La−Lb|/max < 0.3` (cheap 프리게이트, 콘텐츠 없이).
3. **문자 w-shingle(w=10) 정확 Jaccard ≥ T(=0.85)**.
4. **후보 참여 문서만 셰일링** — cross-format 후보가 아닌 대형 same-format 문서(sto 250K·norms 333K)는 셰일 폭발 없이 제외(성능).
5. **전이성 금지**: 쌍(pair) 단위만, 생존/패자 엣지 자체가 검증 ≥ T (3-way 쌍둥이 near-threshold 체인 오병합 방지).

**행동 = 비파괴적 마킹**(삭제 아님): 두 행 보존, `superseded_by = 생존 doc_id`(생존=최고 `FORMAT_PRIORITY`). build_rag/hybrid_search가 색인·검색에서 **억제**(이중 임베딩만 방지, 감사·복원 가능). 표본이 작고 추출기 버전 드리프트로 임계값이 이동할 수 있어 **파괴 대신 마킹**이 안전.

### 검증된 SQL (라이브 모델 투입은 배선 단계에서 — 지금은 다른 빌드 교란 방지 위해 미투입)
```sql
-- silver.document_twins (NON-DESTRUCTIVE): emits (doc_id, superseded_by, jaccard)
-- 게이트=포맷상이+doctype동일+길이비<0.3, 문자 w-shingle 정확 Jaccard≥T, 후보 참여문서만 셰일링
WITH meta AS (
  SELECT doc_id, doc_type,
         lower(regexp_extract(rel_path, '\.([^.]+)$', 1)) AS suffix,
         length(content) AS L
  FROM silver.documents
),
cand AS (                               -- 하드게이트 (콘텐츠 없이 cheap)
  SELECT a.doc_id AS a, b.doc_id AS b
  FROM meta a JOIN meta b
    ON a.doc_id < b.doc_id
   AND a.suffix <> b.suffix             -- 포맷 상이 (load-bearing)
   AND a.doc_type = b.doc_type          -- doctype 동일
   AND abs(a.L - b.L)::DOUBLE / greatest(a.L, b.L) < 0.3
),
parts AS (SELECT a AS d FROM cand UNION SELECT b FROM cand),
shing AS (                              -- 후보 참여 문서만 셰일링
  SELECT d.doc_id, substr(s, g, 10) AS sh
  FROM (SELECT doc_id, regexp_replace(nfc_normalize(content), '\s', '', 'g') AS s
        FROM silver.documents WHERE doc_id IN (SELECT d FROM parts)) d,
       range(1, length(d.s) - 10 + 2) AS t(g)
),
dshing AS (SELECT DISTINCT doc_id, sh FROM shing),
sizes  AS (SELECT doc_id, COUNT(*) AS n FROM dshing GROUP BY doc_id),
inter  AS (
  SELECT c.a, c.b, COUNT(*) AS i
  FROM cand c
  JOIN dshing da ON da.doc_id = c.a
  JOIN dshing db ON db.doc_id = c.b AND db.sh = da.sh
  GROUP BY c.a, c.b
),
scored AS (
  SELECT i.a, i.b, i.i::DOUBLE / (sa.n + sb.n - i.i) AS jaccard
  FROM inter i JOIN sizes sa ON sa.doc_id=i.a JOIN sizes sb ON sb.doc_id=i.b
  WHERE i.i::DOUBLE / (sa.n + sb.n - i.i) >= 0.85    -- T; 실측 마진 +0.30
)
-- 생존 = 최고 FORMAT_PRIORITY. 패자(loser)만 superseded_by 마킹(비파괴).
SELECT ... -- 생존/패자를 FORMAT_PRIORITY로 결정, loser.doc_id + superseded_by=winner
FROM scored ...
```

## 5. 롤아웃 (다른 세션의 포맷확장 커밋 후 · 빌드 유휴 시)
1. **측정 먼저**: `report_dupes.py`(그 세션 소유)에 (a) **네거티브 대조군**(정정본·원안/대안·같은회사 다른서류·모법/시행령 — 전부 실측으로 거부 확인) (b) **인박스 라벨셋**(이름 불일치·cross-collection 실드롭) (c) 잔여 이중색인률을 1급 지표로 추가.
2. `silver.document_twins` 모델 신설(위 SQL) → **2개 PYTHONHASHSEED 동일성 단언**(결정성). DuckDB `nfc_normalize` == Python `NFC+strip` smoke 단언.
3. **성능 검증**: 빌드 유휴 시 실제 lake로 실행, 셰일링이 후보(법안)로 한정돼 유계임을 확인(대형 same-format 문서 제외).
4. **비파괴 배선**: `silver.documents`에 `superseded_by` LEFT JOIN, build_rag/hybrid_search가 마킹된 렌디션 억제(색인 스킵). 파괴적 붕괴는 **하지 않음**.
5. **크로스 컬렉션**: 인박스 PDF(_root)와 큐레이션 HWP(디지털자산관련법안원문) 쌍둥이를 위해 self-join은 컬렉션 무관으로(작은 코퍼스라 저렴).

## 5b. Turnkey 배선 diff (다른 세션 커밋 후 그대로 적용)

**(1) 네거티브 대조군 하니스** — `report_dupes.py`의 `report_twins()`에 추가할 케이스(전부 실측으로 거부 확인됨, 회귀 감지용):
- 정정본: 원본 .txt vs 1글자 정정 .txt (같은 포맷 → 게이트 제외 기대)
- 원안/대안: STO 원안 vs 대안 (doctype 동일이나 cross-format 아니면 제외; 실측 J로 마진 보고)
- 다른 법적서류: 증권신고서 vs 투자설명서 (같은 회사·다른 doctype이면 doctype 게이트 제외)
- 모법/시행령: cross-format이라도 J 0.04–0.08로 T 아래 (확인)
- 각 케이스에 **잔여 이중색인률**을 1급 숫자로 출력.

**(2) `silver.documents` 비파괴 소비** — `renditions` 뒤 최종 SELECT에 LEFT JOIN:
```sql
-- silver.documents 최종 SELECT (rendition_rank=1 이후)에 추가
LEFT JOIN silver.document_twins t ON t.doc_id = renditions.doc_id
-- 새 컬럼: t.superseded_by (NULL이면 대표/단독). 행은 삭제하지 않음(비파괴).
```

**(3) build_rag 색인 억제** — `build_index`에서 `superseded_by IS NOT NULL` 문서를 `chunks_vec`/`chunks_fts`에서 제외(이중 임베딩 방지), `chunks`에는 보존(provenance). hybrid_search는 superseded 렌디션을 결과에서 억제하되 graph_rag related에는 노출 가능(감사).

**(4) 결정성·정규화 단언**(smoke): `nfc_normalize(x)+strip` (DuckDB) == `normalize_for_fingerprint` 계열 (Python)을 코퍼스 샘플로 바이트 비교; 2개 PYTHONHASHSEED에서 `document_twins` 출력 동일.

## 6. 남은 한계 (알고 남긴 것)
- **T=0.85는 10개 양성·90개 음성 점추정** — 코퍼스 확장 시 재측정. 파괴 대신 마킹이라 드리프트가 삭제 아닌 복원가능.
- **cross-format인데 진짜 다른 문서가 T 넘는 경우**(예: 어떤 법안 hwp vs 다른 법안 pdf)는 실측 최대 0.610 → 현재 안전하나, doctype 세분(모든 법안이 doctype=bill)이 커지면 재검토.
- **3-way(hwp+hwpx+pdf)·docx 쌍둥이 미검증**(코퍼스 0건) → 해당 렌디션은 provenance 시딩에 맡기고 퍼지키는 {hwp,pdf} 우선.
- **추출기 버전 드리프트**(pypdf/hwpkit 업글)가 셰일 집합을 바꿈 → 추출기 버전 핑거프린트를 T 옆에 커밋, 불일치 시 마킹 비활성.
