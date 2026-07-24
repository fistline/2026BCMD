# Korean document-type markers, and the traps in each

Reference for `doctype-profile-authoring`. Loaded on demand; the skill body does
not repeat any of it.

Every pattern below assumes `re.MULTILINE`, `[ \t]*` after `^`, and `[ \t\r]*$`
before `$`. The `\r` is not optional: extractors emit CRLF, `$` matches before
`\n` only, and a `[ \t]*$` pattern therefore returns **nothing** on real input
while passing every test written against a file you read yourself. The gate's
CRLF-twin check exists because this failure is invisible otherwise — it caught
the shipped `bill` profile.

Put `\s*` between the syllables of every label (`주\s*문`, `부\s*칙`). Official
Korean documents letter-space headings for typographic reasons, and the spacing
is not consistent between producers.

---

## 의안 / 법률안 — shipped as `bill.py`

| Zone | Marker | Notes |
| --- | --- | --- |
| 제안이유 | `^제?\s*안\s*이\s*유[ \t\r]*$` | `once=True` |
| 주요내용 | `^주\s*요\s*내\s*용[ \t\r]*$` | `once=True` |
| 본칙 | `^(?:제\s*1\s*편\|제\s*1\s*장\|제\s*1\s*조\s*\()` | `once=True`. There is **no** `법률 제N호` line in real bills — verified across 8. |
| 부칙 | `^부\s*칙[ \t]*(?:\([^)\r\n]{0,40}\))?[ \t\r]*$` | Not `once`: a bill amending several statutes has several. |
| 신구조문대비표 | `^신\s*[·․.]?\s*구\s*조?\s*문?\s*대\s*비\s*표` | `once=True`. The separator is sometimes `·`, sometimes `․` (U+2024), sometimes `.` |

**Why 신구조문대비표 is a zone and not a discard.** Its left column is *repealed*
law. It must stay retrievable — you often want to see what changed — but it must
never share a heading with the article that replaced it. Without the zone,
`제1조(목적)` appears twice in one document and an agent citing it cannot tell
which is current. This is the single strongest argument for zone markers.

**REJECT before REQUIRE.** A bill, a statute and a contract all contain
`제N조(제목)` identically. Only the negatives discriminate: 주문/청구취지 mark a
판결문, and a `갑`/`을` preamble marks a 계약서.

---

## 판결문 — not shipped

No judgment exists in `data/raw/documents`, so no profile ships. Shipping one for
a type with zero instances means maintaining untested code, and the gate would
have nothing to check it against. Author it when the first judgment arrives.

| Section | Marker sketch | Trap |
| --- | --- | --- |
| 주문 | `^주\s*문[ \t\r]*$` | On its own line. `주문` also appears mid-sentence in 이유 ("주문과 같이 판결한다"), so anchor it and use `once=True`. |
| 청구취지 | `^청\s*구\s*취\s*지` | Civil only. Criminal judgments have no 청구취지, so it cannot be in REQUIRE. |
| 이유 | `^이\s*유[ \t\r]*$` | Two characters, extremely common as a substring. Anchoring is mandatory. |
| 별지 | `^별\s*지` | Often a table; treat as its own zone. |

REQUIRE should be 사건번호 (`\d{4}\s*[가-힣]{1,3}\s*\d+`) plus an anchored 주문.
사건번호 alone matches a bill that *cites* a case.

---

## 계약서 — not shipped

Same reasoning: none in the corpus.

| Element | Marker sketch | Trap |
| --- | --- | --- |
| 전문 | `[「"']?갑[」"']?\s*과\s*[「"']?을[」"']?` | This is the strongest positive signal and the strongest REJECT for a bill. |
| 조항 | `^제\s*\d+\s*조\s*\([^)\r\n]{1,80}\)` | **Identical** to a statute. Never put it in REQUIRE. |
| 서명란 | `^\s*\(?\s*갑\s*\)?\s*[:：]` | Contains party names — see the naming rule below. |

**The naming rule that matters most.** Never emit a bare `갑` or `을` as a node.
`silver.relations` de-duplicates on `(source_entity, relation, target_entity)`
with **no doc_id**, so a bare `갑` from one contract does not merely merge with
another contract's — it **deletes** the other's edge. Always namespace with
`{doc_id}`, which the engine's format strings support:
`source="{doc_id}:갑"`.

Entity resolution (A회사 ≡ A주식회사) does **not** belong in a profile. It lives
in `normalise_entity`, which is pinned by a smoke assertion to agree with
`document_id`. Merging inside a profile fragments the graph into ids no node
carries, and the blocking referential audit will still pass because
`gold.entities` manufactures a node for every edge endpoint.

---

## Checking your work

`uv run python -m pipeline.doctypes.gate --report` before and after. The numbers
that mean something:

- **marker recall** — your headed sections against the generic `_KO_SECTION_RE`
  count. Below 0.95 your profile is worse than no profile.
- **largest-section ratio** — above 0.5 means you matched near the top and
  swallowed the rest. This is the documented failure shape of generated
  extractors: they work on the samples the author saw and return nothing on the
  tail.

Span tiling and character coverage are **not** evidence. `sections()` emits
consecutive spans from 0 to `len(text)` by construction, so a profile matching 3
of 179 headings scores tiling=True and coverage=1.000, identical to an honest
one. That was measured; do not reintroduce those checks.
