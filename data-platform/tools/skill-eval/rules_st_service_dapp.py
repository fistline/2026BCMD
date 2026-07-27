"""st-service-dapp 스킬(ST/RWA 서비스 dApp 생성)의 assertion 판정 규칙.

이 스킬의 assertion 상당수는 **스킬 고유 어휘(S1~S6)** 를 요구한다. 그래서 기계 채점이
"내용이 맞는가"가 아니라 "같은 말로 썼는가"를 재기 쉽다. 두 가지로 대응한다.

1. **어휘를 요구하는 항목은 그대로 판정한다.** S1~S6 은 이 스킬이 정한 규약이고,
   후속 PHASE 가 그 판정을 받아 쓰므로 어휘 일치 자체가 요구사항이다.
2. **다만 그것이 우열의 증거는 아니다.** 스킬 없이 답한 쪽이 ERC-3643(T-REX)·ERC-1155
   같은 다른 어휘로 근접한 답을 낼 수 있다. 점수 차이를 "스킬이 더 옳다"로 읽으면 안 되고,
   이 파일은 그 판단을 하지 않는다 — 판정 근거만 evidence 에 남긴다.

판단이 필요한 항목(근거 인용의 환언, 질의를 몰아서 했는가, 부가 확장을 '언급'했나
'붙였나')은 규칙을 두지 않는다. 느슨한 정규식으로 통과시키면 채점이 무의미해진다.
"""
from __future__ import annotations

import re

# 부가 확장 — 요구사항에 없으면 붙이지 않는다.
EXTENSIONS = {"ERC-4626": r"ERC-?4626", "ERC-2612": r"ERC-?2612", "Votes": r"ERC-?20\s*Votes|Votes"}

# 생성물로 볼 수 있는 것. 판정만 요청받았으면 이런 게 나오면 안 된다.
CODE_SUFFIX = re.compile(r"\.(sol|ts|tsx|json|toml)$", re.I)


def judge(a: str, ctx: dict):
    """assertion 하나를 판정한다. (passed, evidence) 또는 판정 불가면 None."""
    text, names = ctx["text"], ctx["names"]

    if "컨트랙트 코드나 프로젝트 폴더를 생성하지 않는다" in a or "곧바로 코드 생성에 들어가지 않는다" in a:
        code = [n for n in names if CODE_SUFFIX.search(n)]
        return not code, f"산출된 코드·설정 파일: {code or '없음'} (전체 {len(names)}개)"

    if "S1~S6 중 하나로 명확히 판정한다" in a:
        # 판정문에 등급이 있어야 한다. 후보 나열만으로는 판정이 아니다.
        verdict = re.search(r"(S[1-6])\s*[=:(]|판정[^\n]{0,20}(S[1-6])|(S[1-6])\s*로 판정", text)
        return bool(verdict), ("판정 표기 " + verdict.group(0)[:24] if verdict
                               else f"S등급 판정문 없음 (S 언급 {len(re.findall(r'S[1-6]', text))}회)")

    m = re.search(r"판정 결과가 (S[1-6])\(([^)]+)\)이다", a)
    if m:
        grade, spec = m.group(1), m.group(2)
        # 해당 등급이 '결론'으로 쓰였는지 본다 — 다른 등급이 더 자주 나오면 결론이 아니다.
        counts = {g: len(re.findall(rf"\b{g}\b", text)) for g in [f"S{i}" for i in range(1, 7)]}
        top = max(counts, key=counts.get) if any(counts.values()) else None
        core = re.search(r"ERC-?\d+", spec)
        core_ok = bool(core and re.search(core.group(0).replace("-", "-?"), text))
        ok = top == grade and core_ok
        return ok, f"등급 빈도 {counts} · 최다 {top} · 핵심표준({core.group(0) if core else '?'}) {'○' if core_ok else '×'}"

    if "ERC-7943을 적용하지 않는다고 명시한다" in a:
        ok = bool(re.search(r"ERC-?7943[^\n]{0,60}(않|불필요|제외|미적용|넣지)", text)
                  or re.search(r"(않|불필요|제외|미적용|넣지)[^\n]{0,40}ERC-?7943", text))
        return ok, "ERC-7943 미적용 명시 " + ("확인" if ok else "미확인")

    if "5가지 서비스 예시를 선택지로 제시한다" in a:
        # 번호·표 어느 형식이든 5개 후보가 서 있으면 된다.
        numbered = len(re.findall(r"^\s*(?:[1-5]\.|\|\s*[A-E]?[1-5]\s*\|)", text, re.M))
        ok = numbered >= 5
        return ok, f"열거 항목 {numbered}개 (5개 이상 필요)"

    if "4개 고정 설정" in a and "묻지 않는다" in a:
        # 고정 설정을 '질문'했는지 본다. 설명·전제로 언급하는 것은 위반이 아니다.
        asked = [k for k in ["MockUSDC", "SQLite", "Token Standard", "체인"]
                 if re.search(rf"{k}[^\n]{{0,40}}(무엇|어떤|어느|선택해|골라|알려주|원하시)", text)]
        return not asked, f"고정 설정 질의: {asked or '없음'}"

    return None
