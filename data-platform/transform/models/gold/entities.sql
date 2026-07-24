MODEL (
  name gold.entities,
  kind FULL,
  description 'Graph nodes for pipeline/build_graph.py: every document plus every entity that appears on either end of an edge. Building nodes from the union of both edge endpoints is what guarantees no edge can dangle.',
  grain entity_id,
  audits (
    not_null(columns := (entity_id, kind)),
    unique_values(columns := (entity_id))
  )
);

WITH document_nodes AS (
  SELECT
    d.doc_id AS entity_id,
    'document' AS kind,
    COALESCE(d.title, d.rel_path) AS label,
    d.doc_id AS doc_id,
    d.rel_path AS rel_path,
    2 AS priority
  FROM silver.documents AS d
), edge_nodes AS (
  SELECT source_entity AS entity_id, source_kind AS kind, doc_id, rel_path
  FROM silver.relations
  UNION ALL
  SELECT target_entity AS entity_id, target_kind AS kind, doc_id, rel_path
  FROM silver.relations
), referenced_nodes AS (
  SELECT
    e.entity_id,
    e.kind,
    e.entity_id AS label,
    MIN(e.doc_id) AS doc_id,
    MIN(e.rel_path) AS rel_path,
    1 AS priority
  FROM edge_nodes AS e
  WHERE e.entity_id IS NOT NULL AND LENGTH(TRIM(e.entity_id)) > 0
  GROUP BY e.entity_id, e.kind
), unioned AS (
  SELECT entity_id, kind, label, doc_id, rel_path, priority FROM document_nodes
  UNION ALL
  SELECT entity_id, kind, label, doc_id, rel_path, priority FROM referenced_nodes
), ranked AS (
  SELECT
    u.*,
    ROW_NUMBER() OVER (
      PARTITION BY u.entity_id
      ORDER BY u.priority DESC, u.kind
    ) AS node_rank
  FROM unioned AS u
)
SELECT
  r.entity_id,
  r.kind,
  r.label,
  r.doc_id,
  r.rel_path,
  COALESCE(o.out_degree, 0) AS out_degree,
  COALESCE(i.in_degree, 0) AS in_degree
FROM ranked AS r
LEFT JOIN (
  SELECT source_entity, COUNT(*) AS out_degree
  FROM silver.relations
  GROUP BY source_entity
) AS o
  ON o.source_entity = r.entity_id
LEFT JOIN (
  SELECT target_entity, COUNT(*) AS in_degree
  FROM silver.relations
  GROUP BY target_entity
) AS i
  ON i.target_entity = r.entity_id
WHERE r.node_rank = 1
