---
name: document-drafting
description: Draft or revise a document grounded in this node's indexed corpus. Use when asked to write, extend, summarise, or reconcile a design doc, spec, README, or note, and the answer should come from the local documents rather than from memory.
---

# Document drafting

Never draft from memory. Everything in this corpus is local and cheap to query,
so an unsourced claim is a choice, not a constraint.

## Procedure

1. **Retrieve before writing.** Run `hybrid_search` on the document's topic,
   then again on each section heading you intend to write. Two or three narrow
   queries beat one broad one: fusion ranks per query, so a narrow query is what
   pulls a specific passage above generic prose.

   ```
   uv run python -m agent.tools.hybrid_search "inbox promotion rules" --limit 5
   ```

   When the topic spans connected documents (여러 법안이 같은 개념을 각자
   규정하는 경우), run `agent.tools.graph_rag` instead: its `related` section
   returns the parallel passages of graph-connected documents with the edge as
   provenance, which is exactly what a cross-document draft must cite.

2. **Check what the topic touches.** Run `graph_query` downstream from the
   subject to find the documents it depends on, so the draft does not contradict
   or silently duplicate one of them.

   ```
   uv run python -m agent.tools.graph_query ingestion-pipeline --direction downstream
   ```

3. **Write, citing `rel_path` for every non-obvious claim.** Each result carries
   `rel_path` and `heading`; cite that pair. If retrieval returned nothing for a
   claim, say so in the draft rather than filling the gap from memory.

4. **Reconcile every citation before finishing.** Walk your citations one at a
   time — not one broad re-query. For each `rel_path` + `heading` you cited,
   re-run `hybrid_search` on the sentence it supports and judge the hit by the
   ranking rules in `corpus-search` ("Read the ranking fields before trusting a
   hit" — do not restate them here):

   - Is your citation still the hit to trust first (both `vector_rank` and
     `keyword_rank` set), or has a keyword-only / vector-only hit displaced it?
   - Does any higher-`rrf_score` hit contradict the sentence? Resolve it, or name
     the disagreement in the draft explicitly.
   - Did retrieval return nothing for the sentence? Then it is unsourced — say so
     rather than keeping it.

   This is a same-session self-check: you are re-reading a corpus you just wrote
   against. For a genuinely adversarial pass, run the finished draft through a
   FRESH `corpus-search` session with no drafting context, so the reviewer is not
   primed by the sentences it is checking.

## When the corpus does not cover it

Say what is missing. If external material is genuinely needed, use the Scrapling
MCP hook (`agent/tools/scrapling_mcp.py`), prefer an official API, respect
robots.txt and site terms, and drop what you retrieve into
`data/inbox/documents/` so it is indexed with normal provenance before you cite
it. Do not cite a page you fetched but did not index.

## Constraints

Write only into the control plane or a path the user named. Never edit
`data/raw/` — it is immutable, and edits there break the guarantee that
`data/processed/` can be rebuilt from code plus raw.
