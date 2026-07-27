"""Operator tool: land OFFICIAL Korean legal documents in the inbox.

NOT part of the build and NOT on the read path. An operator runs this to pull
source material; the fetched files land in `data/inbox/documents/` and the normal
pipeline promotes, transforms and indexes them with the same provenance as any
other document (§ invariant 8: prefer an official API, respect robots.txt).

Three capabilities, split by whether the source is robots-clean:

  law              법제처 국가법령정보 OPEN API (law.go.kr/DRF). Enacted statute
                   text as XML, serialised to a .txt. Robots-clean, run by default.
  bill             국회 열린국회정보 OPEN API (open.assembly.go.kr). Bill metadata
                   and the official billId. Robots-clean, run by default. This is
                   the first-party way to resolve a billId, replacing any
                   third-party lookup.
  bill --allow-likms
                   The 원문 HWP bytes exist ONLY on likms.assembly.go.kr, whose
                   robots.txt is `Disallow: /` for generic agents. So the byte
                   download is gated behind this flag and prints a robots/ToS
                   notice: the operator asserts permission to fetch specific known
                   files (not to crawl). Default runs never touch likms.
  dart             DART 전자공시 OPEN API (opendart.fss.or.kr). Fetches 증권신고서
                   bodies, defaulting to 투자계약증권/조각투자 offerings, as .txt.
                   Robots-clean: every request stays on the opendart.fss.or.kr API
                   host; the dart.fss.or.kr web viewer (Disallow) is never touched,
                   so the appraisal 첨부서류 behind it are intentionally NOT fetched.

Credentials live in .env (git-ignored, § invariant 6), never committed:
  LAW_OC             OC for law.go.kr DRF. Free: register at open.law.go.kr.
  ASSEMBLY_API_KEY   KEY for open.assembly.go.kr. Free: register and issue a key.
  DART_API_KEY       crtfc_key for opendart.fss.or.kr. Free: register (instant).

Discovering datasets on 공공데이터포털 (data.go.kr) is a manual recipe, not a
subcommand: the odcloud catalog API returns only catalog metadata, never a 원문,
so it is not on any fetch path. To search the catalog on demand (needs a free
data.go.kr serviceKey):
    curl "https://api.odcloud.kr/api/15077093/v1/open-data-list?returnType=JSON\
&perPage=20&cond[title::LIKE]=증권신고서&serviceKey=<KEY>"
If a specific dataset that yields a document ever surfaces, promote it to its own
subcommand against a verified live response -- do not add a generic client.

CLI:
    uv run python -m pipeline.fetch_law law --query 가상자산이용자보호
    uv run python -m pipeline.fetch_law bill --name 디지털자산기본법안
    uv run python -m pipeline.fetch_law bill --name 디지털자산기본법안 --allow-likms
    uv run python -m pipeline.fetch_law dart --list-only
    uv run python -m pipeline.fetch_law dart --corp 투게더아트 --list-only
    uv run python -m pipeline.fetch_law dart --detail-ty C001 --keyword '' --list-only
    uv run python -m pipeline.fetch_law dart --corp 투게더아트 --related --list-only
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from pathlib import Path

from pipeline import get_paths

USER_AGENT = "local-agent-platform-fetch_law/0.1 (operator tool; respects robots.txt)"

DRF_SEARCH = "https://www.law.go.kr/DRF/lawSearch.do"
DRF_BODY = "https://www.law.go.kr/DRF/lawService.do"
ASSEMBLY_OPENAPI = "https://open.assembly.go.kr/portal/openapi/"
# DART OPEN API. Only the opendart.fss.or.kr API host is ever contacted; the
# dart.fss.or.kr web viewer (/report/viewer.do, /pdf/download, 첨부 gates) is
# robots-Disallow and must never be built into a URL here.
DART_LIST = "https://opendart.fss.or.kr/api/list.json"
DART_DOCUMENT = "https://opendart.fss.or.kr/api/document.xml"
DART_CORPCODE = "https://opendart.fss.or.kr/api/corpCode.xml"
# The first 투자계약증권 증권신고서 in Korea (투게더아트, Stanley Whitney) was
# filed on this date; nothing earlier exists to sweep for.
DART_INVESTMENT_CONTRACT_EPOCH = "20230801"
# 발행공시(C) 상세유형. 투자계약증권 has NO code here (it files under the group but
# is not enumerated), so it is targeted by the report_nm keyword, not a code.
DART_DETAIL_TYPES = {
    "C001": "증권신고(지분증권)",
    "C002": "증권신고(채무증권)",
    "C003": "증권신고(파생결합증권)",
    "C004": "증권신고(합병등)",
    "C005": "증권신고(기타)",
    "C006": "소액공모(지분증권)",
    "C007": "소액공모(채무증권)",
    "C008": "소액공모(파생결합증권)",
    "C009": "소액공모(합병등)",
    "C010": "소액공모(기타)",
    "C011": "호가중개시스템을통한소액매출",
}
# The offering lifecycle for one 증권신고서, matched as report_nm substrings:
# 원신고서 → 정정신고서 → 투자설명서 → 발행실적보고서 / 철회신고서. DART exposes no
# offering id, so a --related family is grouped by corp_code + these + a time window.
DART_LIFECYCLE_PATTERNS = ("증권신고서", "투자설명서", "증권발행실적보고서", "철회신고서")
# 국회의원 발의법률안. 위원장 제안 대안(a merged/passed 대안) is not a member
# proposal and will not appear here; look those up with service TVBPMBILL11.
ASSEMBLY_BILL_SERVICE = "nzmimeepazxkubdpn"
LIKMS_BILL_DETAIL = "https://likms.assembly.go.kr/bill/billDetail.do"
LIKMS_FILEGATE = "https://likms.assembly.go.kr/filegate/servlet/FileGate"

# likms.assembly.go.kr/robots.txt is `User-agent: * / Disallow: /`: the whole
# site is off-limits to generic agents, and the 원문 bytes live only there. This
# is printed before any likms request so the choice is explicit and logged.
LIKMS_ROBOTS_NOTICE = (
    "NOTICE: likms.assembly.go.kr/robots.txt disallows all generic agents "
    "(Disallow: /). No official API serves the bill 원문 bytes; likms FileGate is "
    "the only source. Proceeding fetches SPECIFIC known files you identified, not "
    "a crawl. You assert you have permission under the site's terms. To stop, omit "
    "--allow-likms and download manually from the billDetail.do URL printed above."
)


class FetchError(RuntimeError):
    """A fetch could not complete. Raised with an operator-actionable message."""


# --------------------------------------------------------------------------
# HTTP (stdlib only, to keep this tool dependency-free like pipeline/extract.py)
# --------------------------------------------------------------------------
def _http_get(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 30) -> bytes:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _decode(raw: bytes) -> str:
    """Decode a Korean government page. Most are UTF-8; some likms pages EUC-KR."""
    for encoding in ("utf-8", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _sanitise_filename(name: str) -> str:
    """Keep Hangul; strip path separators, control chars and leading dots."""
    cleaned = re.sub(r"[/\\\x00-\x1f]+", "_", name).strip().lstrip(".")
    return cleaned or "document"


def _land(filename: str, data: bytes, dest_dir: Path | None = None) -> Path:
    """Write bytes into the inbox (or a caller-supplied directory) and return the path.

    Writes to data/inbox/documents/ by default, never straight to data/raw or the
    serving index: fetched material goes through the same promotion and provenance
    as everything else.
    """
    directory = Path(dest_dir) if dest_dir else get_paths().inbox
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _sanitise_filename(filename)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# 법령: 법제처 DRF (robots-clean)
# --------------------------------------------------------------------------
def _statute_text_from_drf(xml_bytes: bytes) -> str:
    """Serialise a DRF law body to plain statute text in document order.

    Every 조문내용/항내용/호내용/목내용 element is one line, so the 제N조 headings
    survive for the generic Korean sectioner (_KO_SECTION_RE) to split on.
    """
    root = ET.fromstring(xml_bytes)
    lines = [
        element.text.strip()
        for element in root.iter()
        if element.tag.endswith("내용") and element.text and element.text.strip()
    ]
    return "\n".join(lines)


# DRF search/body differ per document class only in the element name, the id
# field, the 현행 flag, and the body's id-param key. The body XML uses the same
# 조문내용/항내용/… "내용" tags for both, so _statute_text_from_drf serves both.
_DRF_TARGETS = {
    # target -> (search element, id field, name field, 현행 field, body id-param)
    "law": ("law", "법령일련번호", "법령명한글", "현행연혁코드", "MST"),
    "admrul": ("admrul", "행정규칙일련번호", "행정규칙명", "현행연혁구분", "ID"),
}


def fetch_law(
    query: str,
    oc: str | None = None,
    ef_yd: str | None = None,
    dest_dir: Path | None = None,
    target: str = "law",
) -> Path:
    """Fetch an enacted 법령 or 행정규칙 body by name and land it as a .txt in the inbox.

    target="law" covers 법률/시행령/시행규칙; target="admrul" covers 고시/훈령/예규/세칙
    (its body id-param is ID, not MST, but the body XML uses the same 내용 tags).

    Determinism note: DRF returns the CURRENT text, which changes when the rule is
    amended; pass ef_yd=YYYYMMDD to pin a version (법령 only). The inbox->raw
    promotion records any change as a revision either way.
    """
    oc = oc or os.environ.get("LAW_OC")
    if not oc:
        raise FetchError(
            "LAW_OC is not set. Register a free OC at open.law.go.kr (email "
            "verification only) and put LAW_OC=<your-id> in .env, or pass oc='test' "
            "(the shared DRF test key)."
        )
    if target not in _DRF_TARGETS:
        raise FetchError(f"unknown target {target!r}; expected one of {sorted(_DRF_TARGETS)}.")
    element, id_field, name_field, current_field, body_param = _DRF_TARGETS[target]

    search = ET.fromstring(_http_get(DRF_SEARCH, {"OC": oc, "target": target, "type": "XML", "query": query, "display": "5"}))
    result_code = (search.findtext("resultCode") or "").strip()
    if result_code and result_code != "00":
        raise FetchError(f"law.go.kr search failed (resultCode={result_code}). Check LAW_OC and the query.")

    entries = search.findall(element)
    if not entries:
        raise FetchError(f"no {target} matched {query!r}. Try the exact name, e.g. 가상자산이용자보호.")

    chosen = next((e for e in entries if (e.findtext(current_field) or "").strip() == "현행"), entries[0])
    entry_id = (chosen.findtext(id_field) or "").strip()
    name = (chosen.findtext(name_field) or query).strip()
    if not entry_id:
        raise FetchError(f"law.go.kr returned no {id_field} for {name!r}; cannot fetch the body.")

    body_params = {"OC": oc, "target": target, "type": "XML", body_param: entry_id}
    if ef_yd and target == "law":
        body_params["efYd"] = ef_yd
    text = _statute_text_from_drf(_http_get(DRF_BODY, body_params))
    if len(text.strip()) < 50:
        raise FetchError(f"law.go.kr returned an empty body for {name!r} ({body_param}={entry_id}).")

    return _land(f"{name}.txt", text.encode("utf-8"), dest_dir)


# --------------------------------------------------------------------------
# 의안: open.assembly OPEN API (robots-clean) + likms FileGate (gated)
# --------------------------------------------------------------------------
def _assembly_rows(payload: dict, service: str) -> list:
    """Pull the data rows out of an open.assembly JSON response, or fail loudly."""
    if isinstance(payload, dict) and "RESULT" in payload:
        info = payload["RESULT"]
        raise FetchError(f"open.assembly error {info.get('CODE')}: {info.get('MESSAGE')}")
    block = payload.get(service) if isinstance(payload, dict) else None
    if not isinstance(block, list):
        raise FetchError("unexpected open.assembly response shape; check the service name and KEY.")
    for section in block:
        if isinstance(section, dict) and "row" in section:
            return section["row"] or []
    # A well-formed response with no matches carries a head INFO code and no rows.
    return []


def resolve_bill(
    name: str | None = None,
    bill_no: str | None = None,
    key: str | None = None,
    age: str | None = "22",
    service: str = ASSEMBLY_BILL_SERVICE,
) -> list:
    """Resolve bill metadata (including BILL_ID) from open.assembly. Robots-clean.

    This is the first-party billId lookup; no third-party site is involved.
    """
    key = key or os.environ.get("ASSEMBLY_API_KEY")
    if not key:
        raise FetchError(
            "ASSEMBLY_API_KEY is not set. Register free at open.assembly.go.kr, "
            "issue an 인증키, and put ASSEMBLY_API_KEY=<key> in .env."
        )
    if not name and not bill_no:
        raise FetchError("give --name (의안명) or --bill-no (의안번호) to resolve a bill.")

    params = {"KEY": key, "Type": "json", "pIndex": "1", "pSize": "100"}
    if age:
        params["AGE"] = age
    if name:
        params["BILL_NAME"] = name
    if bill_no:
        params["BILL_NO"] = bill_no
    payload = json.loads(_decode(_http_get(ASSEMBLY_OPENAPI + service, params)))
    return _assembly_rows(payload, service)


def _parse_filegate(html: str) -> list:
    """Extract (bookId, type) pairs from a likms billDetail.do page.

    The verified download link form is `filegate/servlet/FileGate?bookId=<UUID>
    &type=<0|1>` (0=HWP, 1=PDF). If the page exposes downloads only as JavaScript
    calls whose layout has changed, this finds nothing and the caller falls back
    to telling the operator to download manually -- it never guesses a bookId.
    """
    pairs = re.findall(r"bookId=([0-9A-Za-z\-]{16,})&(?:amp;)?type=(\d)", html)
    seen: set = set()
    unique = []
    for book_id, kind in pairs:
        marker = (book_id, kind)
        if marker not in seen:
            seen.add(marker)
            unique.append(marker)
    return unique


def fetch_bill(
    name: str | None = None,
    bill_no: str | None = None,
    key: str | None = None,
    age: str | None = "22",
    service: str = ASSEMBLY_BILL_SERVICE,
    allow_likms: bool = False,
    dest_dir: Path | None = None,
    stream=sys.stdout,
) -> Path | None:
    """Resolve a bill's metadata; with allow_likms, also download its 원문 HWP.

    Without allow_likms this only prints metadata and the official billDetail.do
    URL for a manual download, and returns None. The default never touches likms.
    """
    rows = resolve_bill(name=name, bill_no=bill_no, key=key, age=age, service=service)
    if not rows:
        raise FetchError(
            f"no bill matched name={name!r} bill_no={bill_no!r} in service {service}. "
            "A 위원장 제안 대안 is not a member proposal; try --service TVBPMBILL11."
        )

    chosen = next((row for row in rows if name and (row.get("BILL_NAME") or "").strip() == name.strip()), rows[0])
    bill_id = (chosen.get("BILL_ID") or "").strip()
    bill_name = (chosen.get("BILL_NAME") or name or "bill").strip()
    proposer = (chosen.get("PROPOSER") or chosen.get("RST_PROPOSER") or "").strip()
    detail = (chosen.get("DETAIL_LINK") or f"{LIKMS_BILL_DETAIL}?billId={bill_id}").strip()

    print(f"resolved {len(rows)} candidate(s); using:", file=stream)
    print(f"  BILL_NAME : {bill_name}", file=stream)
    print(f"  BILL_ID   : {bill_id}", file=stream)
    print(f"  PROPOSER  : {proposer}", file=stream)
    print(f"  PROC      : {(chosen.get('PROC_RESULT') or '').strip()}", file=stream)
    print(f"  DETAIL    : {detail}", file=stream)

    if not allow_likms:
        print(
            "\nmetadata only. The 원문 HWP is on likms.assembly.go.kr, which "
            "robots.txt disallows for generic agents. Download it yourself from the "
            "DETAIL url above, or re-run with --allow-likms to assert operator "
            "permission for this specific known file.",
            file=stream,
        )
        return None

    if not bill_id:
        raise FetchError("the resolved bill has no BILL_ID; cannot locate its 원문.")

    print("\n" + LIKMS_ROBOTS_NOTICE, file=stream)
    html = _decode(_http_get(LIKMS_BILL_DETAIL, {"billId": bill_id}, headers={"Referer": detail}))
    books = _parse_filegate(html)
    hwp = next((book_id for book_id, kind in books if kind == "0"), None)
    if not hwp:
        raise FetchError(
            "could not locate a FileGate HWP link on billDetail.do (the page layout "
            f"may have changed). Download it manually from: {detail}"
        )

    data = _http_get(LIKMS_FILEGATE, {"bookId": hwp, "type": "0"}, headers={"Referer": detail})
    filename = f"{bill_name}_{proposer}.hwp" if proposer else f"{bill_name}.hwp"
    landed = _land(filename, data, dest_dir)
    print(f"\nlanded: {landed}", file=stream)
    return landed


# --------------------------------------------------------------------------
# DART 증권신고서: opendart.fss.or.kr OPEN API (robots-clean, API host only)
# --------------------------------------------------------------------------
def _dart_windows(since: str, until: str, days: int = 90):
    """Yield (bgn_de, end_de) YYYYMMDD windows. list.json caps a no-corp_code
    query at ~3 months, so the sweep is chunked rather than one wide range."""
    start = date(int(since[:4]), int(since[4:6]), int(since[6:8]))
    end = date(int(until[:4]), int(until[4:6]), int(until[6:8]))
    while start <= end:
        stop = min(start + timedelta(days=days - 1), end)
        yield start.strftime("%Y%m%d"), stop.strftime("%Y%m%d")
        start = stop + timedelta(days=1)


def _dart_list(
    crtfc_key: str,
    bgn_de: str,
    end_de: str,
    pblntf_ty: str | None,
    corp_code: str | None,
    detail_ty: str | None = None,
) -> list:
    """One paginated list.json sweep of a window. Raises on a real error status;
    treats 013 (no data for the window) as an empty result, not a failure."""
    rows: list = []
    page = 1
    while True:
        params = {
            "crtfc_key": crtfc_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": str(page),
            "page_count": "100",
        }
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty
        if detail_ty:
            params["pblntf_detail_ty"] = detail_ty
        if corp_code:
            params["corp_code"] = corp_code
        payload = json.loads(_decode(_http_get(DART_LIST, params)))
        status = payload.get("status")
        if status == "013":  # 조회된 데이터가 없습니다 -- a normal empty window.
            return rows
        if status != "000":
            raise FetchError(f"DART list.json error {status}: {payload.get('message')}")
        rows.extend(payload.get("list", []))
        if page >= int(payload.get("total_page", 1)):
            return rows
        page += 1


def _dart_document_text(crtfc_key: str, rcept_no: str) -> str:
    """Download one filing body (document.xml -> ZIP of DART XML) as plain text.

    document.xml returns a ZIP on success and a small XML error envelope
    (`<result><status>..`) otherwise. DART bodies are SGML-flavoured and can
    carry undefined entities, so XML parsing falls back to tag stripping rather
    than aborting the whole sweep on one malformed filing.
    """
    raw = _http_get(DART_DOCUMENT, {"crtfc_key": crtfc_key, "rcept_no": rcept_no})
    if raw[:2] != b"PK":  # not a ZIP: it is the error envelope.
        message = _decode(raw)
        found = re.search(r"<message>(.*?)</message>", message)
        raise FetchError(f"DART document.xml for {rcept_no}: {found.group(1) if found else message[:120]}")

    parts: list = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".xml"):
                continue
            decoded = _decode(archive.read(name))
            try:
                root = ET.fromstring(decoded)
                lines = [
                    fragment.strip()
                    for element in root.iter()
                    for fragment in (element.text, element.tail)
                    if fragment and fragment.strip()
                ]
            except ET.ParseError:
                lines = [line.strip() for line in re.sub(r"<[^>]+>", "\n", decoded).splitlines() if line.strip()]
            parts.append("\n".join(lines))
    return "\n".join(parts)


def _norm_corp(text: str) -> str:
    """Fold a company name for matching: NFC, no spaces, casefold."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text or "")).casefold()


