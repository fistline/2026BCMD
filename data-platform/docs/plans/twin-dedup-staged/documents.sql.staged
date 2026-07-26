MODEL (
  name silver.documents,
  kind FULL,
  description 'Current documents: latest sync batch only, one row per doc_id, then one served rendition per content fingerprint. Filtering to the newest ingested_at makes a deletion in data/raw propagate instead of leaving a ghost row; the fingerprint stage collapses the same content arriving in two formats (e.g. .hwp and .pdf) so it is not double-indexed downstream. Renditions the EXACT fingerprint misses (an .hwp and .pdf of one bill linearise differently) are collapsed by the fuzzy silver.document_twins layer, which marks the lower-priority rendition superseded so it is not served.',
  grain doc_id,
  audits (
    not_null(columns := (doc_id, rel_path, title)),
    unique_values(columns := (doc_id))
  )
);

WITH latest_batch AS (
  SELECT MAX(ingested_at) AS batch_at
  FROM bronze.documents
), current_batch AS (
  SELECT d.*
  FROM bronze.documents AS d
  CROSS JOIN latest_batch AS b
  WHERE d.ingested_at = b.batch_at
), per_doc AS (
  -- Stage 1: collapse revisions and same-basename id collisions to one row per
  -- doc_id, newest first. This is the original behaviour and is what keeps
  -- doc_id unique; content_sha256 is a deterministic tiebreak.
  SELECT
    c.*,
    ROW_NUMBER() OVER (
      PARTITION BY c.doc_id
      ORDER BY c.ingested_at DESC, c.content_sha256
    ) AS revision_rank
  FROM current_batch AS c
), one_per_doc AS (
  SELECT * FROM per_doc WHERE revision_rank = 1
), renditions AS (
  -- Stage 2: collapse multiple FORMATS of the same content (a bill as both .hwp
  -- and .pdf) to a single served rendition. Documents shorter than the floor stay
  -- keyed by doc_id, so two short unrelated notes can never merge on a
  -- coincidental fingerprint. Format priority keeps the AUTHORITATIVE rendition:
  -- an editable original outranks a derived one, and a modern container outranks
  -- the legacy binary it was converted from, so the ladder runs
  -- hwp > hwpx > docx > doc > pptx > ppt > xlsx > xls > pdf > everything else.
  -- PDF sits last because it is what the others are exported TO. Leaving a new
  -- format in the ELSE bucket would rank it BELOW pdf and let a derived PDF win
  -- over its own source. content_sha256 alone is NOT a total order: two
  -- byte-identical twins of one filing dropped in different folders share it (and
  -- the same batch and doc_type), so doc_id is appended as the final total-order
  -- key to keep the surviving rendition -- and its collection/doc_id/chunk_ids --
  -- stable across rebuilds (idempotence).
  -- The 200-char floor must match FINGERPRINT_MIN_CHARS in pipeline/chunking.py.
  SELECT
    o.*,
    ROW_NUMBER() OVER (
      PARTITION BY CASE
          WHEN LENGTH(TRIM(o.content)) >= 200 THEN 'fp:' || o.content_fingerprint
          ELSE 'id:' || o.doc_id
        END
      ORDER BY
        CASE o.doc_type
          WHEN 'hwp'  THEN 9 WHEN 'hwpx' THEN 8
          WHEN 'docx' THEN 7 WHEN 'doc'  THEN 6
          WHEN 'pptx' THEN 5 WHEN 'ppt'  THEN 4
          WHEN 'xlsx' THEN 3 WHEN 'xls'  THEN 2
          WHEN 'pdf'  THEN 1 ELSE 0
        END DESC,
        o.ingested_at DESC,
        o.content_sha256,
        o.doc_id
    ) AS rendition_rank
  FROM one_per_doc AS o
)
-- Exact-fingerprint survivors, minus the fuzzy-superseded renditions. The
-- LEFT JOIN keeps a served doc when it has no twin (tw.doc_id IS NULL); a bill's
-- .pdf whose .hwp twin outranks it drops out here so it is not double-indexed,
-- while its bytes remain in bronze/raw and the pair is recorded in
-- silver.document_twins (auditable, reversible). No dependency cycle:
-- document_twins reads bronze.documents, never silver.documents.
SELECT
  renditions.doc_id,
  rel_path,
  doc_type,
  NULLIF(TRIM(title), '') AS title,
  content,
  content_sha256,
  content_fingerprint,
  byte_size,
  source_modified_at,
  ingested_at
FROM renditions
LEFT JOIN silver.document_twins AS tw ON tw.doc_id = renditions.doc_id
WHERE rendition_rank = 1
  AND content IS NOT NULL
  AND LENGTH(TRIM(content)) > 0
  AND tw.doc_id IS NULL
