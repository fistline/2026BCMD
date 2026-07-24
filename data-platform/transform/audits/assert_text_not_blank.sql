-- Generic, parameterised audit: fail if the named column is null or whitespace.
--
-- Blank text is the failure mode that matters most here: FTS5 will happily index
-- an empty string and sqlite-vec will embed it, so a blank chunk becomes a row
-- that can be retrieved but says nothing. Blocking, so it stops the build.
AUDIT (
  name assert_text_not_blank,
  blocking true
);

SELECT
  *
FROM @this_model
WHERE
  @column IS NULL
  OR LENGTH(TRIM(CAST(@column AS TEXT))) = 0
