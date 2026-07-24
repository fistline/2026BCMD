"""Profile for 의안 / 법률안: a National Assembly bill.

Data only. Four tables of compiled regexes and nothing else at module level —
the gate's purity check rejects any other top-level statement.

Every pattern carries `\\r` in its line classes and `\\s*` between the syllables
of a label. Official Korean documents letter-space their headings (`주 문`), and
an extractor that emits CRLF makes a `$`-anchored pattern silently match nothing,
which is the failure that looks like "the profile just did not fire".
"""

import re

from pipeline.doctypes import EdgeRule, Marker

DOC_TYPE = "bill"

# All must match. 의안번호 plus a proposal-reason heading is what a bill has and
# a statute, a contract and a judgment do not.
REQUIRE = (
    re.compile(r"의\s*안[\s\r\n]*번\s*호"),
    re.compile(r"^[ \t]*제?\s*안\s*이\s*유", re.MULTILINE),
)

# None may match. Written before REQUIRE on purpose: 계약서 and 법령 share
# `제N조(제목)` with a bill exactly, so only the negatives discriminate.
REJECT = (
    re.compile(r"^[ \t]*주\s*문[\s\r]*$", re.MULTILINE),      # 판결문
    re.compile(r"^[ \t]*청\s*구\s*취\s*지", re.MULTILINE),     # 판결문
    re.compile(r"[「\"']?갑[」\"']?\s*과\s*[「\"']?을[」\"']?"),  # 계약서 preamble
)

# Zones partition the document; sections are headings inside the current zone.
# 신구조문대비표 is a zone rather than being dropped, because its text is
# repealed law: it must stay retrievable but must never share a heading with the
# live article that replaced it.
MARKERS = (
    Marker(
        role="zone",
        label="제안이유",
        pattern=re.compile(r"^[ \t]*제?\s*안\s*이\s*유[ \t\r]*$", re.MULTILINE),
        once=True,
    ),
    Marker(
        role="zone",
        label="주요내용",
        pattern=re.compile(r"^[ \t]*주\s*요\s*내\s*용[ \t\r]*$", re.MULTILINE),
        once=True,
    ),
    Marker(
        role="zone",
        label="본칙",
        # Where the enacted text starts. Real bills open the body with 제1편 or
        # 제1장 rather than a "법률 제N호" line, which does not appear at all.
        # once=True because 제1장 recurs under later 편, and a second match would
        # re-partition the document in the middle of the statute.
        pattern=re.compile(
            r"^[ \t]*(?:제\s*1\s*편|제\s*1\s*장|제\s*1\s*조\s*\()",
            re.MULTILINE,
        ),
        once=True,
    ),
    Marker(
        role="zone",
        label="부칙",
        pattern=re.compile(r"^[ \t]*부\s*칙[ \t]*(?:\([^)\r\n]{0,40}\))?[ \t\r]*$", re.MULTILINE),
    ),
    Marker(
        role="zone",
        label="신구조문대비표",
        pattern=re.compile(r"^[ \t]*신\s*[·․.]?\s*구\s*조?\s*문?\s*대\s*비\s*표", re.MULTILINE),
        once=True,
    ),
    Marker(
        role="zone",
        label="비용추계서",
        pattern=re.compile(r"^[ \t]*비\s*용\s*추\s*계\s*서", re.MULTILINE),
        once=True,
    ),
    Marker(
        role="section",
        label="조",
        pattern=re.compile(
            r"^[ \t]*제\s*\d+\s*조(?:\s*의\s*\d+)?\s*\([^)\r\n]{1,80}\)",
            re.MULTILINE,
        ),
    ),
    Marker(
        role="section",
        label="장",
        pattern=re.compile(
            r"^[ \t]*제\s*\d+\s*장\s*[^\r\n]{1,60}[ \t\r]*$",
            re.MULTILINE,
        ),
    ),
)

# Bills cite the statutes they amend and the laws they delegate to. Both are
# namespaced with {doc_id} so one bill's edge can never overwrite another's:
# silver.relations de-duplicates on (source, relation, target) with no doc_id.
EDGES = (
    EdgeRule(
        relation="amends",
        pattern=re.compile(r"「(?P<law>[^」\r\n]{2,60}?법(?:률)?)\s*」\s*일부를?\s*개정"),
        source="{doc_id}",
        source_kind="document",
        target="{law}",
        target_kind="statute",
    ),
    EdgeRule(
        # "「A법」에 따른/따라" is the bill CITING another law's provisions -- a
        # reference (인용), NOT delegation (위임). A bill referencing 기간제법 or
        # 국정감사법 in a definition is not delegating authority to it. Real 위임
        # (a decree empowered by its enabling law) lives in 시행령/시행규칙 text,
        # so delegates_to is reserved for a future 법령 profile; this is references.
        relation="references",
        pattern=re.compile(r"「(?P<law>[^」\r\n]{2,60}?법(?:률)?)\s*」\s*에\s*따[른라]"),
        source="{doc_id}",
        source_kind="document",
        target="{law}",
        target_kind="statute",
    ),
)
