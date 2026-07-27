"""sto-filing 스킬(증권신고서 작성)의 assertion 판정 규칙.

`gen-docs/st_prospectus/sto-filing/evals/evals.json` 의 assertion 문장을 앞부분으로 골라
기계 판정한다. assertion 을 고치면 여기 매칭 문구도 같이 고쳐야 한다 — 안 맞으면 그 항목은
조용히 통과하는 게 아니라 **보류(판단 필요)로 떨어진다.** 그렇게 설계했다.

규칙을 쓸 때의 원칙 두 가지. 둘 다 이 스킬을 채점하다 실제로 틀려서 얻은 것이다.

1. **같은 뜻의 다른 표현을 떨어뜨리지 않는다.** `채권적 청구권` 하나만 찾다가, 올바르게
   `수익 받을 계약상 권리`라고 쓴 출력을 실패로 셌다. 대안 표현을 `any_of` 로 묶는다.
2. **금지 표현은 문맥을 본다.** 신고서는 "원금·수익 보장과 무관함"을 **반드시 써야** 하므로
   `수익 보장` 키워드 검사는 필연적으로 오탐을 낸다. `banned_hits` 가 부정 표지를 걸러낸다.
"""
from __future__ import annotations

import re

# 1층 A 필수 8개 — 요약정보 핵심투자위험의 소제목으로 존재해야 한다.
# 근거와 측정 방법: sto-filing/references/reference-filings.md §4-1
TIER1 = {
    "효력발생": r"효력\s*발생",
    "환금성": r"환금성",
    "원금 미보장": r"원금\s*(미보장|손실|을 보장하지)",
    "공모가 산정한계": r"공모가.{0,10}산정",
    "미래예측진술": r"미래예측진술|장래 예측|forward.?looking",
    "일반청약자 배정": r"배정\s*방법|배정방식|균등\s*배정",
    "공모자금 보관": r"공모자금|청약증거금.{0,10}(예치|보관)",
    "이해상충": r"이해상충",
}

BANNED = [r"수익\s*보장", r"원금\s*손실\s*없", r"안정적\s*수익", r"안정적\s*현금흐름", r"확실한\s*수익"]


def filing_files(names: list[str]) -> list[str]:
    """증권신고서 초안으로 볼 수 있는 파일. 판정서·입력요청서·심사검토서는 신고서가 아니다."""
    return [n for n in names
            if re.search(r"증권신고서|prospectus|registration", n, re.I)
            and not re.search(r"판정서|입력요청서|심사", n)]


# 코퍼스 `sto` 컬렉션의 실물 6건. V-7 대조 기록을 확인할 때 이 이름들을 찾는다.
CORPUS_FILINGS = ["스탁키퍼", "투게더아트", "서울옥션블루", "열매컴퍼니", "소유_대전하나", "뮤직카우"]


def pick(files: dict, *pats: str) -> str:
    """파일명이 패턴에 맞는 첫 파일의 내용. 없으면 빈 문자열."""
    for name, content in files.items():
        if any(re.search(p, name) for p in pats):
            return content
    return ""


