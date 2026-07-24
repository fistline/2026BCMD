-- Every current document must survive into at least one chunk.
--
-- silver.documents already drops blank documents (LENGTH(TRIM(content)) > 0), so
-- a document that reaches it has real text. If that document then owns zero
-- chunks, the WHOLE document fell out of retrieval in silence: extraction went
-- flat, a doc_type stopped being handled, or the sliver filter ate every piece.
-- No row-level audit sees this, because the evidence is an ABSENT row rather than
-- a malformed one -- the same blind spot that hid the 부칙 loss, one grain up at
-- the document level. assert_profile_sections_survived guards the section grain;
-- this guards the document.
--
-- A cross-build volume delta was the obvious alternative and was rejected: it
-- misses a single dropped document under any threshold loose enough to survive
-- legitimate re-chunking (the 부칙 loss was 1 chunk of ~15), and a per-machine
-- baseline is either data-in-git or a no-op on a fresh clone, breaking the
-- committed-deterministic baseline the rest of this repo keeps. This invariant
-- needs no baseline and runs everywhere, including CI and first clone.
--
-- Audited on silver.chunks, not silver.documents: silver.chunks already depends
-- on silver.documents, so reading it here adds no DAG cycle, whereas auditing
-- silver.documents against silver.chunks would. Mirrors
-- assert_relation_endpoints_resolved: LEFT JOIN, assert nothing missed.
-- Blocking: a vanished document must stop the build, not warn.
AUDIT (
  name assert_every_document_chunked,
  blocking true
);

SELECT
  d.doc_id,
  d.rel_path
FROM silver.documents AS d
LEFT JOIN (SELECT DISTINCT doc_id FROM @this_model) AS c
  ON c.doc_id = d.doc_id
WHERE c.doc_id IS NULL
