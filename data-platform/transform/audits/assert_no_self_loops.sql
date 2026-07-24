-- Generic, parameterised audit: an edge may not point at its own source.
--
-- A self-loop makes the recursive CTE in pipeline/build_graph.py revisit a node
-- forever unless the cycle guard catches it first. Rejecting them at the quality
-- gate is cheaper than defending against them at every query.
AUDIT (
  name assert_no_self_loops,
  blocking true
);

SELECT
  *
FROM @this_model
WHERE
  @source_column = @target_column