def judge(a: str, ctx: dict):
    """assertion 하나를 판정한다. (passed, evidence) 또는 판정 불가면 None."""
    text, names, files = ctx["text"], ctx["names"], ctx.get("files", {})
    has, any_of, banned_hits = ctx["has"], ctx["any_of"], ctx["banned_hits"]

    # ---- 공통 ----
    if "법률자문" in a:
        ok = bool(re.search(r"법률자문이 아니|법률 자문이 아니|법률자문을 대체", text))
        return ok, "산출물에 법률자문 아님 고지 " + ("발견" if ok else "없음")

    if "신고서 초안 파일을 생성하지 않는다" in a:
        f = filing_files(names)
        return not f, f"생성된 신고서류 파일: {f or '없음'}"

    # ---- eval1 · 유형 판정 ----
    if "네 항목이 모두 있다" in a:
        miss = [n for n in ["증권 해당 여부", "증권 유형", "신뢰도", "진행 가능"] if n not in text]
        return not miss, f"누락: {miss or '없음'}"

    if "제6조제5항" in a and "운용" in a:
        ok = has(text, r"제6조\s*제5항|§\s*6⑤|제6조제5항", r"운용")
        return ok, "§6⑤ + '운용' 동시 " + ("존재" if ok else "미확인")

    if "공유지분으로 구성할지 채권적" in a:
        # '채권적 청구권' 외에 '계약상 권리'·'수익 받을 권리' 등으로 쓴다.
        # 확정하지 않고 선택지를 제시하거나 사용자에게 묻는 것도 assertion 이 허용한다.
        share = re.search(r"공유지분", text)
        claim = any_of(text, r"채권적", r"계약상\s*권리", r"수익\s*받을\s*권리", r"청구권")
        return bool(share and claim), \
            f"공유지분 {'○' if share else '×'} · 채권적/계약상 권리 대안 {'○' if claim else '×'}"

    # ---- 공유물 분할금지 (eval1·2 공통) ----
    if "공유물 분할금지" in a:
        n = len(re.findall(r"공유물\s*분할", text))
        if "한계" in a:
            ok = n > 0 and any_of(text, r"존속기간|3년|해지", r"승계")
            return ok, f"'공유물 분할' {n}회 · 한계(존속기간·해지·승계) " + ("포함" if ok else "미포함")
        return n > 0, f"'공유물 분할' {n}회 언급"

    # ---- eval2 · 기획 모드 전체 작성 ----
    if "_시뮬" in a:
        hit = [n for n in names if "_시뮬" in n]
        return bool(hit), f"_시뮬 파일: {hit or '없음'}"

    if "워터마크" in a and "가정치" in a:
        n = len(re.findall(r"가정치·시뮬레이션 — 제출용 아님", text))
        return n > 0, f"워터마크 {n}회"

    if "투자결정시 유의사항" in a and "표제부 직후" in a:
        ok = bool(re.search(r"투자\s*결정\s*시\s*유의사항|투자결정시 유의사항", text))
        return ok, "유의사항 블록 " + ("발견" if ok else "없음")

    if "다른 증권 형태로" in a:
        ok = has(text, r"다른 (증권|형태)", r"법률검토|변호사")
        return ok, "보충성 논거 + 법률검토 참조 " + ("존재" if ok else "없음")

    if "1층 필수 위험 8개" in a:
        miss = [k for k, p in TIER1.items() if not re.search(p, text)]
        return not miss, f"누락 {len(miss)}개: {miss or '없음'}"

    if "감시·감독 외부기관 부재" in a or ("감시자" in a and "겸영" in a):
        ok = has(text, r"감시|감독", r"겸영")
        return ok, "감시자 부재 + 겸영 " + ("둘 다 존재" if ok else "누락")

    if "값출처" in a:
        csv = [n for n in names if n.endswith(".csv")]
        ok = bool(csv) and "가정" in text
        return ok, f"CSV {csv or '없음'} · '가정' 표기 {'있음' if '가정' in text else '없음'}"

    if "보장성 표현" in a:
        hit = banned_hits(text, BANNED)
        return not hit, ("긍정형 보장 표현 없음 (부정·경고형 용례는 제외)" if not hit
                         else f"검출 {len(hit)}건: {hit[:2]}")

    if "손실(하락) 시나리오" in a:
        ok = has(text, r"하락", r"상승")
        return ok, "하락·상승 시나리오 " + ("병기" if ok else "미확인")

    # 심사 결과를 두 축으로 나눴는가 (common-core §6). 같은 프롬프트의 두 실행이 이 정책을
    # 반대로 잡아 지적 9건 대 27건으로 갈렸던 자리라, 표현 변주를 넉넉히 받는다.
    if "두 축으로 나눠 보고한다" in a:
        defect = any_of(text, r"기재\s*결함", r"기재상\s*결함")
        missing = any_of(text, r"미확보\s*사실", r"미확보\s*F")
        return bool(defect and missing), \
            f"'기재 결함' {'○' if defect else '×'} · '미확보 사실' {'○' if missing else '×'}"

    if "지적 건수를 부풀리지 않는다" in a:
        # 묶음 보고의 표지는 '묶음' 이라는 단위. 개별 나열이면 이 단어가 나올 이유가 없다.
        ok = bool(re.search(r"미확보\s*사실\s*\d+\s*묶음|\d+\s*묶음", text))
        return ok, "미확보 사실을 묶음 단위로 보고 " + ("확인" if ok else "미확인")

    # ---- 심사검토서의 산출·보고 (파일별 접근이 필요한 것들) ----
    if "약관이 아직 작성되지 않았다는" in a:
        review = pick(files, r"심사검토서")
        if not review:
            return None  # 심사검토서가 없으면 이 항목은 사람이 본다
        # 줄 전체가 아니라 **판정 셀**만 읽는다. 실물 심사검토서는 비고란에
        # "스킬 규정에 따라 미충족으로 판정하지 않는다"라고 쓰는데, 줄을 통째로 훑으면
        # 그 부정형을 미충족 판정으로 세게 된다 — 실제로 그렇게 오탐이 났다.
        verdicts = []
        for line in review.splitlines():
            if "X-10" not in line or "|" not in line:
                continue
            for cell in line.split("|"):
                c = cell.replace("*", "").strip()
                if c in ("충족", "부분충족", "미충족", "해당없음"):
                    verdicts.append(c)
        if not verdicts:
            return None  # 표 형식이 아니면 판정하지 않는다
        return "미충족" not in verdicts, f"X-10 판정 셀: {verdicts}"

    if "분량" in a and ("근거로 사용하지 않는다" in a or "미충족 근거" in a):
        review = pick(files, r"심사검토서")
        # 미충족·부분충족 판정을 내린 줄에 분량·비율·개수 논거가 섞였는지만 본다.
        # 문서가 '분량은 기준이 아니다'라고 쓰는 것은 위반이 아니므로 판정줄로 한정한다.
        bad = [l for l in review.splitlines()
               if re.search(r"미충족|부분충족", l) and re.search(r"분량|전체의 \d+\s*%|개수 (미달|부족)", l)]
        return not bad, ("판정줄에 분량·개수 논거 없음" if not bad else f"검출 {len(bad)}건: {bad[0][:70]}")

    if "집계표" in a and "미충족 항목의 ID" in a:
        # 집계표에 A-1·W-3 같은 항목 ID 가 실제로 나열돼야 한다. 개수만 있으면 어느 항목을
        # 봤는지 확인할 수 없고, 다음 심사와 비교도 안 된다.
        ids = re.findall(r"\b[A-EVXYZW]-\d{1,2}\b", text)
        ok = bool(re.search(r"집계", text)) and len(set(ids)) >= 5
        return ok, f"'집계' {'○' if re.search(r'집계', text) else '×'} · 항목 ID {len(set(ids))}종"

    # ---- eval3 · 신탁 게이트 ----
    if "제110조" in a:
        ok = bool(re.search(r"제110조|§\s*110", text))
        return ok, "§110 인용 " + ("있음" if ok else "없음")

    if "재설계 대안" in a:
        ok = any_of(text, r"투자계약증권.{0,20}(전환|재설계)", r"위탁자로 참여", r"대안")
        return ok, "재설계 대안 제시 " + ("확인" if ok else "미확인")

    if "영구 불가" in a:
        ok = any_of(text, r"하위법규|제도화|개정", r"바뀔 수 있")
        return ok, "제도 변화 여지 언급 " + ("있음" if ok else "없음")

    if "제110조를 개정하지 않았다" in a or "토큰증권 제도화법" in a:
        ok = any_of(text, r"110.{0,30}개정하지", r"미개정", r"확인하지 (않|못)")
        return ok, "§110 미개정 사실 또는 미확인 고지 " + ("있음" if ok else "없음")

    if "신탁업 인가" in a:
        ok = bool(re.search(r"신탁업\s*인가", text))
        return ok, "수탁자 신탁업 인가 확인 " + ("있음" if ok else "없음")

    return None
