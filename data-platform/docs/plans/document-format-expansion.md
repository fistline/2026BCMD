# 문서 포맷 확장 — doc/docx, xls/xlsx, ppt/pptx, pdf, png/jpg

- 상태: **구현 완료(2026-07-26)**. 계획 → 적대적 리뷰 → 구현 → 검증 순으로 진행.
- 범위: `pipeline/extract.py`(추출기), `pipeline/chunking.py`(포맷 표·우선순위),
  `pipeline/watcher.py`(쌍둥이 시딩), `pipeline/report_dupes.py`(`--twins` 측정),
  `transform/models/silver/documents.sql`(렌디션 사다리),
  `transform/audits/assert_profile_sections_survived.sql`, `tools/ocr/ocr_prepare.py`.

## 결론 요약

| 포맷 | 결정 | 리더 | 기본 설치 |
|---|---|---|---|
| `.docx` `.xlsx` `.pptx` | 빌드 경로 | **표준 라이브러리만** (zip+XML 텍스트 노드 스윕) | 예 |
| `.pdf` | 빌드 경로 | `pypdf` 6.x (BSD-3, 순수 파이썬), 텍스트 레이어만 | 예 |
| `.doc` `.xls` `.ppt` | 빌드 경로, **옵트인** | `office-oxide` 0.1.8 (MIT/Apache-2.0) | 아니오 (`--extra legacy`) |
| `.png` `.jpg` 등 | **빌드 경로 아님** | `tools/ocr/ocr_prepare.py` → 사람 검수 → `.txt` | 아니오 (`--extra ocr`) |

OOXML을 office-oxide에 위임하지 않은 이유: OOXML은 zip 안 XML이라
**텍스트 노드 스윕**(스키마 파싱 아님)으로 표·텍스트박스·각주가 자동으로 딸려 온다.
각 30줄 남짓, 감사 가능, 버전 업으로 결과가 안 바뀐다. 감수한 office-oxide 리스크
(출시 3개월·0.x·단독 메인테이너)를 **대안이 없는 3종에만 가둔다.**

## 측정 1 — HWP/PDF 쌍둥이 fingerprint (`make dupes-twins`)

AGENTS.md가 PDF 활성화 전 요구한 게이트. `source/`의 실제 쌍둥이 10쌍 대상.
정규화 후보 5종을 측정했다.

| 후보 | 내용 | 일치 |
|---|---|---|
| n0 | 현행 `normalize_for_fingerprint` (NFC + 공백 축약) | **0/10** |
| n1 | n0 + 페이지 표식(`- 1 -`) 제거 | **0/10** |
| n2 | n1 + 공백 전부 제거 | **0/10** |
| n2/layout | n2 + pypdf `extraction_mode="layout"` | **4/10** |
| n4 | n2 + 문자 다중집합 정렬(순서 무시) | **7/10** |

**진단**: hwpkit과 pypdf는 법안의 **문자에는 합의하고 순서에 불합의**한다.
공백 제거 후 문자 다중집합이 완전히 동일(jaccard 1.0)한 쌍이 다수이고,
표지의 `의 안 번 호` 표를 두 추출기가 서로 다른 지점에 선형화한다
(HWP: 제목→의안번호→제안이유 / PDF plain: 제목→제안이유→…→의안번호).
페이지 furniture·공백 정규화로는 닿지 않는 층위다.

**n4(7/10) 기각**: 순서 무시 키는 **애너그램을 전부 중복으로 만든다.**
무엇을 색인할지 결정하는 equality 키로는 너무 약하다. 이 저장소가
`excluded.tsv`에 기각을 남기는 것과 같은 이유로 여기 기록한다.

**채택한 해법 — 경계를 바꿨다.** 내용 dedup을 더 밀어붙이는 대신,
`seed_inbox`가 큐레이션된 쌍둥이 집합에서 **최상위 렌디션 하나만 시딩**한다
(`watcher._superseded_renditions`). 이것은 우회가 아니라 **이미 문서화된 의도의 구현**이다:
AGENTS.md 불변식 1과 `seed_inbox` docstring이 *"`.hwp` 원본 옆의 `.pdf` 쌍둥이는
`source/`에 남고 파이프라인에 들어가지 않는다"*고 명시하고 있었고, 지금까지는
`.pdf`가 미지원이라 **우연히** 지켜지고 있었다. PDF가 읽히는 순간 그 우연이 깨진다.

`source/`에서 쌍둥이는 **큐레이션 사실**이므로 provenance 기준 스킵이 확률적이지 않고 정확하다.
인박스로 직접 떨어진 쌍둥이는 provenance를 알 수 없으므로 기존 content fingerprint가
계속 그물 역할을 한다. 두 메커니즘이 서로 다른 경계에서 서로 다른 것을 안다.

