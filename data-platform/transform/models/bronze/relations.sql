MODEL (
  name bronze.relations,
  kind VIEW,
  description 'Singer landing for the relations stream, shape preserved.',
  grain relation_id,
  audits (
    not_null(columns := (relation_id, source_entity, target_entity, relation))
  )
);

SELECT
  CAST(relation_id AS TEXT) AS relation_id,
  CAST(doc_id AS TEXT) AS doc_id,
  CAST(rel_path AS TEXT) AS rel_path,
  CAST(source_entity AS TEXT) AS source_entity,
  CAST(source_kind AS TEXT) AS source_kind,
  CAST(relation AS TEXT) AS relation,
  CAST(target_entity AS TEXT) AS target_entity,
  CAST(target_kind AS TEXT) AS target_kind,
  CAST(evidence AS TEXT) AS evidence,
  CAST(ingested_at AS TIMESTAMP) AS ingested_at
FROM lake.raw.relations
