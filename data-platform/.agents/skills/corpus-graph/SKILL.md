---
name: corpus-graph
description: Trace how documents in the corpus relate through the local dependency graph — what a bill delegates to a lower decree (위임), what one document references or is referenced by (인용·참조), and which statute a bill amends (개정) where present. Use for reachability questions across bills and documents ("이 법안이 어떤 법에 위임하나", "무엇이 이 문서에 의존하나"), not content questions. For the blast radius of a CODE change specifically, use code-impact-analysis instead.
allowed-tools: Bash(uv run python -m agent.tools.graph_query *), Bash(make impact *)
compatibility: data-platform 색인(data/serving/index.sqlite)이 있어야 하며 uv 로 실행한다. 색인이 없으면 make fetch-index(발행된 색인 설치, 약 92MB·1분) 또는 make build(직접 인코딩, 약 32분)가 선행되어야 한다.
---

# Corpus graph

Reachability is a graph question, and search cannot answer it: a bill and the
statute it delegates to need not share any words, so no keyword or vector hit
reveals the link. The graph is built from bill relation markers and document
declarations, and is traversed by a recursive CTE. In the current bill corpus the
live edges are dominated by `delegates_to` (위임 — a provision hands a detail to a
lower decree); `amends` (개정) is a defined relation that materialises only for a
bill that carries amendment text, so it may be absent until such a bill is
indexed. Other edge types present: `references`, `mentions`, `defines`,
`depends_on`.

## Run it

```
uv run python -m agent.tools.graph_query 05_디지털자산의시장및산업에관한법률안_박상혁-hwp --direction downstream --max-depth 2
```

`make impact NODE=<node>` is the same walk upstream. Flags: `--direction`
(`upstream` = what depends on / points at this, `downstream` = what this points
at, default upstream), `--max-depth` (hop limit, default 3), `--relations`
(restrict edge types, e.g. `amends delegates_to`), `--limit` (default 50).

Two adjacent tools, in case the question is not pure reachability: when you
want the connected documents' *passages* rather than their names — "관련
법안들의 해당 조문까지 보여줘" — use `make ask` (the corpus-search skill),
whose `related` section walks the same edges and returns text with the edge as
provenance. To *see* the graph, the graph-viz skill renders it in a browser
(`make graph-serve`).

## Procedure

1. **Find the node.** Node ids are slugs of the file name; a bill keeps its
   format suffix (the trailing `-hwp`, as in `05_디지털자산의시장및산업에관한법률안_박상혁-hwp`).
   If the id is not exact, `graph_query` resolves the closest candidates and
   reports its pick in `start_node` and the alternatives in `candidates` — check
   that field before trusting the walk.

2. **Pick the direction for the question.** "이 법안이 무엇에 위임·참조하나"
   is `downstream` from the bill. "이 법률에 위임하는 법안이 무엇이냐" is
   `upstream` from the statute node. `depth` on each result is the shortest hop
   count; depth 2+ is the transitive reach that search would miss.

3. **Narrow when the graph is noisy.** `--relations delegates_to references`
   drops weak `mentions` edges when a document that merely names a statute is
   crowding out the real delegation edges.

## Reporting

List reached nodes grouped by `depth`, and for each name the relation from the
result's `via_relations` field ("가상자산-이용자-보호-등에-관한-법률 (depth 1,
via delegates_to)"). Within a depth, results order by accumulated edge weight;
`delegates_to` is weighted 1.0 (the strongest legal edge) while a bare
`mentions` is 0.3, so a high-weight path is a legislative dependency and a
low-weight one may be a passing reference. If the walk returns
nothing, say the node has no known relations and note that the graph only knows
edges the indexed corpus declared — a relation stated only in prose, or in a bill
not yet indexed, is invisible to it. Treat the graph as a lower bound on the real
relationships, not the complete picture.