## 측정 2 — 기존 HWPX 추출기 버그 (발견 및 수정)

`_extract_hwpx`가 **표 셀 텍스트를 두 번** 냈다. 확인:

```
입력: <hp:p>바깥문단 <hp:tbl><hp:tc><hp:p>셀텍스트</hp:p></hp:tc></hp:tbl></hp:p>
기존 출력: '바깥문단셀텍스트\n셀텍스트'   <- 셀텍스트 2회
```

문단이 **중첩**되기 때문이다 — HWPX 표는 `<hp:p>` 안에 살고 셀은 자기 `<hp:p>`를 갖는다.
Word 텍스트박스(`<w:p>` 안의 DrawingML `<a:p>`)도 같은 모양이다.
법안의 신구조문대비표가 이중 색인되는데 청크 수는 그럴듯해서 **다운스트림에서 안 보인다.**

수정: `_paragraph_texts`가 바깥 문단이 자손을 소비하게 하고 중첩 문단은 건너뛴다.
docx/pptx가 같은 헬퍼를 쓰므로 신규 포맷은 처음부터 이 버그가 없다.
**blast radius 0** — 현 코퍼스에 `.hwpx`가 0건이라 재색인 영향 없음.

## 적대적 리뷰에서 계획이 바뀐 지점

1. **PDF 게이트 닭-달걀** — `report_dupes`는 bronze를 읽어 PDF가 이미 인제스트돼야
   답이 나온다. `--twins`(읽기 전용, `source/` 직접 추출)로 순환을 끊었다.
2. **추출 캐시** — `extract.py` 주석이 *"파싱이 초 단위인 포맷이 들어오면 재검토하라"*고
   예고했고 PDF(161쪽 ≈ 0.7초)가 바로 그 포맷. `parse_document`는 파일당 빌드당 3회
   실행되므로 sha256(bytes) 키 memo 캐시를 넣었다. 바이트 주소 지정이라 결정성 무영향.
3. **블로킹 감사 구멍** — `assert_profile_sections_survived.sql`의
   `doc_type IN ('hwp','hwpx','txt')`가 하드코딩. `ROUTABLE_SUFFIXES = BINARY_SUFFIXES | {.txt}`
   라서 새 포맷은 자동으로 프로파일 대상이 되는데 감사에서는 빠진다 →
   claim된 docx/pdf가 섹션을 통째로 잃어도 빌드 통과. IN 리스트 확장.
4. **docx에 마크다운 헤딩 합성 금지** — `w:pStyle` 값이 템플릿·로케일마다 다르고
   (`Heading1`/`1`/`제목1`), 합성 마커가 `content`에 들어가 fingerprint를 오염시킨다.
   docx·pdf는 문단 평문만. 한국 법률문서는 `_KO_SECTION_RE`가 이미 섹션을 잡는다
   (HWP가 지금 그렇게 동작). xlsx/pptx만 시트·슬라이드 경계가 내용에 없어 ATX 마커를 낸다.
5. **렌디션 사다리 역전** — `hwp=3, hwpx=2, pdf=1, ELSE 0`에서 docx가 ELSE 0이면
   **PDF가 docx를 이긴다**(원본이 파생본에 짐). 9단 사다리로 재작성하고
   `chunking.FORMAT_PRIORITY` 단일 출처 + smoke test로 SQL 사본 드리프트 차단.

## 남은 한계 (알고 남긴 것)

- **인박스 직접 투입 쌍둥이**는 여전히 이중 색인될 수 있다. HWP/PDF 조합에서
  content fingerprint가 0/10이므로 그물이 잡지 못한다. `make dupes`가 빈 결과를
  내면 병합이 안 됐다는 뜻이고, `make dupes-twins`가 진단 도구다.
- **`.doc`/`.ppt` 추출 품질은 실측되지 않았다.** 코퍼스에 0건이라 픽스처를 만들 수 없었다.
  `.xls`만 실제 BIFF 파일(xlwt 생성)로 왕복 확인했다: `'손익\n매출액\t1000\n영업이익\n'`.
  실제 `.doc`가 들어오면 `make triage`와 추출 결과를 눈으로 확인할 것.
- **암호화 OOXML의 `ExtractionProtected` 경로는 코드 검토로만 확인**했다.
  OLE 컨테이너 생성 라이브러리가 없어 진짜 암호화 픽스처를 만들 수 없었다.
  OLE이지만 암호화가 아닌 경우(실제 HWP를 `.docx`로 오인)는 테스트로 커버돼 있다.
