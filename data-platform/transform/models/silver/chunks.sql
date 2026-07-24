MODEL (
  name silver.chunks,
  kind FULL,
  description 'Chunks belonging to a current document, de-duplicated and blank-stripped. The inner join to silver.documents is what guarantees the serving index can never contain a chunk of a document that no longer exists in data/raw.',
  grain chunk_id,
  audits (
    not_null(columns := (chunk_id, doc_id, content)),
    unique_values(columns := (chunk_id)),
    assert_text_not_blank(column := content),
    assert_profile_sections_survived,
    assert_every_document_chunked
  )
);

WITH latest_batch AS (
  SELECT MAX(ingested_at) AS batch_at
  FROM bronze.chunks
), current_rows AS (
  SELECT
    c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.chunk_id
      ORDER BY c.ingested_at DESC
    ) AS revision_rank
  FROM bronze.chunks AS c
  CROSS JOIN latest_batch AS b
  WHERE c.ingested_at = b.batch_at
)
SELECT
  c.chunk_id,
  c.doc_id,
  d.rel_path,
  d.title,
  d.doc_type,
  c.chunk_index,
  COALESCE(NULLIF(TRIM(c.heading), ''), d.title) AS heading,
  TRIM(c.content) AS content,
  c.char_start,
  c.char_end,
  c.token_estimate,
  c.content_sha256,
  c.ingested_at
FROM current_rows AS c
INNER JOIN silver.documents AS d
  ON d.doc_id = c.doc_id
WHERE c.revision_rank = 1
  AND c.content IS NOT NULL
  AND LENGTH(TRIM(c.content)) > 0
