-- A document a profile claimed must produce headed sections.
--
-- Fires on a REGRESSION, not on coverage: a profile that claims a document and
-- then yields a single unheaded chunk has silently replaced working generic
-- chunking with nothing. That is the EVAPORATE failure shape -- a parser that
-- works on the documents it saw and returns empty on the tail -- and it is
-- invisible downstream because the chunk count stays plausible.
--
-- Blocking. A profile that trips this must be fixed; the audit must not be.
AUDIT (
  name assert_profile_sections_survived,
  blocking true
);

SELECT
  doc_id,
  COUNT(*) AS chunk_count
FROM @this_model
WHERE doc_type IN ('hwp', 'hwpx', 'txt')
GROUP BY doc_id
HAVING COUNT(*) = 1 AND MAX(COALESCE(NULLIF(TRIM(heading), ''), '')) = ''
