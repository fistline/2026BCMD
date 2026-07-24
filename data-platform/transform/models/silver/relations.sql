MODEL (
  name silver.relations,
  kind FULL,
  description 'Directed edges asserted by current documents, one row per (source, relation, target). Self-loops are dropped so recursive traversal in graph_query cannot spin.',
  grain relation_id,
  audits (
    not_null(columns := (relation_id, source_entity, relation, target_entity)),
    unique_values(columns := (relation_id)),
    assert_no_self_loops(source_column := source_entity, target_column := target_entity)
  )
);

WITH latest_batch AS (
  SELECT MAX(ingested_at) AS batch_at
  FROM bronze.relations
), current_rows AS (
  SELECT
    r.*,
    ROW_NUMBER() OVER (
      PARTITION BY LOWER(TRIM(r.source_entity)), r.relation, LOWER(TRIM(r.target_entity))
      ORDER BY r.ingested_at DESC, r.relation_id
    ) AS dedup_rank
  FROM bronze.relations AS r
  CROSS JOIN latest_batch AS b
  WHERE r.ingested_at = b.batch_at
)
SELECT
  r.relation_id,
  r.doc_id,
  r.rel_path,
  LOWER(TRIM(r.source_entity)) AS source_entity,
  COALESCE(NULLIF(TRIM(r.source_kind), ''), 'unknown') AS source_kind,
  LOWER(TRIM(r.relation)) AS relation,
  LOWER(TRIM(r.target_entity)) AS target_entity,
  COALESCE(NULLIF(TRIM(r.target_kind), ''), 'unknown') AS target_kind,
  r.evidence,
  r.ingested_at
FROM current_rows AS r
INNER JOIN silver.documents AS d
  ON d.doc_id = r.doc_id
WHERE r.dedup_rank = 1
  AND LENGTH(TRIM(r.source_entity)) > 0
  AND LENGTH(TRIM(r.target_entity)) > 0
  AND LOWER(TRIM(r.source_entity)) <> LOWER(TRIM(r.target_entity))
