---
name: code-impact-analysis
description: Trace the blast radius of a change using the local dependency graph. Use when asked what breaks if something changes, what depends on a module or symbol, why something is still referenced, or whether a change is safe to make.
---

# Code impact analysis

Impact is transitive, so it is a graph question. Searching the text of the thing
you are changing cannot answer it: the modules that break are precisely the ones
that never mention it.

## Procedure

1. **Find the node.** Node ids are slugs: `search_core.py` is `search_core`,
   `chunking-rules.md` is `chunking-rules`. `graph_query` resolves near-misses
   and reports what it picked in `start_node`, so check that field before
   trusting the walk.

2. **Walk upstream for blast radius.**

   ```
   uv run python -m agent.tools.graph_query vector_store --direction upstream --max-depth 3
   ```

   `depth` is the shortest hop count from the thing you are changing. Depth 1 is
   a direct caller; depth 2+ is where regressions hide, because nothing at that
   distance names the symbol you touched.

3. **Walk downstream for prerequisites.** `--direction downstream` answers what
   the module itself relies on, which is what you need before deleting or
   inlining something.

4. **Narrow when the graph is noisy.** `--relations imports depends_on` drops
   weak `mentions` and `references` edges. Use it when a doc that merely names a
   symbol is crowding out real code dependencies.

5. **Read the code before concluding.** The graph tells you *where* to look;
   it does not tell you whether the change actually breaks that call site. Feed
   each affected `rel_path` to `hybrid_search` (or open the file) and confirm.

   ```
   uv run python -m agent.tools.hybrid_search "knn_lookup" --limit 5
   ```

## Reporting

List affected nodes grouped by depth, and for each one state what specifically
would break. "service_api is affected (depth 2, via imports)" is a finding;
"several modules may be affected" is not. If the walk returns nothing, say the
node has no known dependents and note that the graph only knows about edges the
indexed corpus declares.

## Limits worth stating

The graph is built from Python imports and from document front matter, so a
dynamic import, a plugin registry, or a dependency expressed only in prose is
invisible to it. When a change looks high-risk, say that the graph is a lower
bound on impact rather than the complete picture.
