---
name: retrieval-tuning
description: Change a retrieval knob or the chunking and prove the change was an improvement rather than a coincidence. Use when asked to tune, sweep, or optimise retrieval quality (SECTION_CAP, RERANK_WEIGHT, RRF_K, VECTOR_WEIGHT, ALIAS_EXPANSION, MAX_CHUNK_CHARS, the embedder), when a measured number needs to become the new default, or when re-recording an eval floor. Covers the order that keeps an expensive experiment cheap and an eval honest — pre-declared reading rules, sweep-edge and mechanism checks, snapshot-before-rebuild, and the citation the value must carry.
---

# Retrieval tuning

The failure this exists to prevent is not a bad number. It is a good number that
came from the wrong place: a sweep whose best value sat at the edge of the range,
a per-kind drop hidden by a mean, a floor recorded from an index that reused
cached vectors, or a default whose justification was measured on a corpus that no
longer exists. Every one of those happened in this repo, and each is now either
gated or listed below.

Read `AGENTS.md` "Measuring retrieval" first — it is the governing text and this
skill does not restate it.

## 1. Decide how you will read the result, before you run it

Write the rule down where the user can see it, then keep it. The one that has held
up here: **an offsetting result is a rejection, not a draw.** If one kind rises and
another falls, the change does not ship.

Also pick the threshold. `make verify` fails an ARM metric at 0.02 and a per-KIND
metric at 0.08; anything below that is not a result on 12 graded queries, where one
query is worth 0.083 overall and 0.333 within its kind.

## 2. Know which axis you are on, because the costs differ by four orders

| axis | knobs | cost per point |
|---|---|---|
| query-time | `SECTION_CAP` `RERANK_WEIGHT` `RRF_K` `VECTOR_WEIGHT` `ALIAS_EXPANSION` | ~7 s |
| build-time | `MAX_CHUNK_CHARS` `EMBEDDING_PRECISION` `ENCODE_BATCH` the embedder | 25–70 min |

Sweep the first freely. For the second, ask whether the question needs a build at
all — `make chunk-ceiling` answers "what ceiling truncates nothing" from the token
distribution, and that is one rebuild you do not run. It only looks DOWNWARD from
the current ceiling and refuses anything above it.

## 3. Snapshot before an expensive rebuild

```
make snapshot        # ~0.1 s, APFS clone, no extra disk
# change the constant, make build, measure
make restore         # ~0.1 s if the experiment loses
```

Without this an experiment costs a rebuild to run and another to undo. With it the
undo is free, which is often the difference between running the experiment and
guessing.

## 4. Sweep, then distrust the edge

If the best value is the largest or smallest you tried, **you have found the edge
of your range, not the optimum.** Extend and re-run. `RERANK_WEIGHT` peaked at the
edge (2.0) of its first sweep; extending it found the real peak at 3.0 and a
plateau to 4.0 [M:rerank-weight].

Then check the shape against the mechanism, which is the part no optimiser does:

- does the direction match what the formula predicts?
- does the extreme converge where theory says it must?

That second question is what made 3.0 trustworthy: at large weights the fusion
`w/(k + retr) + 1/(k + ce)` must collapse to the plain fused ordering, and the
measured curve did exactly that at 8.0. Without it, a peak on 12 queries is
indistinguishable from two queries moving.

## 5. Read per kind, never the mean

`make eval` prints both. A mean can stay bit-identical while a kind collapses —
AGENTS.md carries the case. Watch also for the `no held-out split` note: the
queries you tuned on are the queries the floor comes from, so a gain measured here
is an upper bound on a gain elsewhere.

## 6. Record the floor only from a canonical index

`make eval-baseline` refuses an incrementally-built one, and it is right to:
cached vectors were encoded beside different neighbours. Two things follow that
cost half an hour to learn:

- **`make verify` leaves the index incremental**, because it runs a build. Order is
  `make index-canonical` → record → `make verify`, never verify → record.
- both floors move together when the candidate POOL changes. `SECTION_CAP` and
  `RERANK_CANDIDATES` change what the cross-encoder sees, and that graph is
  dynamically quantised, so every CE score shifts [M:rerank-batch].

## 7. Make the value cite its measurement

A new default is not done until it carries a measurement id in `[M:<slug>]` form. `tools/check_citations.py`
enforces it in the pre-commit hook, CI and `make verify`: every tuning knob's
`.env.example` paragraph must name a measurement, every citation must resolve, and
every measurement must name its corpus (`make corpus-id`). Add the row with the
command that reproduces it — a row is only added by someone who ran it.

## 8. If the index changed, the published one is now stale

A knob that moves `index_signature` (the embedder, the tokenizer, the n-gram
widths) makes the release named by `index_release.json` un-installable on the tree
you just changed — `make fetch-index` will refuse it before downloading, with both
signatures printed. That refusal is correct, but leaving it there means every
consumer builds for 32 minutes instead of one.

So a landed change ends with `make publish-index YES=1` and a commit of
`index_release.json` — the pointer is the only checksum anyone trusts, and it is
worth nothing until it is committed. Publishing refuses anything but a canonical
index and re-runs all three floors first, so it also double-checks §6.

Chunk size is the case that catches people: `MAX_CHUNK_CHARS` does NOT move the
signature, so a stale release stays installable and simply answers from different
chunks than the tree expects. Republish on any rebuild that changes what is in the
index, not only on the ones the signature notices.

## What this skill will not do

Choose the knob for you, or judge whether a citation is the RIGHT one. And it does
not fix the real limit: 12 graded queries. Every conclusion above rests on them,
and widening the set — `correction-harvesting` — is worth more than any tuning this
skill can run.