def _keyword_terms(keyword: str) -> list:
    """Split --keyword into NFC-normalised OR terms. Empty keyword -> [] (keep all)."""
    return [unicodedata.normalize("NFC", term.strip()) for term in (keyword or "").split(",") if term.strip()]


def _report_matches(report_nm: str, terms: list) -> bool:
    """True if report_nm (NFC) contains ANY term, or there are no terms.

    NFC on both sides: DART report_nm can arrive in a different normalisation than
    the CLI argument, and a mismatch would silently drop valid rows.
    """
    if not terms:
        return True
    text = unicodedata.normalize("NFC", report_nm or "")
    return any(term in text for term in terms)


def _dart_corp_index(crtfc_key: str, refresh: bool = False, cache_dir: Path | None = None) -> list:
    """Return the corp master as a list of (corp_code, corp_name, stock_code).

    corpCode.xml is one ZIP of the whole DART register (~100k corps, listed and
    non-listed). It is derived, regenerable data, so it is cached in the data
    plane (git-ignored), never in the inbox. `refresh` re-downloads it.
    """
    directory = Path(cache_dir) if cache_dir else get_paths().processed
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / "dart_corpcode.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    raw = _http_get(DART_CORPCODE, {"crtfc_key": crtfc_key})
    if raw[:2] != b"PK":  # error envelope, not a ZIP
        message = _decode(raw)
        found = re.search(r"<message>(.*?)</message>", message)
        raise FetchError(f"DART corpCode.xml: {found.group(1) if found else message[:120]}")

    entries: list = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = next((member for member in archive.namelist() if member.lower().endswith(".xml")), None)
        if not name:
            raise FetchError("DART corpCode.xml ZIP contained no .xml member.")
        root = ET.fromstring(_decode(archive.read(name)))
        for node in root.iter("list"):
            entries.append([
                (node.findtext("corp_code") or "").strip(),
                (node.findtext("corp_name") or "").strip(),
                (node.findtext("stock_code") or "").strip(),
            ])
    cache.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return entries


