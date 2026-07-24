"""Profile for 법령 / 행정규칙: an enacted statute, decree, rule or 고시 body.

Data only. Four tables of compiled regexes and nothing else at module level.

The DRF-fetched statute text is a run of 제N조(제목) articles with NO 의안번호 and
NO 제안이유 -- exactly what separates it from a 법률안 (bill), whose enacted text
also carries titled articles. So the bill markers are the disqualifiers here and
the negatives do the discrimination. 행정규칙(고시/세칙) number their articles
제N-M조 (편-조), so every 조 pattern allows the optional `-M`.
"""

import re

from pipeline.doctypes import EdgeRule, Marker

DOC_TYPE = "statute"

# A titled article is what an enacted statute body is made of. REQUIRE alone
# cannot separate it from a bill's enacted text; the REJECTs below do that.
REQUIRE = (
    re.compile(r"제\s*\d+(?:\s*-\s*\d+)?\s*조(?:\s*의\s*\d+)?\s*\([^)\r\n]{1,80}\)"),
)

# None may match. 의안번호/제안이유 send a document to bill.py (declared first);
# rejecting them here keeps the two profiles mutually exclusive (the gate checks
# exclusivity). 주문/청구취지 = 판결문, 갑과 을 = 계약서. An ATX markdown heading
# (`## 제11조(벌칙)`) marks a hand-written .md memo, not a DRF-fetched statute
# body -- the enacted text arrives as plain 텍스트 with 제N조 at the line start and
# never as markdown, so this REJECT keeps the smoke fixtures (korean-penalty.md,
# korean-terms.md, both titled-article memos) generic without touching the real
# .txt corpus.
REJECT = (
    re.compile(r"의\s*안[\s\r\n]*번\s*호"),
    re.compile(r"^[ \t]*제?\s*안\s*이\s*유", re.MULTILINE),
    re.compile(r"^[ \t]*주\s*문[\s\r]*$", re.MULTILINE),
    re.compile(r"[「\"']?갑[」\"']?\s*과\s*[「\"']?을[」\"']?"),
    re.compile(r"^#{1,6}\s", re.MULTILINE),
)

MARKERS = (
    Marker(
        role="zone",
        label="부칙",
        pattern=re.compile(r"^[ \t]*부\s*칙[ \t]*(?:\([^)\r\n]{0,40}\))?[ \t\r]*$", re.MULTILINE),
    ),
    Marker(
        role="section",
        label="편",
        pattern=re.compile(r"^[ \t]*제\s*\d+\s*편\s*[^\r\n]{1,60}[ \t\r]*$", re.MULTILINE),
    ),
    Marker(
        role="section",
        label="장",
        pattern=re.compile(r"^[ \t]*제\s*\d+\s*장\s*[^\r\n]{1,60}[ \t\r]*$", re.MULTILINE),
    ),
    Marker(
        role="section",
        label="조",
        pattern=re.compile(
            r"^[ \t]*제\s*\d+(?:\s*-\s*\d+)?\s*조(?:\s*의\s*\d+)?\s*\([^)\r\n]{1,80}\)",
            re.MULTILINE,
        ),
    ),
)

# A 시행령/시행규칙/감독규정 names its enabling law once, in 제1조(목적):
# "이 규칙은 「자본시장과 금융투자업에 관한 법률」 및 같은 법 시행령에서 위임된
# 사항...". That is the real 위임 (delegation) the ontology's delegates_to=1.0 is
# for; the enabling-law node is minted from {law}. The parent 법률 itself never
# says this, so it emits no self-delegation. References ("「법」에 따른") are NOT
# extracted here: one 시행령 carries dozens (자본시장법 시행령 has 80), which would
# flood the graph -- add them later only with a graph-eval measurement.
EDGES = (
    EdgeRule(
        relation="delegates_to",
        pattern=re.compile(
            r"「(?P<law>[^」\r\n]{2,60}?법(?:률)?)\s*」\s*(?:및\s*같은\s*법[^\r\n]{0,25})?\s*에서\s*위임"
        ),
        source="{doc_id}",
        source_kind="document",
        target="{law}",
        target_kind="statute",
    ),
)
