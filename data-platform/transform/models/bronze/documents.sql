MODEL (
  name bronze.documents,
  kind VIEW,
  description 'Singer landing for the documents stream, shape preserved. Types are made explicit so downstream layers do not inherit DuckDB HUGEINT from the loader.',
  grain doc_id,
  audits (
    not_null(columns := (doc_id, rel_path, content_sha256))
  )
);

SELECT
  CAST(doc_id AS TEXT) AS doc_id,
  CAST(rel_path AS TEXT) AS rel_path,
  CAST(doc_type AS TEXT) AS doc_type,
  CAST(title AS TEXT) AS title,
  CAST(content AS TEXT) AS content,
  CAST(content_sha256 AS TEXT) AS content_sha256,
  CAST(content_fingerprint AS TEXT) AS content_fingerprint,
  CAST(byte_size AS BIGINT) AS byte_size,
  CAST(source_modified_at AS TIMESTAMP) AS source_modified_at,
  CAST(ingested_at AS TIMESTAMP) AS ingested_at
FROM lake.raw.documents