def _resolve_corp(query: str, index: list) -> list:
    """Resolve a company name to candidate (corp_code, corp_name, stock_code).

    Exact folded match wins; otherwise the registered name must CONTAIN the query
    (so "루센트블록" matches "루센트블록(주)"), closest (shortest) name first. The
    reverse direction -- registered name is a substring of the query -- is
    deliberately NOT used: a one-character corp like "록" is a substring of
    "루센트블록" and would silently resolve to the wrong company. A firm absent
    from DART must return no match, not a garbage one.
    """
    want = _norm_corp(query)
    if not want:
        return []
    exact = [entry for entry in index if _norm_corp(entry[1]) == want]
    if exact:
        return exact
    contains = [entry for entry in index if want in _norm_corp(entry[1])]
    contains.sort(key=lambda entry: len(_norm_corp(entry[1])))
    return contains


def _dart_related(key: str, corp_code: str, anchor_rcept_dt: str, pre_days: int, post_days: int) -> list:
    """Collect one offering's lifecycle filings for a corp, ordered chronologically.

    DART exposes no offering identifier, so membership is corp_code + a time window
    around the anchor's 접수일 + a lifecycle report_nm match. pblntf_ty is OMITTED
    (swept for the fixed corp_code) because it is unconfirmed that 투자설명서 /
    증권발행실적보고서 / 철회신고서 all sit under 발행공시 -- trusting a type here
    could silently drop them. rcept_no is monotonic, so ascending order is the
    chronological 원신고서 -> 정정 -> 투자설명서 -> 발행실적/철회 sequence.
    """
    anchor = date(int(anchor_rcept_dt[:4]), int(anchor_rcept_dt[4:6]), int(anchor_rcept_dt[6:8]))
    since = (anchor - timedelta(days=pre_days)).strftime("%Y%m%d")
    until = (anchor + timedelta(days=post_days)).strftime("%Y%m%d")
    seen: dict = {}
    for bgn_de, end_de in _dart_windows(since, until):
        for row in _dart_list(key, bgn_de, end_de, None, corp_code):
            report_nm = row.get("report_nm") or ""
            if any(pattern in report_nm for pattern in DART_LIFECYCLE_PATTERNS):
                seen[(row.get("rcept_no") or "").strip()] = row
    return [seen[rcept_no] for rcept_no in sorted(seen)]


