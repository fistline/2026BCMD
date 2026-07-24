MODEL (
  name gold.relations,
  kind FULL,
  description 'Graph edges consumed verbatim by pipeline/build_graph.py. Both endpoints are resolved against gold.entities here, so the referential guarantee the graph depends on is enforced in the DAG rather than assumed at query time. Weight encodes how load-bearing an edge is, so multi-hop traversal can rank paths instead of returning every reachable node flat.',
  grain (source_entity, relation, target_entity),
  audits (
    not_null(columns := (relation_id, source_entity, relation, target_entity, weight)),
    assert_no_self_loops(source_column := source_entity, target_column := target_entity),
    assert_relation_endpoints_resolved(
      source_node_column := source_node_id,
      target_node_column := target_node_id
    )
  )
);

SELECT
  r.relation_id,
  r.source_entity,
  r.source_kind,
  r.relation,
  r.target_entity,
  r.target_kind,
  CASE r.relation
    WHEN 'depends_on' THEN 1.0
    WHEN 'imports' THEN 1.0
    -- 위임 (delegates_to): a decree empowered by its enabling law -- an authority
    -- dependency = 1.0 (above references). Reserved for a 법령 profile that extracts
    -- 시행령->모법 위임; the bill profile's "「법」에 따른" citations are references,
    -- not 위임 (legal-schema-authoring). Kept in the CASE so real 위임 ranks right.
    WHEN 'delegates_to' THEN 1.0
    WHEN 'uses' THEN 0.8
    WHEN 'defines' THEN 0.8
    WHEN 'supersedes' THEN 0.6
    WHEN 'references' THEN 0.5
    WHEN 'related_to' THEN 0.4
    ELSE 0.3
  END AS weight,
  source_node.entity_id AS source_node_id,
  target_node.entity_id AS target_node_id,
  r.doc_id,
  r.rel_path,
  r.evidence,
  r.ingested_at
FROM silver.relations AS r
LEFT JOIN gold.entities AS source_node
  ON source_node.entity_id = r.source_entity
LEFT JOIN gold.entities AS target_node
  ON target_node.entity_id = r.target_entity
