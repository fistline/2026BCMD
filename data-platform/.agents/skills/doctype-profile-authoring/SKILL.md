---
name: doctype-profile-authoring
description: Teach the build how to parse a document type it does not know, by compiling a committed regex profile for it (판결문, 계약서, 의안, 법령, 심사보고서). Use when asked to analyse or split a folder of Korean legal documents by structure, to split 판결문 on 주문/이유, to parse 제N조 clause structure, or to pull party and clause nodes and edges out of contracts or judgments. Also use when an indexed document came back as one unstructured chunk, or when searching for 부칙 or a 조문 heading returns nothing. Step 0 decides between a throwaway script and a committed profile, so start here for one-off requests too. The profile is written once, reviewed, and committed; the build never calls a model.
---

# Authoring a document-type profile

Per-type structure does not generalise: 판결문 splits on 주문/이유, 의안 and 계약서 on
제N조, and new types keep arriving. But a model that writes the parser at build
time makes the build unreproducible and leaves the blocking audits gating
something that changes every run. So the model compiles the tables once, and
every build after that runs plain Python.

## Answer the request first

Write and run the script the user asked for in your own scratchpad, outside the
repo, and show the output. Nothing under `data/` and nothing in `pipeline/`
changes for a one-off. Only continue past this section when the type will
recur — an ongoing feed, or roughly five or more documents. A profile is code
someone maintains forever.

Only `.md .markdown .txt .rst .py .hwp .hwpx` reach the pipeline. There is no
PDF path, and `pipeline/extract.py` rejects `\x0c`, which every `pdftotext` page
break contains — so converting a PDF and adding `.pdf` to the suffix set fails
twice over. Convert to `.txt` into `data/inbox/documents/` and let the watcher
record the promotion.

## Procedure

1. **Measure the gap.** `uv run python -m pipeline.doctypes.gate --report` prints
   which profile claims each document in `data/raw/documents`, how many sections
   it produced against the generic baseline, and the line-initial markers in
   documents nothing claims. Do not write a profile until this names the
   documents you are fixing. Keep the output; step 6 diffs against it.

2. **Read `pipeline/doctypes/bill.py`.** It is the template and it is in the
   build, so it cannot drift. Copy it. A second module shape is worse than a
   suboptimal marker regex.

3. **Fill the four tables** — `REQUIRE` (all must match), `REJECT` (none may
   match), `MARKERS`, `EDGES` — and add one line to `PROFILE_MODULES`. Write no
   executable statements: the gate rejects anything at top level that is not
   `import re` or a tuple assignment.

4. **Write `REJECT` before `REQUIRE`.** 계약서 and 법령 share `제N조(제목)`
   exactly, so only the negatives discriminate. Put `\s*` between every syllable
   (`주\s*문`) — official Korean documents letter-space their labels — and `\r` in
   every line class, or the marker silently returns nothing on CRLF input.
   `references/korean-doctype-markers.md` has the traps per type.

5. **Commit a golden pair** at `pipeline/fixtures/doctypes/<type>.sample.txt`
   and `.golden.json`. Synthesise the sample: no real case number, party name or
   personal data, 8 KB cap, cut at a section boundary. It must stay in that
   subdirectory — a `.txt` at the top of `pipeline/fixtures/` is seeded into the
   inbox and promoted into the immutable raw zone before any gate runs.

6. **Run the gate, then re-run the report.** `uv run python -m
   pipeline.doctypes.gate`, then `make build && make verify`. A new profile that
   takes documents an existing one was handling is a regression, not a feature.
   If an audit fails, fix the profile — never the audit.

## Constraints

A profile is a pure function of bytes: no I/O, no clock, no randomness, no set
iteration. Keep every marker anchored and bounded — a nested quantifier hangs
`make build` with no error and no traceback while the purity check reports clean.

A profile never builds a `Chunk`, never assigns a `chunk_id`, and never names a
clause or party itself: the engine prefixes those with `doc_id`. A bare `갑` does
not merely merge nodes, it deletes another document's edge, because silver
dedups on `(source, relation, target)` with no `doc_id`.

조사 stripping and entity merging belong in `normalise_entity`, which is pinned
to agree with `document_id`. Doing either inside a profile fragments the graph
into ids no node carries.

## Reporting

Give the routing result for each new fixture, marker recall against the generic
baseline, the largest-section ratio, and the unclaimed count before and after.
Then stop. This procedure produces a diff for a human to merge.
