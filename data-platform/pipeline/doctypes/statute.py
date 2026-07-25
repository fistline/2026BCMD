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
# Anchored at LINE START (^, MULTILINE): an enacted statute prints 제N조(제목) as
# a heading at column 0, whereas a soft-law 가이드라인 only mentions 제N조(...) mid
# sentence as an inline citation. Without the anchor those guidelines were claimed
# and then sectioned into one degenerate span; the anchor lets them fall through to
# the default chunker instead.
REQUIRE = (
    re.compile(
        r"^[ \t]*제\s*\d+(?:\s*-\s*\d+)?\s*조(?:\s*의\s*\d+)?\s*\([^)\r\n]{1,80}\)",
        re.MULTILINE,
    ),
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

# A 시행령/시행규칙/감독규정/세칙 names its enabling law once, in 제1조(목적):
# "이 규칙은 「자본시장과 금융투자업에 관한 법률」 및 같은 법 시행령에서 위임된
# 사항...". That is the real 위임 (delegation) the ontology's delegates_to=1.0 is
# for; the enabling-law node is minted from the FIRST 「...법」 (the parent 법률;
# any 「시행령」/「감독규정」 named after it is a co-source, not the target).
#
# The match is ANCHORED on the 목적 opening "이 (영|규정|세칙|규칙)은" and the whole
# clause must stay on one line. That anchor is the precision guard: a 부칙 ◇제정사유
# zone can also say "「금소법」...에서 위임된 사항" (정보처리위탁규정 cites a law that is
# NOT its 모법), but it is not introduced by "이 규정은", so it is correctly ignored.
# The parent 법률 itself never opens this way, so it emits no self-delegation.
# Recipient/조문 refs between the law and 위임 (예: "...에서 금융위원회에 위임한",
# "제4조부터 제5조의4까지 및 동법 시행령...에서 위임된") are tolerated, and 위임된/
# 위임한/위임하는 all count. References ("「법」에 따른") are still NOT extracted: one
# 시행령 carries dozens (자본시장법 시행령 has 80), which would flood the graph.
EDGES = (
    EdgeRule(
        relation="delegates_to",
        pattern=re.compile(
            r"이\s*(?:영|규정|세칙|규칙)\s*은[^\r\n]{0,12}?"
            r"「(?P<law>[^」\r\n]{2,60}?법(?:률)?)\s*」"
            r"[^\r\n]{0,240}?에서[^\r\n]{0,45}?위임(?:된|한|하는)"
        ),
        source="{doc_id}",
        source_kind="document",
        target="{law}",
        target_kind="statute",
    ),
)
