MODEL (
  name silver.document_twins,
  kind FULL,
  description 'Fuzzy cross-format twins that the exact content_fingerprint misses. A bill dropped as both .hwp and .pdf gets DIFFERENT fingerprints (hwpkit vs pypdf linearise the 의안번호 cover table differently -- measured 0/10 exact match), so silver.documents Stage-2 never collapses them and both double-index. This model detects them by character w-shingle Jaccard and marks the lower-priority rendition superseded_by the higher-priority one. NON-DESTRUCTIVE at the lake: the loser stays in bronze/raw and is recorded here (auditable/reversible); silver.documents merely stops SERVING it. Reads bronze (not silver.documents) to avoid a dependency cycle.',
  grain doc_id,
  audits (
    unique_values(columns := (doc_id))
  )
);

-- Same latest-batch + one-row-per-doc collapse silver.documents Stage-1 does, so
-- the twin scan sees exactly the served revision set.
WITH latest_batch AS (
  SELECT MAX(ingested_at) AS batch_at FROM bronze.documents
),
current_batch AS (
  SELECT d.* FROM bronze.documents AS d
  CROSS JOIN latest_batch AS b
  WHERE d.ingested_at = b.batch_at
),
one_per_doc AS (
  SELECT * FROM (
    SELECT c.*,
      ROW_NUMBER() OVER (PARTITION BY c.doc_id ORDER BY c.ingested_at DESC, c.content_sha256) AS rr
    FROM current_batch AS c
  ) WHERE rr = 1
),
prepared AS (
  -- suffix = the true rendition FORMAT (silver doc_type is also format-derived, so a
  -- "same doctype" gate is unusable here -- it would contradict format-different). The
  -- FORMAT_PRIORITY here MUST match transform/models/silver/documents.sql and
  -- chunking.FORMAT_PRIORITY. norm = the n2 fingerprint form: NFC then strip ALL
  -- whitespace, case preserved. It differs from normalize_for_fingerprint (which
  -- COLLAPSES whitespace) on purpose: char-shingling on the whitespace-free string is
  -- invariant to the two extractors' whitespace disagreement and to Korean's lack of
  -- word boundaries.
  SELECT
    doc_id,
    lower(regexp_extract(rel_path, '\.([^.]+)$', 1)) AS suffix,
    LENGTH(content) AS content_len,
    regexp_replace(nfc_normalize(content), '\s', '', 'g') AS norm,
    CASE doc_type
      WHEN 'hwp'  THEN 9 WHEN 'hwpx' THEN 8
      WHEN 'docx' THEN 7 WHEN 'doc'  THEN 6
      WHEN 'pptx' THEN 5 WHEN 'ppt'  THEN 4
      WHEN 'xlsx' THEN 3 WHEN 'xls'  THEN 2
      WHEN 'pdf'  THEN 1 ELSE 0
    END AS prio
  FROM one_per_doc
  WHERE LENGTH(TRIM(content)) >= 200          -- same floor as the fingerprint ladder
),
cand AS (
  -- Hard gates, evaluated WITHOUT touching the content (cheap prune):
  --   format-different  -- the load-bearing discriminator. A 정정/개정/different
  --                        instrument arrives in the SAME format, so it never enters
  --                        here; only cross-format renditions do.
  --   length-ratio<0.15 -- a true rendition is ~0.99 length-identical; this drops a
  --                        boilerplate-sharing pair of different length before the
  --                        (measured) 0.30 Jaccard margin even has to carry it.
  SELECT a.doc_id AS a_id, b.doc_id AS b_id, a.prio AS a_prio, b.prio AS b_prio
  FROM prepared a
  JOIN prepared b
    ON a.doc_id < b.doc_id
   AND a.suffix <> b.suffix
   AND ABS(a.content_len - b.content_len)::DOUBLE / GREATEST(a.content_len, b.content_len) < 0.15
),
parts AS (SELECT a_id AS d FROM cand UNION SELECT b_id AS d FROM cand),
shingle_sets AS (
  -- One DISTINCT shingle SET per candidate participant, materialised as a LIST.
  -- Shingle ONLY candidate participants, so the big single-format documents (a
  -- 333k-char 감독규정, a 249k-char 증권신고서) that have no similar-length
  -- cross-format partner are never listed. w=10 character shingles.
  SELECT p.doc_id,
         list_distinct(list_transform(range(1, LENGTH(p.norm) - 10 + 2),
                                       g -> SUBSTR(p.norm, g, 10))) AS shingles
  FROM prepared p
  WHERE p.doc_id IN (SELECT d FROM parts) AND LENGTH(p.norm) >= 10
),
scored AS (
  -- Exact set Jaccard computed PER CANDIDATE PAIR with list_intersect, NOT a
  -- shingle-level self-join. The self-join (dsh db ON db.shingle = da.shingle)
  -- fans out on shingles common to many legal documents (every 제N조 boilerplate
  -- shard) into a K^2 cartesian that OOM'd DuckDB at 80 GiB; list_intersect stays
  -- O(shingles) per pair. Still no MinHash, no hash() on the decision path ->
  -- deterministic across DuckDB versions and PYTHONHASHSEEDs. T=0.85: measured
  -- twin floor 0.910, worst cross-format different-doc 0.610 (margin +0.30).
  SELECT a_id, b_id, a_prio, b_prio, jaccard
  FROM (
    SELECT c.a_id, c.b_id, c.a_prio, c.b_prio,
           LEN(list_intersect(sa.shingles, sb.shingles))::DOUBLE
             / NULLIF(LEN(sa.shingles) + LEN(sb.shingles)
                      - LEN(list_intersect(sa.shingles, sb.shingles)), 0) AS jaccard
    FROM cand c
    JOIN shingle_sets sa ON sa.doc_id = c.a_id
    JOIN shingle_sets sb ON sb.doc_id = c.b_id
  )
  WHERE jaccard >= 0.85
),
pairs AS (
  -- Winner = higher FORMAT_PRIORITY (tiebreak lower doc_id for a total order). The
  -- loser is marked superseded_by the winner. Pairwise only -- no transitive
  -- union -- so a near-threshold 3-way chain can never merge two docs that did not
  -- themselves verify.
  SELECT
    CASE WHEN a_prio > b_prio OR (a_prio = b_prio AND a_id < b_id) THEN b_id ELSE a_id END AS doc_id,
    CASE WHEN a_prio > b_prio OR (a_prio = b_prio AND a_id < b_id) THEN a_id ELSE b_id END AS superseded_by,
    jaccard
  FROM scored
)
SELECT doc_id, superseded_by, jaccard
FROM (
  SELECT doc_id, superseded_by, jaccard,
    ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY jaccard DESC, superseded_by) AS rr
  FROM pairs
) WHERE rr = 1
ORDER BY doc_id     -- stable row order so the FULL model output is byte-identical across rebuilds
