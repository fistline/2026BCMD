# DART as a source: API for bodies, stealth scrape for attachments

Load this when the source is DART (금융감독원 전자공시). It is the worked example of
a source split across an API and a no-API, bot-hostile tail.

## Per-path channel

| Path | Host | Channel |
| --- | --- | --- |
| `list.json`, `document.xml`, `corpCode.xml` | `opendart.fss.or.kr` (API) | **API — `fetch_law dart` with a crtfc_key** |
| `/dsaf001`, `/report/viewer.do`, `/pdf/download` (첨부 PDF) | `dart.fss.or.kr` (viewer) | **no API, bot-hostile — `fetch_web --stealth`** |

The OPEN API `document.xml` returns the filing BODY only. The 부속서류
(감정평가서, 외부평가기관 평가보고서, 투자설명서 PDF, 감사보고서) live ONLY on the
viewer, which has no API and kills non-browser connections. So the body comes from
the API and the attachments come from `fetch_web` with a stealth browser (below).

## fetch_law dart — search axes (already built)

```
--corp <회사명>          resolve to corp_code via the corpCode master (cached)
--corp-code <8자리>      skip name resolution
--keyword <a,b>          report_nm substring(s), comma = OR; '' keeps all
--pblntf-ty C            공시유형 top level (C=발행공시; '' omits the server param)
--detail-ty C001         발행공시 상세유형 (C001 지분·C002 채무·C005 기타 …)
--since / --until        YYYYMMDD window
--related                also pull the offering family (정정/투자설명서/발행실적/철회)
--list-only              preview; download nothing  (this is the SAMPLE step)
```

Prerequisite: a free `DART_API_KEY` (crtfc_key) in `.env`; the first `--corp`
call downloads the corpCode master into `data/processed/` (git-ignored).

## Attachment download procedure

The 첨부 (감정평가서/계약서/투자설명서 PDF) have no API — they live on the
`dart.fss.or.kr` viewer, which is bot-hostile (it kills non-browser connections),
so a plain fetch will not do; use a stealth browser.

1. From the API body fetch (or `--list-only`), note the `rcept_no`; open the
   viewer for it and copy each wanted 부속서류's PDF URL from the 첨부 list.
2. Land it with a stealth browser (see `references/scrapling-fetch.md`):

   ```
   uv run python -m pipeline.fetch_web --url "<viewer-pdf-url>" --stealth always \
       --dest data/inbox/documents/sto --name 감정평가서_<corp>_<rcept_no>.pdf
   ```

   Name it to carry the `rcept_no` so it de-duplicates and sits beside the body.
   If even a stealth browser is blocked (rare), save the PDF from your own browser
   into the same directory.
3. If the PDF is a SCANNED IMAGE, OCR + HITL-review it (see the main SKILL) so a
   reviewed `.txt` enters the inbox. Then `make build`.

The default build path never fetches; this is an explicit operator step.

## 조각투자(STO) issuers — two tracks

The offering FORM differs, so the fetch filter must branch:

| Firm | corp_name (confirm vs corpCode) | Track | Asset |
| --- | --- | --- | --- |
| 열매컴퍼니 | (주)열매컴퍼니 | 투자계약증권 | 미술품 |
| 서울옥션블루 | (주)서울옥션블루 | 투자계약증권 | 미술품 |
| 투게더아트 | (주)투게더아트 | 투자계약증권 (연속 발행) | 미술품 |
| 스탁키퍼 | (주)스탁키퍼 | 투자계약증권 | 한우 |
| 뮤직카우 | (주)뮤직카우 | **신탁수익증권** (투자계약증권 아님) | 음악저작권 |
| 루센트블록 | (주)루센트블록 | 신탁수익증권 (샌드박스 — DART에 없을 수 있음) | 부동산 |

## One-off runbook (no build; run after DART_API_KEY is set)

```
# 투자계약증권 4사 — default keyword 투자계약증권 fits; --related pulls the family
uv run python -m pipeline.fetch_law dart --corp 열매컴퍼니 --related --list-only
uv run python -m pipeline.fetch_law dart --corp 서울옥션블루 --related --list-only
uv run python -m pipeline.fetch_law dart --corp 투게더아트 --related --list-only
uv run python -m pipeline.fetch_law dart --corp 스탁키퍼 --related --list-only

# 뮤직카우 — 신탁수익증권, NOT caught by the default keyword; branch:
uv run python -m pipeline.fetch_law dart --corp 뮤직카우 --keyword '' --list-only
#   if empty, widen the type:  --pblntf-ty '' --keyword 증권신고서

# 루센트블록 — likely exempt/absent; verify, expect possibly zero:
uv run python -m pipeline.fetch_law dart --corp 루센트블록 --keyword '' --list-only

# Review the --list-only output, then drop --list-only to land the bodies, then:
make build
```

Do NOT run the default sweep uniformly for all six — the default keyword misses
뮤직카우 and mis-scopes 루센트블록.
