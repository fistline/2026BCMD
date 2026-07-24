MODEL (
  name bronze.chunks,
  kind VIEW,
  description 'Singer landing for the chunks stream, shape preserved.',
  grain chunk_id,
  audits (
    not_null(columns := (chunk_id, doc_id))
  )
);

SELECT
  CAST(chunk_id AS TEXT) AS chunk_id,
  CAST(doc_id AS TEXT) AS doc_id,
  CAST(rel_path AS TEXT) AS rel_path,
  CAST(title AS TEXT) AS title,
  CAST(doc_type AS TEXT) AS doc_type,
  CAST(chunk_index AS BIGINT) AS chunk_index,
  CAST(heading AS TEXT) AS heading,
  CAST(content AS TEXT) AS content,
  CAST(char_start AS BIGINT) AS char_start,
  CAST(char_end AS BIGINT) AS char_end,
  CAST(token_estimate AS BIGINT) AS token_estimate,
  CAST(content_sha256 AS TEXT) AS content_sha256,
  CAST(ingested_at AS TIMESTAMP) AS ingested_at
FROM lake.raw.chunks
