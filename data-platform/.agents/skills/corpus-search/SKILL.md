---
name: corpus-search
description: Search this node's indexed corpus for what the documents actually say — Korean bill and statute text (법안·법률·조문), design docs, and code. Use to answer any question grounded in the local documents, to find the article or passage on a topic (예: 예치금 분리보관, 스테이블코인 발행자 자기자본, 시행일, 벌칙), to look up an exact identifier, or whenever grep is too literal to find a passage phrased in different words. Hybrid vector (sqlite-vec) + keyword (FTS5) retrieval fused with reciprocal rank fusion; `make ask` additionally returns passages from graph-connected documents (같은 법령에 위임하는 다른 법안의 관련 조문) with edge provenance.
allowed-tools: Bash(uv run python -m agent.tools.hybrid_search *), Bash(make query *), Bash(uv run python -m agent.tools.graph_rag *), Bash(make ask *)
---

# Corpus search

The answer to "what do the documents say about X" is in the local index, not in
memory. Retrieve first, then answer from what came back, and cite it. This is the
first move on any content question; drafting a document or tracing code impact
are separate skills that call this one as a step.

## Run it

```
uv run python -m agent.tools.hybrid_search "예치금을 분리보관해야 하나" --limit 5
```

`make query Q="<query>"` is the same thing for a quick look. Flags: `--limit` (hits,
default 5), `--candidates` (ranking depth before fusion, default 40; raise it
when a passage is mid-ranked by both halves rather than top-ranked by one),
`--full-content` (whole chunks instead of 400-char snippets — use it once you
have found the right chunk and need its full text). Smoke fixtures share the index
but are hidden from results by default; pass `--include-fixtures` only to inspect
them. The command prints JSON to stdout: `results[]`, each with `rel_path`,
`collection`, `heading`, `content`, and the ranking fields below, plus a
`provenance` block naming the index that answered (`index_signature`,
`embedding_provider`, `node_role`, and the chunk/node/edge counts) so you can tell
which build a result came from.

**Widen with graph context when connected documents may hold the answer.**

```
uv run python -m agent.tools.graph_rag "예치금을 분리보관해야 하나" --depth 1
```

`make ask Q="<query>"` is the same thing. Its `results` section is exactly the
search above — same rows, same order, never re-ranked — and a `related` section
adds passages from documents the dependency graph connects to the top hits: the
bill that delegates to the same statute (co-citation), the module that imports
what you found (direct edge). Each related row carries `connection.links` (the
actual edges: relation, shared node, verbatim citing evidence) and a `selection`
tier — `query_match` (found by your wording), `lexical_overlap` (found by the
connective vocabulary, e.g. the shared statute's name), `doc_head` (nothing
matched; the document's head is shown only because the graph insists it is
related — treat as a pointer, not an answer). `--depth 0` is plain search;
`--depth 2` follows direct document edges one hop further. Related discovery
crosses collections even when the direct search is scoped — that is what the
graph is for.

**Scope to a collection when the question is about one body of documents.** A
collection is a document's first inbox folder (`sto/…` → `sto`; a file at the
root → `_root`), so organising is just moving files between folders. Restrict a
search with `--collection sto` (or `make query Q="<query>" COLLECTION=sto`); list what
exists with `--list-collections` / `make collections`. Unscoped searches every
collection.

## Query in the document's own terms, and narrow

- **Two or three narrow queries beat one broad one.** Fusion ranks per query, so
  a specific query is what lifts a specific article above generic prose. Ask each
  facet separately rather than in one long sentence.
- **Korean is bridged three ways; know which one you are leaning on.** The
  keyword arm matches 가상자산 inside 가상자산이용자보호법 (character bigrams,
  plus Kiwi-extracted nouns when the index was built with `KIWI_MORPH=1`); the
  vector arm is a semantic model (bge-m3 by default via `.env`), so it can reach
  a synonym nobody listed; and `pipeline/aliases.tsv` adds the measured,
  citable equivalences (스테이블코인 ↔ 자산연동형) that must not depend on a
  model's opinion. A synonym hit that matters should still be verified in the
  statute's own vocabulary — if a term returns nothing, try the word the
  statute itself would use (발행인 not 발행사, 예치금 not 보증금), or widen
  through the graph with `make ask`, which reaches the parallel article in a
  connected bill regardless of wording.
- **Two domain overviews carry the entity grain and the wrong-answer modes.** For a
  question that spans a whole domain, skim `source/디지털자산관련법안원문/00_디지털자산_법안개요.md`
  (the 8 competing, un-enacted digital-asset bills — which bill coins which
  stablecoin term) or `source/토큰증권법안원문/00_토큰증권_입법경과_및_통합내용.md`
  (the passed STO 대안). Both cite `pipeline/aliases.tsv` / `excluded.tsv` rather
  than restating them. Background, not a required first step.

## Read the ranking fields before trusting a hit

`vector_rank` and `keyword_rank` are each null when only the other half matched.

- both set → matched on meaning **and** exact terms; trust these first.
- only `keyword_rank` → a literal string hit that may be off-topic.
- only `vector_rank` → topical but may not contain your exact words.

`rrf_score` is the fused rank, higher is better. Cite the `rel_path` + `heading`
pair for every non-obvious claim; if retrieval returned nothing for a claim, say
so rather than filling the gap from memory.

## Limits worth stating

The index only knows the indexed corpus. A vector search returns exactly `k`
rows however distant, so a plausible-looking hit on an out-of-corpus topic is
expected — check that its `rrf_score` and terms actually match before relying on
it. For reachability ("what depends on / relates to X"), use `corpus-graph`;
search cannot answer it, because the related items need not mention each other.
