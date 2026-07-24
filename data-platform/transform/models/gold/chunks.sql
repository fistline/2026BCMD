MODEL (
  name gold.chunks,
  kind FULL,
  description 'Retrieval units consumed verbatim by pipeline/build_rag.py. embed_text prepends the document title and heading so the embedding carries the context a bare chunk body loses.',
  grain chunk_id,
  audits (
    not_null(columns := (chunk_id, doc_id, content, embed_text)),
    unique_values(columns := (chunk_id)),
    assert_text_not_blank(column := embed_text)
  )
);

SELECT
  c.chunk_id,
  c.doc_id,
  c.rel_path,
  c.title,
  c.doc_type,
  c.heading,
  c.chunk_index,
  c.content,
  -- Every argument must be non-null: CONCAT_WS is normalised to strict-null
  -- semantics, so a single NULL branch would null the whole embedding text.
  CONCAT_WS(
    CHR(10),
    CONCAT('# ', COALESCE(c.title, c.rel_path)),
    CASE WHEN c.heading <> c.title THEN CONCAT('## ', c.heading) ELSE '' END,
    c.content
  ) AS embed_text,
  c.char_start,
  c.char_end,
  c.token_estimate,
  c.content_sha256,
  c.ingested_at
FROM silver.chunks AS c