def fetch_dart(
    keyword: str = "투자계약증권",
    since: str = DART_INVESTMENT_CONTRACT_EPOCH,
    until: str | None = None,
    pblntf_ty: str = "C",
    detail_ty: str | None = None,
    corp: str | None = None,
    corp_code: str | None = None,
    refresh_corp: bool = False,
    related: bool = False,
    related_pre_days: int = 7,
    related_post_days: int = 365,
    rcept_no: str | None = None,
    key: str | None = None,
    list_only: bool = False,
    force: bool = False,
    dest_dir: Path | None = None,
    stream=sys.stdout,
) -> list:
    """Fetch 증권신고서 bodies whose report_nm matches `keyword` into the inbox.

    Search axes: pblntf_ty/detail_ty (증권유형, server-side), corp/corp_code
    (company), since/until (period), and keyword (report_nm substring, comma =
    OR; pass an empty keyword to keep every row). With `related`, each matched
    증권신고서 also pulls its offering family (정정신고서/투자설명서/발행실적/철회)
    by corp_code + a time window, since DART has no offering identifier. Only the
    primary submitted document of each filing is fetched; the appraisal 첨부서류
    live behind the robots-disallowed dart.fss.or.kr viewer and are not
    retrievable via the API. Idempotent by rcept_no.
    """
    key = key or os.environ.get("DART_API_KEY")
    if not key:
        raise FetchError(
            "DART_API_KEY is not set. Register free at opendart.fss.or.kr (a "
            "personal crtfc_key is issued instantly) and put DART_API_KEY=<key> in .env."
        )
    if detail_ty and detail_ty not in DART_DETAIL_TYPES:
        raise FetchError(
            f"unknown --detail-ty {detail_ty!r}. 발행공시 codes: "
            + ", ".join(f"{code}={name}" for code, name in DART_DETAIL_TYPES.items())
        )
    until = until or date.today().strftime("%Y%m%d")
    directory = Path(dest_dir) if dest_dir else get_paths().inbox
    terms = _keyword_terms(keyword)

    corp_name = ""
    if corp and not corp_code:
        candidates = _resolve_corp(corp, _dart_corp_index(key, refresh=refresh_corp))
        if not candidates:
            raise FetchError(f"no DART corp matched {corp!r}. Try the exact registered name, or --refresh-corp.")
        corp_code, corp_name, stock_code = candidates[0]
        listed = f"stock {stock_code}" if stock_code else "non-listed"
        print(f"corp {corp!r} -> {corp_name} (corp_code {corp_code}, {listed})"
              + (f"; {len(candidates)} candidates, using the first" if len(candidates) > 1 else ""), file=stream)
        for entry in candidates[1:6]:
            print(f"    also: {entry[1]} (corp_code {entry[0]})", file=stream)

    # Direct-by-id path: fetch exactly the given rcept_no(s), skip the sweep. This
    # is the natural follow-up to --list-only (discover ids, then fetch them).
    if rcept_no:
        ids = [token.strip() for token in rcept_no.split(",") if token.strip()]
        rows = [{"rcept_no": ident, "corp_name": corp_name, "report_nm": ""} for ident in ids]
        print(f"fetching {len(rows)} filing(s) by rcept_no.", file=stream)
    else:
        anchors: list = []
        for bgn_de, end_de in _dart_windows(since, until):
            for row in _dart_list(key, bgn_de, end_de, pblntf_ty, corp_code, detail_ty=detail_ty):
                if _report_matches(row.get("report_nm") or "", terms):
                    anchors.append(row)

        scope = f"pblntf_ty={pblntf_ty}" + (f"/{detail_ty}" if detail_ty else "") + (f", corp_code={corp_code}" if corp_code else "")
        filt = "any OR of " + repr(terms) if terms else "no keyword filter"
        print(f"matched {len(anchors)} filing(s) [{filt}] ({since}..{until}, {scope}).", file=stream)

        # Expand each anchor into its offering family, union by rcept_no.
        by_rcept: dict = {(row.get("rcept_no") or "").strip(): row for row in anchors}
        if related:
            for anchor in anchors:
                anchor_code = (anchor.get("corp_code") or corp_code or "").strip()
                anchor_dt = (anchor.get("rcept_dt") or "").strip()
                if not anchor_code or not anchor_dt:
                    continue
                family = _dart_related(key, anchor_code, anchor_dt, related_pre_days, related_post_days)
                if anchor.get("rm") and any(flag in anchor["rm"] for flag in ("정", "철")):
                    print(f"  [{anchor.get('rcept_no')}] rm={anchor['rm']!r}: a 정정/철회 exists; widen "
                          "--related-post-days if the family looks short.", file=stream)
                print(f"  offering of {anchor.get('rcept_no')} ({anchor.get('corp_name')}): {len(family)} filing(s)", file=stream)
                for member in family:
                    print(f"      {member.get('rcept_dt')}  {member.get('rcept_no')}  {member.get('report_nm')}", file=stream)
                    by_rcept[(member.get("rcept_no") or "").strip()] = member

        rows = [by_rcept[ident] for ident in sorted(by_rcept)]

    print(f"downloading {len(rows)} filing(s) total.", file=stream)
    print(
        "note: only each filing 본문 is fetched. The 외부평가기관 평가보고서, 감정평가서 및 "
        "투자설명서 첨부 PDF are 첨부서류 on the robots-disallowed dart.fss.or.kr viewer "
        "and are intentionally NOT downloaded.",
        file=stream,
    )
    matches = rows

    landed: list = []
    for row in matches:
        rcept_no = (row.get("rcept_no") or "").strip()
        corp_name = (row.get("corp_name") or "").strip()
        report_nm = (row.get("report_nm") or "").strip()
        if list_only:
            print(f"  {rcept_no}  {corp_name}  {report_nm}", file=stream)
            continue
        if not force and any(directory.glob(f"*_{rcept_no}.txt")):
            print(f"  skip {rcept_no} ({corp_name}): already in the inbox", file=stream)
            continue
        text = _dart_document_text(key, rcept_no)
        if len(text.strip()) < 50:
            print(f"  skip {rcept_no} ({corp_name}): body has little extractable text", file=stream)
            continue
        # Label from the actual report_nm, not the search keyword, so it is
        # meaningful for any search axis. Idempotency rides on the _{rcept_no}
        # suffix, so the prefix is free to change.
        label = report_nm or "증권신고서"
        path = _land(f"{label}_{corp_name}_{rcept_no}.txt", text.encode("utf-8"), dest_dir)
        print(f"  landed {rcept_no}: {path.name}", file=stream)
        landed.append(path)
    return landed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    law = sub.add_parser("law", help="Fetch an enacted 법령 body (법제처 DRF, robots-clean).")
    law.add_argument("--query", required=True, help="법령명/행정규칙명, e.g. 가상자산이용자보호")
    law.add_argument("--target", default="law", choices=["law", "admrul"], help="법령(law) or 행정규칙(admrul, e.g. 고시/세칙).")
    law.add_argument("--oc", default=None, help="DRF OC (default env LAW_OC; pass 'test' for the shared test key).")
    law.add_argument("--ef-yd", default=None, help="Pin an enforcement-date version, YYYYMMDD (법령 only).")
    law.add_argument("--dest", default=None, help="Override the landing directory (default: the inbox).")

    bill = sub.add_parser("bill", help="Resolve a bill's metadata (open.assembly, robots-clean).")
    bill.add_argument("--name", default=None, help="의안명, e.g. 디지털자산기본법안")
    bill.add_argument("--bill-no", default=None, help="의안번호")
    bill.add_argument("--age", default="22", help="대수 (default 22).")
    bill.add_argument("--service", default=ASSEMBLY_BILL_SERVICE, help="open.assembly service (use TVBPMBILL11 for 대안).")
    bill.add_argument("--allow-likms", action="store_true", help="Also download the 원문 HWP from likms (robots-disallowed; see NOTICE).")
    bill.add_argument("--dest", default=None, help="Override the landing directory (default: the inbox).")

    dart = sub.add_parser("dart", help="Fetch 증권신고서 bodies from DART (default: 투자계약증권/조각투자).")
    dart.add_argument("--keyword", default="투자계약증권", help="report_nm substring(s), comma = OR; pass '' to keep all (default 투자계약증권).")
    dart.add_argument("--corp", default=None, help="Company name; resolved to a corp_code via the DART master.")
    dart.add_argument("--corp-code", default=None, help="Restrict to one 8-digit DART corp_code (skips name resolution).")
    dart.add_argument("--rcept-no", default=None, help="Fetch exactly these 14-digit receipt id(s), comma-separated; skips the sweep. Pair with --corp for the filename label.")
    dart.add_argument("--pblntf-ty", default="C", help="증권/공시 유형, top level (default C=발행공시; e.g. B=주요사항보고).")
    dart.add_argument("--detail-ty", default=None, help="발행공시 상세유형 code, e.g. C001 지분증권, C002 채무증권 (see --help detail).")
    dart.add_argument("--since", default=DART_INVESTMENT_CONTRACT_EPOCH, help="Sweep start YYYYMMDD (default 20230801).")
    dart.add_argument("--until", default=None, help="Sweep end YYYYMMDD (default: today).")
    dart.add_argument("--refresh-corp", action="store_true", help="Re-download the DART corp master before resolving --corp.")
    dart.add_argument("--related", action="store_true", help="Also fetch each 신고서's offering family (정정/투자설명서/발행실적/철회).")
    dart.add_argument("--related-pre-days", type=int, default=7, help="Family window before the anchor's 접수일 (default 7).")
    dart.add_argument("--related-post-days", type=int, default=365, help="Family window after the anchor's 접수일 (default 365).")
    dart.add_argument("--list-only", action="store_true", help="Print matching filings, download nothing.")
    dart.add_argument("--force", action="store_true", help="Re-download even if the rcept_no is already in the inbox.")
    dart.add_argument("--dest", default=None, help="Override the landing directory (default: the inbox).")

    args = parser.parse_args(argv)
    try:
        if args.command == "law":
            path = fetch_law(args.query, oc=args.oc, ef_yd=args.ef_yd, dest_dir=args.dest, target=args.target)
            print(f"landed: {path}")
        elif args.command == "dart":
            fetch_dart(
                keyword=args.keyword,
                since=args.since,
                until=args.until,
                pblntf_ty=args.pblntf_ty,
                detail_ty=args.detail_ty,
                corp=args.corp,
                corp_code=args.corp_code,
                refresh_corp=args.refresh_corp,
                related=args.related,
                related_pre_days=args.related_pre_days,
                related_post_days=args.related_post_days,
                rcept_no=args.rcept_no,
                list_only=args.list_only,
                force=args.force,
                dest_dir=args.dest,
            )
        elif args.command == "bill":
            fetch_bill(
                name=args.name,
                bill_no=args.bill_no,
                age=args.age,
                service=args.service,
                allow_likms=args.allow_likms,
                dest_dir=args.dest,
            )
    except FetchError as error:
        print(f"fetch failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
