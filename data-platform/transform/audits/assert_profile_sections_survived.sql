-- A document a profile claimed must produce headed sections.
--
-- Fires on a REGRESSION, not on coverage: a profile that claims a document and
-- then yields a single unheaded chunk has silently replaced working generic
-- chunking with nothing. That is the EVAPORATE failure shape -- a parser that
-- works on the documents it saw and returns empty on the tail -- and it is
-- invisible downstream because the chunk count stays plausible.
--
-- Blocking. A profile that trips this must be fixed; the audit must not be.
--
-- The doc_type list below must cover every ROUTABLE_SUFFIXES entry in
-- pipeline/chunking.py. Routable is BINARY_SUFFIXES plus .txt, so ADDING A
-- BINARY FORMAT SILENTLY WIDENS what profiles may claim -- and a format missing
-- from this list is claimable but unaudited, which is the one combination that
-- lets the regression above ship.
AUDIT (
  name assert_profile_sections_survived,
  blocking true
);

SELECT
  doc_id,
  COUNT(*) AS chunk_count
FROM @this_model
WHERE doc_type IN (
  'hwp', 'hwpx', 'txt',
  'docx', 'xlsx', 'pptx',
  'doc', 'xls', 'ppt',
  'pdf'
)
GROUP BY doc_id
HAVING COUNT(*) = 1 AND MAX(COALESCE(NULLIF(TRIM(heading), ''), '')) = ''
