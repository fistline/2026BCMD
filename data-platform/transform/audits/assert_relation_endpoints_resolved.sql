-- Referential gate between the two gold tables the graph is built from.
--
-- build_graph.py loads gold.entities into `nodes` and gold.relations into
-- `edges`. If an edge endpoint has no matching node, multi-hop traversal walks
-- off the end of the graph and impact analysis silently under-reports.
--
-- The audited model resolves both endpoints against gold.entities with a LEFT
-- JOIN, which makes gold.entities a real edge in the SQLMesh DAG rather than an
-- unmapped reference. This audit only has to assert that neither side missed.
-- Blocking: an unresolved endpoint must stop the build, not warn about it.
AUDIT (
  name assert_relation_endpoints_resolved,
  blocking true
);

SELECT
  *
FROM @this_model
WHERE
  @source_node_column IS NULL
  OR @target_node_column IS NULL
