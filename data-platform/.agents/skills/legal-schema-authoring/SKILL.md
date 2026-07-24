---
name: legal-schema-authoring
description: Extend the graph's relation-weight + entity-kind ONTOLOGY with legal-specific relations (위임/개정/준용 and friends) and node kinds — as a committed, reviewed, MEASURED diff authored once offline. Use when the legal graph's path-ranking is wrong (a dominant relation like delegates_to sitting at the ELSE 0.3 weight tier), when adding a legal relation or node kind (준용 / 조문 containment / 당사자), or when reconciling live extracted relations with the committed weight table. This is NOT extraction (which tokens become edges — that is doctype-profile-authoring); it decides which relation strings are legal-vocabulary and how load-bearing each is. It never designs or mutates the schema at runtime; it drafts a diff a human commits.
---

# Authoring the legal graph ontology

The graph's schema is TWO committed data sites: the relation→weight CASE in
`transform/models/gold/relations.sql` and the node-`kind` flow in
`gold/entities.sql`. This skill extends THAT ontology for legal documents. It is
the ontology counterpart to `doctype-profile-authoring`: that skill decides which
text tokens EMIT a relation; this one decides which relation strings are
legal-vocabulary and their weight.

Two stages, never one:

- **Stage 1 — the committed baseline.** The current canonical ontology. `make
  build` produces it model-free and deterministically; the blocking audits
  (`assert_no_self_loops`, `assert_relation_endpoints_resolved`) gate it.
- **Stage 2 — this skill, in Claude Code / Codex.** Read Stage-1's BUILT graph,
  find where the generic vocabulary under-serves the legal corpus, and DRAFT an
  ontology diff. Measure it with `make eval-graph`. A human reviews and commits.
  **The agent never mutates the live schema and never self-applies.** A per-run,
  model-authored weight/kind table would break three guarantees at once:
  make-build-calls-no-model, the ONE canonical schema (invariant 4), and
  measure-before-adding.

## The motivating divergence (confirm it still holds)

`pipeline/doctypes/bill.py` emits `delegates_to` (위임) and `amends` (개정), and
`delegates_to` is the DOMINANT edge in the live graph — yet neither appears in the
weight CASE, so both fall to ELSE 0.3, indistinguishable from `mentions` noise:
path-ranking is effectively off for the legal graph. Confirm with `make eval-graph`
and watch the `delegation` kind's `path_strength`.

## Procedure

0. **Boundary and one-off check.** Is this an ONTOLOGY change (which relation
   strings are legal-vocabulary, their weight, which node kinds exist) or an
   EXTRACTION change (which tokens in a doc-type emit them)? Extraction routes to
   `doctype-profile-authoring` and stops here. A throwaway analysis goes to the
   scratchpad and stops — the CASE and kind flow are code someone maintains forever.

1. **Measurement gate (now built — keep it green).** `pipeline/eval_graph.py` +
   `make eval-graph` + `pipeline/eval_graph_baseline.json` already exist:
   judgments anchored on node SLUGS + relation type (never on transient
   degree/rank), scored weight-aware (`path_strength` moves with an absolute
   weight change; `mrr`/`p@1` move with reordering). Every ontology change must
   move the graph floor in the intended direction with no regression. If your
   change targets a relation with no judgment, ADD one first — a real 위임/준용
   chain anchored on slugs.

2. **Measure the divergence.** Diff the live relations against the weight table:
   `SELECT relation, COUNT(*), MIN(weight) FROM edges GROUP BY relation ORDER BY
   COUNT(*) DESC` versus the CASE at `gold/relations.sql`. Name every relation
   string falling to ELSE 0.3.

3. **Draft the ontology diff as committed DATA.** Extend the SINGLE CASE in
   `gold/relations.sql` (never a parallel legal table). Keep it lean (~5-15
   relation types). For any new `entity_kind` an edge asserts, prove
   `normalise_entity` / `document_id` actually mints that slug, or the edge dangles
   at an id no node carries.

4. **Normalize first.** 준용/인용 edges are only as good as name and number
   normalization; add `aliases.tsv` rows (제N조 to article id, abbreviated law
   names, '같은 법' expansion) BEFORE the bronze→silver dedup, each with a
   provenance article and a measurement; record rejections in `excluded.tsv`.

5. **Gate.** `make build`, `make eval-graph`, `make verify`. The audits stay
   blocking. The graph floor must improve on the seeded 위임/준용 judgments and no
   retrieval floor may regress. If an audit fails, fix the draft, never the audit.

6. **Report a diff; a human merges.** State the reachability delta, which
   relations moved off the 0.3 ELSE tier and to where, any new kind plus proof a
   node carries it, and alias rows added or rejected. Do not self-apply.

## Proposed legal ontology (lean — add a type only with a measurement)

| relation | 의미 | weight | maps onto |
| --- | --- | --- | --- |
| `delegates_to` | 위임 (decree ← enabling law) | 1.0 | depends_on tier — **the divergence fix** |
| `amends` | 개정 | 0.6 | supersedes tier |
| `applies_mutatis_mutandis` | 준용 (imports a provision's rules) | 0.7 | NEW — above references, below defines |
| `contains` | 조·항·호 structural spine | 1.0 | new structural label |
| `supersedes` / `defines` / `references` | 폐지 / 정의 / 인용 | 0.6 / 0.8 / 0.5 | reuse verbatim |
| `mentions` | weak name-drop | 0.3 | leave at ELSE unless measured |
| deontic (`imposes_obligation` / `grants_right` / `sanctions`), `held_by` | 의무·권리·벌칙 / 당사자 | 0.8 / 0.4 | add only on measured need |

Entity kinds (keep ~3-7): `document`, `statute`, `provision` (조/항/호), `term`,
`role`; `case` (판례) deferred until a case-law corpus exists.

## Constraints

Schema is committed DATA, authored once offline — `make build` never calls a model
to design it. ONE canonical schema: extend the single weight CASE and kind flow,
never a parallel legal schema (invariant 4). Draft-then-approve: propose a diff
gated by `make verify`; a human commits. No dangling edges: every kind an edge
asserts must be a slug `normalise_entity` / `document_id` mints. Respect the
extraction boundary: which tokens become edges is `doctype-profile-authoring`;
which strings are legal-vocabulary and how load-bearing is here. Canonicalize
deterministically at silver→gold (LOWER/TRIM plus explicit membership), never a
free-form model-emitted relation string.
