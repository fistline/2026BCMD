---
name: correction-harvesting
description: Turn a discovered retrieval MISS into a measured, governed fix. Use when hybrid_search returned the wrong passage, missed an article it should have found, or someone corrected a grounded answer — the correction becomes a heading-anchored eval case first, then (only if a synonym gap is proven) a cited aliases.tsv row, else an excluded.tsv rejection. The default outcome is an eval plus a rejection, NOT a new alias. Offline and operator-run; make build never calls a model.
---

# Correction harvesting

Anthropic's analytics agents turn every stakeholder correction into a new eval
plus a reference-doc fix. This project is offline and single-operator, so the same
loop runs by hand — and it already has every durable store it needs: heading-
anchored evals in `pipeline/eval_queries.json`, a measurement-gated synonym table
in `pipeline/aliases.tsv`, and a rejection log in `pipeline/excluded.tsv`. Do NOT
add a `corrections.jsonl` backlog; those three files ARE the record, tracked in
git, and a fourth queue would drift.

The bias is toward REJECTION. On this corpus the measured ratio is roughly three
rejected equivalences to ten accepted (`excluded.tsv`), and every alias costs a
retrieval pass and dilutes fusion (`aliases.tsv` header). A miss is far more often
a missing eval, or a deliberate non-equivalence, than a missing alias.

## Procedure

1. **Write the miss down as an eval, not an alias.** Add a heading-anchored entry
   to `pipeline/eval_queries.json` with the right `kind` (vocabulary_match /
   synonym_gap / particle_glue / cross_bill / negative). Anchor `relevant` by a
   HEADING REGEX (the 제N조 or 주문/이유 heading), never a chunk_id — ids are
   positional and a sectioning change re-points them. If the correct behaviour is
   "return NOTHING", it is a `negative` case.

2. **Confirm it fails.** `make eval`. The new case should miss under the current
   retriever — that is the reproduction. If it already passes, there was no bug;
   keep the eval as coverage and stop.

3. **Diagnose BEFORE touching aliases.** Read the passages the query actually
   returned.
   - Wrong regime, a distinct penalty, or a bare-pointer co-occurrence → this is a
     NON-equivalence: record it in `excluded.tsv` with the reason (follow the three
     worked rejections already there) and stop. No alias.
   - The corpus genuinely names one referent with two surfaces that share no
     characters (character bigrams already bridge the rest) → a real synonym gap.

4. **Only then, a cited alias.** Add ONE `aliases.tsv` row (`group  surface
   provenance`), where provenance names the article that ASSERTS the equivalence
   (e.g. `06/제2조(정의)`). No provenance article, no row.

5. **Measure; keep only if it wins.** `make eval` again. Keep the alias only if the
   target `kind` improves with ZERO per-kind regression — the per-kind floor in
   `pipeline/eval_retrieval.py` now enforces this, so a row that fixes one kind and
   quietly breaks another fails the gate. If it does not win, move it to
   `excluded.tsv` with the measurement.

6. **Re-record the floor when the corpus changed.** Adding documents shifts every
   ranking, so `compare()` skips on a chunk_count mismatch. Re-record with
   `make eval-baseline` so the new numbers — and the git SHA now carried in the
   baseline — become the floor.

## Constraints

Never anchor an eval judgment to a chunk_id (positional). Never add an alias
without a provenance article and a measured win. Never create a corrections
backlog file — the eval / alias / exclusion trio is the durable, tracked record.
This is an operator step; nothing here runs inside `make build`, which stays
model-free.
