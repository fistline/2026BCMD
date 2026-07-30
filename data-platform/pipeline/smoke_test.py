"""End-to-end assertions against the built serving index.

Run by `make build` and by the verification gate. This is not a unit test of the
Python helpers: it opens `data/serving/index.sqlite` exactly the way the agent
will and asserts the two behaviours the platform exists to provide.

  1. Hybrid retrieval beats either half alone. The check is deliberately not
     "search returns something": it asserts that an exact code identifier is
     found via the keyword ranking, which is the case a vector-only index gets
     wrong, and that fusion still surfaces prose for a paraphrased query.

  2. Multi-hop impact analysis works. The fixtures encode
     service_api -> search_core -> vector_store, so asking what is affected by
     a change to vector_store must reach service_api at depth 2. A one-hop join
     would return only search_core, which is exactly the failure this catches.

Exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
import unicodedata
from itertools import pairwise
from pathlib import Path

# Smoke pins the FUSED core (deterministic retrieval + graph). The opt-in
# reranker is a query-time reordering with its own coverage -- test_reranker
# (which passes rerank=True explicitly) and `make eval-rerank`. Force it off for
# the whole run so a deployment .env with RERANK=1 cannot reorder the pinned
# results. Set BEFORE importing pipeline, whose load_dotenv is override=False, so
# this wins over .env.
os.environ["RERANK"] = "0"

from pipeline import chunking, get_paths, get_settings
from pipeline import extract as extract_module
from pipeline.aliases import expand_query
from pipeline.build_graph import graph_query, resolve_node
from pipeline.build_rag import (
    CJK_RUN,
    HashingEmbedder,
    _features,
    _fixture_doc_ids,
    build_fts_query,
    connect_index,
    expand_cjk,
    get_embedder,
    index_signature,
    serialize_vector,
)
from pipeline.build_rag import (
    hybrid_search as _hybrid_search,
)
from pipeline.chunking import (
    _KO_SECTION_RE,
    BINARY_SUFFIXES,
    CHUNK_OVERLAP_CHARS,
    FORMAT_PRIORITY,
    MAX_CHUNK_CHARS,
    MIN_ADVANCE_CHARS,
    SUPPORTED_SUFFIXES,
    ChunkingError,
    _split_window,
    chunk_text,
    document_id,
    normalise_entity,
    normalize_for_fingerprint,
    parse_document,
)
from pipeline.export import DocumentChunks
from pipeline.extract import ExtractionCorrupt, ExtractionEmpty, extract_text
from pipeline.watcher import _superseded_renditions


# Smoke fixtures share the serving index but build_rag hides them from real answers
# by default. In a fresh clone the corpus IS the fixtures, so the retrieval-
# mechanics assertions here must SEE them; this thin wrapper re-admits them. The
# no-leak assertion in test_hybrid_search calls _hybrid_search directly (default
# excluded) to prove the read path hides fixtures from a real answer.
def hybrid_search(*args, include_fixtures=True, rerank=False, **kwargs):
    # Force rerank=False so the retrieval PINS below test the fused core and stay
    # stable even when .env sets RERANK=1 (the opt-in reranker reorders results).
    # test_reranker passes rerank=True explicitly where it exercises the reranker.
    return _hybrid_search(*args, include_fixtures=include_fixtures, rerank=rerank, **kwargs)

FAILURES: list = []

# Pinned before the Korean retrieval work landed and re-confirmed after it, so a
# future change to the tokenizer or the n-gram widths cannot quietly reorder
# English and code results. Update only with a measurement that justifies it.
ASCII_PINNED_TOP5 = [
    "search_core.py",
    "service_api.py",
    "service_api.py",
    "architecture-overview.md",
    "serving-index.md",
]
# Chunk count of a build containing only the bundled fixtures. The strict
# ordering pin above is only meaningful against exactly that corpus.
FIXTURE_ONLY_CHUNK_COUNT = 28


SKIPS: list = []


def skip(description: str, reason: str) -> None:
    """A precondition was absent. NOT a pass -- and it must say why.

    Three checks used to bail by passing a literal True to `check` with a
    parenthetical reason,
    which prints the same green tick as a real pass. "The property holds" and
    "the property was not examined" then look identical in the output and in the
    exit code, which is the single pattern the audit found most often. A reason
    is mandatory because the skip line is the only evidence anyone gets.
    """
    SKIPS.append(f"{description}: {reason}")
    print(f"  SKIP {description} -- {reason}")


def check(description: str, condition: bool, detail=" ") -> None:
    if condition:
        print(f"  ok   {description}")
        return
    # `detail` is routinely a list or dict at the call sites, and a failing
    # assertion must not itself crash the run before reporting.
    rendered = f"{description}: {detail}" if detail else description
    FAILURES.append(rendered)
    print(f"  FAIL {rendered}")


def test_index_present(paths) -> None:
    print("index file")
    check("data/serving/index.sqlite exists", paths.index_sqlite.exists(), str(paths.index_sqlite))
    if not paths.index_sqlite.exists():
        raise SystemExit(1)

    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        for required in ("chunks", "chunks_vec", "chunks_fts", "nodes", "edges", "index_meta"):
            check(f"table {required} present", required in tables, sorted(tables))

        counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("chunks", "chunks_vec", "chunks_fts", "nodes", "edges")
        }
        distinct_content = connection.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM chunks GROUP BY collection, content)"
        ).fetchone()[0]
        print(f"       counts: {counts}  distinct_collection_content={distinct_content}")
        check("chunks is non-empty", counts["chunks"] > 0)
        # The retrieval surfaces hold one row per DISTINCT (collection, content):
        # identical boilerplate clauses are indexed once PER COLLECTION so they
        # cannot flood a top-K, yet a clause shared across collections stays
        # reachable when a search is scoped to either. `chunks` keeps every row for
        # provenance and gold<->index parity. The two halves must still match.
        check(
            "vector and keyword halves agree and hold one row per (collection, distinct content)",
            counts["chunks_vec"] == counts["chunks_fts"] == distinct_content,
            str({**counts, "distinct_collection_content": distinct_content}),
        )
        # Equal counts are a proxy; the real invariant is that the two halves index
        # the SAME representative chunk_ids. A future edit could keep the counts equal
        # while selecting different rows for the vector vs keyword halves -- a silent
        # de-sync the count check cannot see (index_signature would then lie).
        vec_ids = {row[0] for row in connection.execute("SELECT chunk_id FROM chunks_vec")}
        fts_ids = {row[0] for row in connection.execute("SELECT chunk_id FROM chunks_fts")}
        check(
            "vector and keyword halves index the SAME chunk_ids, not just equal counts",
            vec_ids == fts_ids,
            f"only in vec: {sorted(vec_ids - fts_ids)[:5]}  only in fts: {sorted(fts_ids - vec_ids)[:5]}",
        )
        check(
            "the retrieval surface never exceeds the full chunk provenance",
            counts["chunks_vec"] <= counts["chunks"],
            str(counts),
        )
        check("graph has nodes and edges", counts["nodes"] > 0 and counts["edges"] > 0, str(counts))
    finally:
        connection.close()


def test_hybrid_search() -> None:
    print("hybrid_search")

    # Fixture-leak guard: the DEFAULT read path (include_fixtures False) must not
    # surface a smoke fixture in a real answer. On a fixture-only build the corpus
    # IS fixtures, so the default returns nothing -- still zero leaked.
    default_hits = _hybrid_search("architecture overview vector_store search_core", limit=8)
    leaked = sorted({hit["doc_id"] for hit in default_hits} & _fixture_doc_ids())
    check("default search hides smoke fixtures from real answers", not leaked, leaked)

    identifier_hits = hybrid_search("hybrid_search", limit=5)
    check("exact identifier query returns hits", bool(identifier_hits), "no rows")
    if identifier_hits:
        matched = [hit for hit in identifier_hits if "hybrid_search" in hit["content"]]
        check(
            "exact identifier appears verbatim in a returned chunk",
            bool(matched),
            [hit["rel_path"] for hit in identifier_hits],
        )
        keyword_ranked = [hit for hit in identifier_hits if hit["keyword_rank"] is not None]
        check(
            "FTS5 side contributed to the fused ranking",
            bool(keyword_ranked),
            "keyword_rank was NULL for every hit",
        )
        check(
            "the code definition of hybrid_search is retrieved",
            any(hit["rel_path"].endswith("search_core.py") for hit in identifier_hits),
            [hit["rel_path"] for hit in identifier_hits],
        )

    prose_hits = hybrid_search("how are documents promoted into the immutable zone", limit=5)
    check("paraphrased prose query returns hits", bool(prose_hits), "no rows")
    if prose_hits:
        vector_ranked = [hit for hit in prose_hits if hit["vector_rank"] is not None]
        check(
            "sqlite-vec side contributed to the fused ranking",
            bool(vector_ranked),
            "vector_rank was NULL for every hit",
        )
        check(
            "ingestion document is surfaced for the paraphrase",
            any("ingestion-pipeline" in hit["doc_id"] for hit in prose_hits),
            [hit["doc_id"] for hit in prose_hits],
        )

    scores = [hit["rrf_score"] for hit in identifier_hits]
    check("results are ordered by descending RRF score", scores == sorted(scores, reverse=True), str(scores))

    # Collection scoping (folder-as-collection). Every hit carries its collection,
    # a scope to its own collection returns the same hits, and a scope to a
    # collection that does not exist returns nothing rather than leaking across.
    from pipeline.build_rag import collection_of

    check(
        "collection_of derives the first inbox folder",
        collection_of("sto/열매.txt") == "sto" and collection_of("flat.txt") == "_root",
        collection_of("sto/열매.txt"),
    )
    if identifier_hits:
        hit = identifier_hits[0]
        own = hit["collection"]
        scoped = hybrid_search("hybrid_search", limit=5, collection=own)
        check(
            "a search scoped to a hit's own collection still finds it",
            any(row["chunk_id"] == hit["chunk_id"] for row in scoped),
            f"scoped to {own!r} lost {hit['chunk_id']}",
        )
        check(
            "every scoped hit is in the requested collection",
            all(row["collection"] == own for row in scoped),
            [row["collection"] for row in scoped],
        )
        empty = hybrid_search("hybrid_search", limit=5, collection="__no_such_collection__")
        check("a scope to a nonexistent collection returns nothing", empty == [], f"{len(empty)} hits leaked")


def test_korean_retrieval() -> None:
    """Korean is the primary corpus, so these are the load-bearing assertions.

    Each one fails for a specific mutation rather than merely proving that rows
    come back. A check like "returns some hits" passes on every broken build.
    """
    print("korean retrieval")

    # Corpus-content pins (a fixture doc at an exact rank) only hold on the
    # bundled fixtures; once real documents are indexed the ranking legitimately
    # shifts, so those pins run fixture-only and a corpus-independent claim runs
    # otherwise. Same split as the ASCII pin below. `make eval` is the retrieval
    # gate for the real corpus.
    _conn = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        fixture_only = _conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == FIXTURE_ONLY_CHUNK_COUNT
    finally:
        _conn.close()

    # 1. Substring inside a compound word. This is a property of the FTS bigram
    #    expansion, so it is asserted with alias expansion OFF: with it on, a
    #    query for 가상자산 also retrieves the 디지털자산 bills, which is correct
    #    behaviour but would let this assertion pass for the wrong reason.
    compound = hybrid_search("가상자산", limit=5, expand=False)
    # Asserted against chunks_fts directly rather than against the fused top-5.
    # The mechanism under test is the bigram expansion -- does a substring query
    # reach INSIDE 가상자산사업자는 -- and that lives in the FTS half. Reading it
    # through a top-5 made it hostage to corpus growth instead: shrinking
    # MAX_CHUNK_CHARS to 650 handed every real statute's chunk a BM25
    # length-normalisation gain and pushed this fixture past rank 30 with the
    # mechanism itself untouched (its four sections are ~110 chars and were never
    # windowed at either size). A ranking assertion cannot answer a mechanism
    # question once a real corpus is indexed; the sibling assertion below already
    # says so in its own comment.
    _fts = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        matched = [
            row[0]
            for row in _fts.execute(
                "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?",
                (build_fts_query("가상자산"),),
            )
        ]
    finally:
        _fts.close()
    check(
        "'가상자산' matches inside 가상자산사업자는 / 가상자산이용자보호법",
        any(chunk_id.startswith("korean-terms#") for chunk_id in matched),
        f"{len(matched)} FTS matches, none of them from korean-terms",
    )
    check(
        "the keyword half found the substring, not just the vector half",
        any(hit["keyword_rank"] is not None for hit in compound),
        "keyword_rank was NULL for every hit",
    )
    # And with expansion on, the same query must reach the bills that call the
    # concept 디지털자산 instead.
    expanded = hybrid_search("가상자산", limit=5)
    check(
        "expansion carries 가상자산 to the 디지털자산 bills",
        any("디지털자산" in hit["doc_id"] for hit in expanded),
        [hit["doc_id"][:28] for hit in expanded[:3]],
    )

    # 2. Particle-glued query against deliberate distractors. korean-terms holds
    #    제7조(신고한 업무의 요건) and korean-penalty holds 제12조(신고를 게을리한
    #    자에 대한 과태료); both must lose to 제3조.
    # The property is that 제3조 outranks the two deliberate 신고 distractors in
    # the same fixture, NOT that it wins against every document the user later
    # adds. Pinning rank 1 outright fails as soon as a real corpus is indexed,
    # which is a corpus change and not a retrieval regression.
    glued = hybrid_search("가상자산사업자의 신고 요건", limit=10)

    def _rank_of(prefix: str):
        for position, hit in enumerate(glued):
            if hit["heading"].split(" · ")[-1].startswith(prefix):
                return position
        return None

    target = _rank_of("제3조(가상자산사업자의 신고)")
    distractors = {
        "제7조(신고한 업무의 요건)": _rank_of("제7조(신고한 업무의 요건)"),
        "제12조(신고를 게을리한": _rank_of("제12조(신고를 게을리한"),
    }
    if fixture_only:
        check(
            "particle-glued query beats the 신고 distractors",
            target is not None and all(other is None or target < other for other in distractors.values()),
            f"제3조 at {target}, distractors at {distractors}",
        )
    else:
        print("       (real corpus indexed; fixture distractor-rank pin skipped)")

    # 3. Two-character legal term. The trigram tokenizer cannot match these at
    #    all, which is why the bigram expansion was chosen over it.
    two_char = hybrid_search("증권", limit=5)
    if fixture_only:
        check(
            "two-character term 증권 retrieves 제11조(벌칙)",
            bool(two_char) and two_char[0]["heading"].startswith("제11조(벌칙)"),
            [hit["heading"] for hit in two_char[:3]],
        )
    else:
        # Corpus-independent property: the bigram expansion still matches a
        # 2-char term (the trigram tokenizer could not), even when real 증권
        # documents outrank the fixture's 제11조(벌칙).
        check(
            "two-character term 증권 still matches via the bigram expansion",
            bool(two_char) and any(hit["keyword_rank"] is not None for hit in two_char),
            [hit["heading"] for hit in two_char[:3]],
        )

    # 4. The vector half alone. Everything above could pass on the keyword half,
    #    so this is the only assertion that actually tests the embedder.
    settings = get_settings()
    embedder = get_embedder(settings)
    vector = embedder.encode(["예치금 분리보관"])[0]
    connection = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        top = connection.execute(
            "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = 5 "
            "ORDER BY distance",
            (serialize_vector(vector),),
        ).fetchall()
        headings = [
            connection.execute(
                "SELECT doc_id, heading FROM chunks WHERE chunk_id = ?", (row[0],)
            ).fetchone()
            for row in top
        ]
    finally:
        connection.close()
    if fixture_only:
        check(
            "vector-only search reaches 제4조(예치금의 보호)",
            bool(headings)
            and headings[0]["doc_id"] == "korean-terms"
            and headings[0]["heading"].startswith("제4조(예치금의 보호)"),
            [dict(row) for row in headings[:3]],
        )
    else:
        # The exact top doc is corpus-dependent once real filings are indexed;
        # assert the vector arm still returns hits.
        check(
            "vector-only search returns hits on the real corpus",
            bool(headings),
            [dict(row) for row in headings[:3]],
        )

    # 5. Cheap contract check. Explicitly NOT a quality check.
    check(
        "Korean text produces a non-zero embedding",
        any(embedder.encode(["가상자산이용자보호법 시행령"])[0]),
        "all components were zero",
    )

    # 6. English and code must not have moved. The exact ordering is a property
    #    of the corpus, so the strict pin applies only to the bundled fixtures;
    #    against a larger corpus the corpus-independent claim is asserted
    #    instead. Pinning the full list unconditionally would fail the moment a
    #    user drops their own documents in, which is not a regression.
    ascii_hits = hybrid_search("hybrid_search", limit=5)
    ascii_paths = [hit["rel_path"] for hit in ascii_hits]
    check(
        "the definition of hybrid_search still ranks first",
        bool(ascii_paths) and ascii_paths[0] == "search_core.py",
        ascii_paths,
    )
    connection = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        connection.close()
    if chunk_count == FIXTURE_ONLY_CHUNK_COUNT:
        check(
            "ASCII ranking is unchanged from the pinned baseline",
            ascii_paths == ASCII_PINNED_TOP5,
            ascii_paths,
        )
    else:
        print(
            f"       (corpus has {chunk_count} chunks, not the bundled "
            f"{FIXTURE_ONLY_CHUNK_COUNT}; strict ASCII pin skipped)"
        )


def test_alias_expansion() -> None:
    """Domain synonyms are the one gap character n-grams provably cannot close."""
    print("alias expansion")

    # Every alias row must cite the article that asserts the equivalence, or the
    # table becomes folklore nobody can check.
    table = (get_paths().root / "pipeline" / "aliases.tsv").read_text(encoding="utf-8")
    rows = [
        line.split("\t")
        for line in table.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    check("every alias row carries a provenance column", all(len(row) >= 3 and row[2].strip() for row in rows), len(rows))

    variants = expand_query("스테이블코인 발행자의 자기자본 요건")
    check(
        "a stablecoin query expands to the statutory coinages",
        any("자산연동형" in variant for variant in variants)
        and any("가치안정형" in variant for variant in variants),
        variants[:4],
    )
    check("the original query is always the first variant", variants[0].startswith("스테이블코인"), variants[0])
    check(
        "an ASCII query is not expanded",
        expand_query("hybrid_search") == ["hybrid_search"],
        expand_query("hybrid_search"),
    )

    # The measured failure that motivated the alias table.
    hits = hybrid_search("스테이블코인 발행자의 자기자본 요건", limit=5)
    check(
        "the stablecoin capital query reaches a 발행자 article",
        any(
            ("발행자" in hit["heading"] or "발행인" in hit["heading"] or "건전성" in hit["heading"])
            for hit in hits
        ),
        [hit["heading"][:34] for hit in hits[:3]],
    )

    # Expansion must not cost precision where the query already matched exactly.
    exact = hybrid_search("예치금 분리보관 의무", limit=5)
    check(
        "expansion does not damage an exact-vocabulary query",
        any("예치금" in hit["heading"] for hit in exact),
        [hit["heading"][:30] for hit in exact[:3]],
    )

    # Scores are averaged over the arms, so a many-variant query is not inflated
    # relative to a single-variant one. Without this any future relevance
    # threshold would be unsettable.
    single = hybrid_search("hybrid_search", limit=1)
    multi = hybrid_search("스테이블코인 발행자", limit=1)
    check(
        "fused scores stay on a comparable scale across variant counts",
        bool(single) and bool(multi) and multi[0]["rrf_score"] <= single[0]["rrf_score"] * 3,
        f"single={single[0]['rrf_score']:.5f} multi={multi[0]['rrf_score']:.5f}" if single and multi else "no rows",
    )


def test_retrieval_internals() -> None:
    """Pure-function contracts that no end-to-end query would pin."""
    print("retrieval internals")

    # The Private Use Area must NOT be classified as CJK: Hancom HWP files use
    # it routinely, and a mistyped codepoint range silently swallows it.
    check("PUA is not treated as CJK", not CJK_RUN.search(""))
    check("CJK Extension A is treated as CJK", bool(CJK_RUN.search("㐀")))
    check("halfwidth katakana is treated as CJK", bool(CJK_RUN.search("ｦ")))

    # A mixed run must split, or the Latin part is shredded into CJK n-grams.
    check(
        "cross-script run keeps its Latin token",
        "etf" in _features("가상자산ETF"),
        sorted(_features("가상자산ETF")),
    )

    # Build side and query side must derive the same n-grams, or the keyword
    # half silently returns nothing for every Korean query.
    indexed = set(expand_cjk("가상자산이용자보호법").split())
    queried = {term.strip('"') for term in build_fts_query("가상자산").split(" OR ")}
    check(
        "query n-grams are a subset of the indexed n-grams",
        queried <= indexed,
        sorted(queried - indexed),
    )

    check(
        "ASCII identifiers survive the FTS query builder whole",
        build_fts_query("hybrid_search") == '"hybrid_search"',
        build_fts_query("hybrid_search"),
    )

    # Every provider must declare model_name, or index_signature carries an
    # empty slot and two same-dimension models are indistinguishable to the
    # staleness guard: queries encoded by one model, vectors built by another,
    # plausible-looking garbage, no error ever.
    class _ProviderWithoutModelName:
        name = "broken"

    try:
        index_signature(_ProviderWithoutModelName(), 1024)
        check("index_signature rejects a provider with no model_name", False, "no error raised")
    except TypeError as error:
        check(
            "index_signature rejects a provider with no model_name",
            "model_name" in str(error),
            str(error)[:80],
        )
    check(
        "the hashing provider declares model_name",
        hasattr(HashingEmbedder(1024), "model_name"),
        "HashingEmbedder has no model_name attribute",
    )

    # A dimension too small for Korean n-gram vocabulary must fail loudly.
    try:
        HashingEmbedder(256)
        check("EMBEDDING_DIM floor is enforced", False, "no error raised")
    except ValueError as error:
        check("EMBEDDING_DIM floor is enforced", "EMBEDDING_DIM" in str(error), str(error))

    # The stale-index guard must reject an index built by different logic.
    settings = get_settings()
    embedder = get_embedder(settings)
    stored = index_signature(embedder, settings.embedding_dim)
    connection = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        actual = connection.execute(
            "SELECT value FROM index_meta WHERE key = 'index_signature'"
        ).fetchone()
    finally:
        connection.close()
    check(
        "index_signature is recorded and matches the current settings",
        actual is not None and actual[0] == stored,
        f"stored={actual[0] if actual else None!r} expected={stored!r}",
    )


def test_chunking() -> None:
    """Chunker contracts that only show up on statute-shaped text."""
    print("chunking")

    # Forward progress. A body with one blank line used to yield 114 chunks of
    # lengths 1159, 150, 149, 148 because the cursor advanced by exactly 1.
    body = ("x" * 39 + "\n") * 29 + "\n" + ("y" * 39 + "\n") * 50
    pieces = list(_split_window(body, 0))
    starts = [piece[1] for piece in pieces]
    # Expressed against the constants, not against a number that happened to be
    # right at one chunk size: `<= 4` was arithmetic for MAX_CHUNK_CHARS=1200 and
    # became unsatisfiable at 650 while the chunker was behaving correctly. This
    # bound is what full-window stepping gives, plus one for a partial tail, so it
    # still catches the 114-chunk explosion the comment above describes.
    window_bound = -(-len(body) // (MAX_CHUNK_CHARS - CHUNK_OVERLAP_CHARS)) + 1
    check(
        "sliding window terminates in a sane number of chunks",
        len(pieces) <= window_bound,
        f"{len(pieces)} pieces (bound {window_bound}) of lengths {[len(p[0]) for p in pieces[:6]]}",
    )
    check(
        "every window advances by a meaningful amount",
        all(later - earlier >= MIN_ADVANCE_CHARS for earlier, later in pairwise(starts)),
        [later - earlier for earlier, later in pairwise(starts)][:6],
    )

    # Article sectioning on text with no Markdown headings, which is what every
    # extracted statute looks like.
    statute = (
        "제3조(가상자산사업자의 신고)\n신고 의무를 정한 본문이며 충분히 길게 쓴 문장이다.\n\n"
        "제4조 (예치금의 보호)\n분리보관 의무를 정한 본문이며 역시 충분히 길게 쓴 문장이다.\n\n"
        "부칙\n이 법은 공포 후 1년이 경과한 날부터 시행한다. 경과조치를 둔다."
    )
    headings = [chunk.heading for chunk in chunk_text("t", statute)]
    check(
        "statute text sections on 제N조 and 부칙",
        headings == ["제3조(가상자산사업자의 신고)", "제4조 (예치금의 보호)", "부칙"],
        headings,
    )

    # A short but COMPLETE section must survive. 부칙 carries the enforcement
    # date, the single most-queried fact about a statute, and is typically ~30
    # characters — under MIN_CHUNK_CHARS. Applying the sliver filter to it
    # silently dropped that date from 3 of 8 real bills on a green build.
    short_section = "제1조(목적)\n이 법은 목적을 정한다.\n\n부칙\n이 법은 공포한 날부터 시행한다."
    short_chunks = chunk_text("t", short_section)
    check(
        "a short complete section is not dropped by the sliver filter",
        any("시행한다" in chunk.content for chunk in short_chunks),
        [(chunk.heading, len(chunk.content)) for chunk in short_chunks],
    )
    check(
        "the 부칙 section keeps its own heading",
        any(chunk.heading == "부칙" for chunk in short_chunks),
        [chunk.heading for chunk in short_chunks],
    )

    # A profile-declared section that produces no chunk is a silent structural
    # loss -- the 부칙 bug generalised. chunk_text must REFUSE it rather than ship
    # a document missing a section its profile named. Driven directly (raising the
    # floor above every window piece) because the shipped filter can no longer
    # trigger it; the point is to keep it that way for profiles added later.
    saved_floor = chunking.MIN_CHUNK_CHARS
    try:
        chunking.MIN_CHUNK_CHARS = 10_000  # forces every window piece under the floor
        over_one_window = "가" * (MAX_CHUNK_CHARS * 2)  # splits into >1 sub-floor pieces
        raised = False
        try:
            chunk_text("t", over_one_window, spans=[("본칙 · 제1조(목적)", 0, len(over_one_window))])
        except ChunkingError as error:
            raised = "본칙 · 제1조(목적)" in str(error)
        check("a profile-declared section erased by the filter raises", raised)

        # The 부칙 guarantee, under the same hostile floor: a lone short section
        # bypasses the filter (whole_section) and survives without raising.
        survivor = chunk_text("t", "이 법은 공포한 날부터 시행한다.", spans=[("부칙", 0, 14)])
        check(
            "a lone short declared section survives even under a hostile floor",
            len(survivor) == 1 and survivor[0].heading == "부칙",
            [(c.heading, c.content) for c in survivor],
        )

        # A whitespace-only declared span (the gap before the first marker) is
        # allowed to produce nothing -- it must NOT raise.
        blank_ok = True
        try:
            check(
                "a whitespace-only declared span produces no chunk and does not raise",
                chunk_text("t", "   \n\n  ", spans=[("", 0, 7)]) == [],
            )
        except ChunkingError:
            blank_ok = False
        check("a whitespace-only declared span never raises", blank_ok)
    finally:
        chunking.MIN_CHUNK_CHARS = saved_floor

    # An inline cross-reference must not be mistaken for a heading.
    check(
        "inline 조문 reference is not a section heading",
        _KO_SECTION_RE.findall("제4조제1항에 따른 신고를 하여야 한다.") == [],
        _KO_SECTION_RE.findall("제4조제1항에 따른 신고를 하여야 한다."),
    )

    # Document identity. `document_id` and `normalise_entity` must agree over
    # the whole suffix table, or a document becomes an orphan node that no edge
    # touches plus an empty edge-endpoint stub - and because gold.entities
    # manufactures a node for every edge endpoint, the blocking referential
    # audit would still pass while the graph quietly fragmented.
    suffix_table = [
        "search_core.py",
        "chunking-rules.md",
        "notes.txt",
        "spec.rst",
        "가상자산이용자보호법.md",
        "01_디지털자산기본법안.hwp",
        "01_디지털자산기본법안.hwpx",
        "01_디지털자산기본법안.pdf",
        "감정평가서.docx",
        "감정평가서.doc",
        "수익률표.xlsx",
        "수익률표.xls",
        "설명자료.pptx",
        "설명자료.ppt",
    ]
    mismatched = [name for name in suffix_table if document_id(name) != normalise_entity(name)]
    check("document_id agrees with normalise_entity over the suffix table", not mismatched, mismatched)

    check(
        "a Korean filename keeps its name instead of an opaque hash",
        document_id("가상자산이용자보호법.md") == "가상자산이용자보호법",
        document_id("가상자산이용자보호법.md"),
    )
    check(
        "NFD and NFC filenames resolve to the same document",
        document_id("가상자산이용자보호법.md")
        == document_id(unicodedata.normalize("NFD", "가상자산이용자보호법.md")),
        document_id(unicodedata.normalize("NFD", "가상자산이용자보호법.md")),
    )
    check(
        "a .hwp and its .hwpx twin stay distinct documents",
        document_id("01_디지털자산기본법안.hwp") != document_id("01_디지털자산기본법안.hwpx"),
        document_id("01_디지털자산기본법안.hwp"),
    )
    check(
        "two different Korean bills do not collide",
        document_id("01_디지털자산기본법안.hwp") != document_id("02_토큰증권발행법안.hwp"),
        document_id("02_토큰증권발행법안.hwp"),
    )
    check(
        "ASCII document ids are unchanged",
        document_id("search_core.py") == "search_core"
        and document_id("chunking-rules.md") == "chunking-rules",
        (document_id("search_core.py"), document_id("chunking-rules.md")),
    )

    # The cross-format dedup rests on normalize_for_fingerprint: it must fold only
    # layout whitespace, never case or real content, or renditions merge wrongly.
    check(
        "whitespace and line-ending variants share one fingerprint",
        normalize_for_fingerprint("제1조 목적\n\n이 법은  ")
        == normalize_for_fingerprint("제1조 목적\r\n\r\n이 법은"),
        None,
    )
    check(
        "different content yields different fingerprints",
        normalize_for_fingerprint("제1조(목적)") != normalize_for_fingerprint("제2조(정의)"),
        None,
    )
    check(
        "case is preserved so code identifiers never fold together",
        normalize_for_fingerprint("VectorStore") != normalize_for_fingerprint("vectorstore"),
        None,
    )

    # The same document from Windows and from macOS must index identically.
    crlf_body = "# T\n\n" + "가나다라마바사아자차 " * 40 + "\n\n" + "카타파하 " * 40
    lf_doc = parse_document("a.md", crlf_body.encode())
    crlf_doc = parse_document("a.md", crlf_body.replace("\n", "\r\n").encode())
    check(
        "CRLF and LF produce identical chunks",
        [chunk.chunk_id for chunk in lf_doc.chunks] == [chunk.chunk_id for chunk in crlf_doc.chunks]
        and [chunk.content for chunk in lf_doc.chunks]
        == [chunk.content for chunk in crlf_doc.chunks],
        "chunk ids or content differ between line endings",
    )


def _synthetic_hwpx(paragraphs) -> bytes:
    """Build a minimal valid HWPX in memory.

    Generated rather than committed: the repo must not carry binary fixtures,
    and a generated one also pins the exact XML shape the reader expects.
    """
    import io
    import zipfile

    body = "".join(
        "<hp:p>" + "".join(f"<hp:t>{run}</hp:t>" for run in runs) + "</hp:p>"
        for runs in paragraphs
    )
    section = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
        'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
        f"{body}</hs:sec>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", section)
    return buffer.getvalue()


def _ooxml(parts: dict) -> bytes:
    """Build a minimal OOXML package in memory from {part name: xml}."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_P_NS = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'


def test_ooxml_extraction() -> None:
    """DOCX, XLSX and PPTX reading, which uses no third-party parser at all."""
    print("OOXML extraction")

    # A paragraph with a tab and a line break, a TABLE cell, and a text box whose
    # DrawingML paragraph is NESTED inside a Word paragraph. The nesting is the
    # point: a naive walk emits the cell and the box twice, which double-indexes
    # the 신구조문대비표 of a bill while every count still looks plausible.
    docx = _ooxml({
        "word/document.xml": (
            f"<w:document {_W_NS} {_A_NS}><w:body>"
            "<w:p><w:r><w:t>제1조(목적)</w:t><w:tab/><w:t>이 법은</w:t>"
            "<w:br/><w:t>둘째 줄</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>셀</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "<w:p><w:r><w:drawing><a:p><a:r><a:t>상자</a:t></a:r></a:p></w:drawing></w:r></w:p>"
            "</w:body></w:document>"
        ),
        "word/footnotes.xml": f"<w:footnotes {_W_NS}><w:p><w:r><w:t>각주</w:t></w:r></w:p></w:footnotes>",
    })
    check(
        "DOCX reads tabs, breaks, tables, text boxes and footnotes exactly once",
        extract_text("a.docx", docx) == "제1조(목적)\t이 법은\n둘째 줄\n셀\n상자\n각주",
        repr(extract_text("a.docx", docx)),
    )

    # slide10 before slide2 in the zip. Lexicographic order would reverse the
    # deck and move every chunk id in it.
    slide = f"<p:sld {_P_NS} {_A_NS}><p:cSld><p:spTree>%s</p:spTree></p:cSld></p:sld>"
    pptx = _ooxml({
        "ppt/slides/slide10.xml": slide % "<a:p><a:r><a:t>열번째</a:t></a:r></a:p>",
        "ppt/slides/slide2.xml": slide % "<a:p><a:r><a:t>두번째</a:t></a:r></a:p>",
        "ppt/notesSlides/notesSlide2.xml": slide % "<a:p><a:r><a:t>메모</a:t></a:r></a:p>",
    })
    check(
        "PPTX orders slides numerically and pairs notes with their slide",
        extract_text("b.pptx", pptx)
        == "# 슬라이드 1\n두번째\n## 슬라이드 1 노트\n메모\n# 슬라이드 2\n열번째",
        repr(extract_text("b.pptx", pptx)),
    )

    # Sheets are declared 손익-then-재무 but stored sheet1-then-sheet2, so zip
    # order and declaration order disagree. Declaration order is the visible one.
    xlsx = _ooxml({
        "xl/workbook.xml": (
            '<workbook xmlns:r="R"><sheets>'
            '<sheet name="손익" r:id="rId2"/><sheet name="재무" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            "<Relationships>"
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            "</Relationships>"
        ),
        "xl/sharedStrings.xml": "<sst><si><t>매출액</t></si><si><t>영업이익</t></si></sst>",
        "xl/worksheets/sheet1.xml": (
            '<worksheet><sheetData><row><c t="s"><v>0</v></c><c><v>1000</v></c>'
            "</row></sheetData></worksheet>"
        ),
        "xl/worksheets/sheet2.xml": (
            '<worksheet><sheetData><row><c t="s"><v>1</v></c>'
            '<c t="inlineStr"><is><t>흑자</t></is></c><c t="b"><v>1</v></c></row>'
            "<row><c/></row></sheetData></worksheet>"
        ),
    })
    check(
        "XLSX follows workbook declaration order and resolves shared strings",
        extract_text("c.xlsx", xlsx) == "# 시트: 손익\n영업이익\t흑자\tTRUE\n# 시트: 재무\n매출액\t1000",
        repr(extract_text("c.xlsx", xlsx)),
    )

    for name, payload, expected in (
        ("d.docx", b"not a zip at all", ExtractionCorrupt),
        ("e.xlsx", _ooxml({"xl/workbook.xml": "<workbook><sheets/></workbook>"}), ExtractionCorrupt),
        ("f.pptx", _ooxml({"unrelated.xml": "<a/>"}), ExtractionCorrupt),
        ("g.doc", b"not an OLE compound file", ExtractionCorrupt),
        (
            "h.docx",
            _ooxml({"word/document.xml": f"<w:document {_W_NS}><w:p><w:r><w:t> </w:t></w:r></w:p></w:document>"}),
            ExtractionEmpty,
        ),
    ):
        try:
            extract_text(name, payload)
            raised = None
        except Exception as error:  # noqa: BLE001 - reported as a check failure
            raised = error
        check(
            f"{name} is rejected as {expected.__name__}",
            isinstance(raised, expected) and name in str(raised),
            f"{type(raised).__name__}: {raised}",
        )

    # An OLE container that is NOT an encrypted package must stay a plain
    # corruption, or a merely damaged file would be reported as needing a
    # password. A real HWP is the OLE file this repo always has to hand.
    bills = sorted((get_paths().root / "source").rglob("*.hwp"))
    if bills:
        try:
            extract_text("mislabelled.docx", bills[0].read_bytes())
            raised = None
        except Exception as error:  # noqa: BLE001 - reported as a check failure
            raised = error
        check(
            "an OLE file that is not an encrypted package is corrupt, not protected",
            isinstance(raised, ExtractionCorrupt),
            f"{type(raised).__name__}: {raised}",
        )


def test_format_ladder() -> None:
    """The rendition ladder in Python and the one in SQL must be one ladder."""
    print("format ladder")

    model = (get_paths().root / "transform" / "models" / "silver" / "documents.sql").read_text(
        encoding="utf-8"
    )
    ranked = re.findall(r"WHEN\s+'(\w+)'\s+THEN\s+(\d+)", model)
    sql_order = [doc_type for doc_type, _rank in sorted(ranked, key=lambda p: -int(p[1]))]
    check(
        "silver.documents ranks formats in FORMAT_PRIORITY order",
        sql_order == list(FORMAT_PRIORITY),
        f"sql={sql_order} python={list(FORMAT_PRIORITY)}",
    )
    # document_twins.sql carries its OWN copy of the same ladder to pick the twin
    # winner (higher priority survives). It must not drift from FORMAT_PRIORITY, or a
    # derived .pdf could be kept over its own .hwp source when both reach bronze.
    twins_model = (
        get_paths().root / "transform" / "models" / "silver" / "document_twins.sql"
    ).read_text(encoding="utf-8")
    twins_ranked = re.findall(r"WHEN\s+'(\w+)'\s+THEN\s+(\d+)", twins_model)
    twins_order = [dt for dt, _r in sorted(twins_ranked, key=lambda p: -int(p[1]))]
    check(
        "silver.document_twins ranks formats in FORMAT_PRIORITY order",
        twins_order == list(FORMAT_PRIORITY),
        f"sql={twins_order} python={list(FORMAT_PRIORITY)}",
    )
    # The twin thresholds are literals in the SQL model and named constants in the
    # controls harness (report_dupes). A silent divergence would certify the feature
    # against different numbers than it runs on, so pin them to one source here.
    from pipeline.report_dupes import TWIN_JACCARD_T, TWIN_LEN_RATIO

    sql_jaccard = re.search(r"jaccard\s*>=\s*([0-9.]+)", twins_model)
    sql_len_ratio = re.search(r"norm_len\)\s*<\s*([0-9.]+)", twins_model)
    check(
        "document_twins Jaccard/length-ratio literals match the harness constants",
        bool(sql_jaccard) and bool(sql_len_ratio)
        and float(sql_jaccard.group(1)) == TWIN_JACCARD_T
        and float(sql_len_ratio.group(1)) == TWIN_LEN_RATIO,
        f"sql=({sql_jaccard and sql_jaccard.group(1)},{sql_len_ratio and sql_len_ratio.group(1)}) "
        f"python=({TWIN_JACCARD_T},{TWIN_LEN_RATIO})",
    )
    check(
        "every ranked format is a suffix the pipeline ingests",
        all(f".{doc_type}" in SUPPORTED_SUFFIXES for doc_type in FORMAT_PRIORITY),
        [d for d in FORMAT_PRIORITY if f".{d}" not in SUPPORTED_SUFFIXES],
    )

    # A curated twin set seeds only its top-ranked rendition. Measured on this
    # corpus, an HWP and its PDF twin do NOT share a content fingerprint, so the
    # content dedup downstream would not catch what this skip prevents.
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        for name in ("bill.hwp", "bill.pdf", "bill.txt", "solo.pdf"):
            (root / name).write_bytes(b"x")
        skipped = {path.name for path in _superseded_renditions(sorted(root.iterdir()))}
    check(
        "a PDF beside its HWP original is not seeded, and a lone PDF still is",
        skipped == {"bill.pdf"},
        sorted(skipped),
    )


def test_twin_detector() -> None:
    """Fuzzy-twin detector: norm parity with SQL, plus a synthetic regression.

    The detector (transform/models/silver/document_twins.sql) and its harness
    (pipeline/report_dupes.py --controls) must compute the SAME normalised
    string, or the harness's green numbers say nothing about the model. Parity
    is asserted byte-for-byte, including the characters where RE2 and Python
    disagree by default (U+3000, U+00A0). The regression then runs the REAL
    model SQL against a synthetic bronze: a cross-format block-reordered twin
    must be marked, while a same-format one-char correction and a genuinely
    different document must not. Skips while the model is not landed.
    """
    print("fuzzy twin detector")
    import duckdb

    from pipeline.report_dupes import TWIN_JACCARD_T, _norm_n2

    con = duckdb.connect(":memory:")

    def duck_norm(value: str) -> str:
        return con.execute(
            "SELECT regexp_replace(nfc_normalize(?), '\\s', '', 'g')", [value]
        ).fetchone()[0]

    tricky = "가Á 나　다 라\r\n마 \t바"  # NFD, U+3000, NBSP, CRLF
    check(
        "DuckDB and Python agree byte-for-byte on the twin norm",
        duck_norm(tricky) == _norm_n2(tricky),
        f"duckdb={duck_norm(tricky)!r} python={_norm_n2(tricky)!r}",
    )
    bills = sorted((get_paths().root / "source").rglob("*.hwp"))
    if bills:
        text = extract_text(bills[0].name, bills[0].read_bytes())[:20000]
        check(
            "the norm parity holds on real bill text",
            duck_norm(text) == _norm_n2(text),
            "diverged on corpus sample",
        )

    model = get_paths().root / "transform" / "models" / "silver" / "document_twins.sql"
    if not model.exists():
        print("       (document_twins.sql not landed; detector regression skipped)")
        return

    # Strip the MODEL () header: it ends at the first line that is exactly ');'.
    # The description string CONTAINS the two characters ');', so a plain
    # substring split would land inside it.
    parts = re.split(r"^\);\s*$", model.read_text(encoding="utf-8"), maxsplit=1, flags=re.M)
    check("document_twins.sql has a MODEL header to strip", len(parts) == 2, "no ');' line")
    body = parts[1]

    con.execute("CREATE SCHEMA bronze")
    con.execute(
        "CREATE TABLE bronze.documents ("
        "doc_id VARCHAR, rel_path VARCHAR, doc_type VARCHAR, content VARCHAR,"
        "content_sha256 VARCHAR, ingested_at TIMESTAMP)"
    )
    base = "".join(
        f"제{i}조(항목{i}) {i}번째 조문은 디지털자산 사업자의 의무와 절차를 정한다. "
        for i in range(1, 60)
    )
    reordered = base[1300:] + " \n　" + base[:1300]  # block swap + whitespace drift
    different = "".join(
        f"제{i}조(별건{i}) 전혀 다른 법률의 {i}번째 규정이 이어진다. " for i in range(1, 60)
    )
    corrected = base.replace("의무와 절차", "의무와 정차", 1)  # 1-char 정정, same format
    for doc_id, rel_path, doc_type, content in (
        ("bill-hwp", "bill.hwp", "hwp", base),
        ("bill-pdf", "bill.pdf", "pdf", reordered),
        ("other-pdf", "other.pdf", "pdf", different),
        ("bill2-hwp", "bill2.hwp", "hwp", corrected),
    ):
        con.execute(
            "INSERT INTO bronze.documents VALUES (?, ?, ?, ?, ?, TIMESTAMP '2026-01-01')",
            [doc_id, rel_path, doc_type, content, doc_id],
        )
    rows = con.execute(body).fetchall()
    check(
        "the detector marks exactly the cross-format twin, loser superseded by winner",
        len(rows) == 1 and rows[0][0] == "bill-pdf" and rows[0][1] == "bill-hwp",
        rows,
    )
    check(
        "the marked pair clears the committed threshold",
        bool(rows) and rows[0][2] >= TWIN_JACCARD_T,
        rows,
    )


def test_binary_extraction() -> None:
    """HWP and HWPX ingestion, and the guards that keep the raw zone clean."""
    print("binary extraction")

    # Exact round-trip through the HWPX reader, including a paragraph split
    # across two text runs, which is how Hancom writes styled text.
    hwpx = _synthetic_hwpx([["제3조(가상자산사업자의 ", "신고)"], ["신고 의무를 정한다."]])
    try:
        text = extract_text("sample.hwpx", hwpx)
    except Exception as error:  # noqa: BLE001 - reported as a check failure
        text = f"<raised {type(error).__name__}: {error}>"
    check(
        "HWPX text runs join into paragraphs exactly",
        text == "제3조(가상자산사업자의 신고)\n신고 의무를 정한다.",
        repr(text),
    )

    # An empty document must raise rather than index as a blank chunk.
    try:
        extract_text("empty.hwpx", _synthetic_hwpx([[""]]))
        check("an empty HWPX raises instead of indexing blank text", False, "no error raised")
    except ExtractionEmpty:
        check("an empty HWPX raises instead of indexing blank text", True)
    except Exception as error:  # noqa: BLE001
        check(
            "an empty HWPX raises instead of indexing blank text",
            False,
            f"{type(error).__name__}: {error}",
        )

    # A non-zip must be named, not silently swallowed.
    try:
        extract_text("bogus.hwpx", b"not a zip at all")
        check("a corrupt HWPX raises with the filename", False, "no error raised")
    except ExtractionCorrupt as error:
        check("a corrupt HWPX raises with the filename", "bogus.hwpx" in str(error), str(error))

    # extract.py runs inside a Singer tap, whose stdout IS the record stream.
    source = (get_paths().root / "pipeline" / "extract.py").read_text(encoding="utf-8")
    check("the extractor never writes to stdout", "print(" not in source, "found a print call")

    # The suffix table must be the only place formats are declared.
    meltano = (get_paths().root / "meltano.yml").read_text(encoding="utf-8")
    check(
        "meltano.yml does not duplicate the suffix list",
        "file_extensions:\n" not in meltano,
        "file_extensions is configured in meltano.yml as well as in chunking.py",
    )
    check(
        "binary suffixes are registered for ingestion",
        BINARY_SUFFIXES <= SUPPORTED_SUFFIXES,
        sorted(SUPPORTED_SUFFIXES),
    )
    # chunking restates the list so it can stay importable without the extractor.
    # A format added to one copy and forgotten in the other would be accepted by
    # the tap and then have no reader, or have a reader the tap never reaches.
    check(
        "the two BINARY_SUFFIXES copies agree",
        extract_module.BINARY_SUFFIXES == BINARY_SUFFIXES,
        f"extract={sorted(extract_module.BINARY_SUFFIXES)} chunking={sorted(BINARY_SUFFIXES)}",
    )
    # Every registered suffix must reach a real extractor. Empty bytes are
    # rejected by all of them, so what is checked is WHICH rejection: the
    # dispatch fallthrough means the suffix was registered and then never wired.
    unwired = []
    for suffix in sorted(BINARY_SUFFIXES):
        try:
            extract_text(f"probe{suffix}", b"")
        except Exception as error:  # noqa: BLE001 - reported as a check failure
            if "no extractor for" in str(error):
                unwired.append(suffix)
    check("every binary suffix reaches an extractor", not unwired, unwired)

    # Real HWP bills, when the user's corpus is present. Skipped on a fresh
    # clone so `make build` stays hermetic.
    bills = sorted(
        (get_paths().root.parent / "디지털자산관련법안원문").glob("*.hwp")
    )
    if not bills:
        print("       (no local HWP corpus; real-bill checks skipped)")
        return
    parsed = parse_document(bills[0].name, bills[0].read_bytes())
    check(
        "a real HWP bill extracts text",
        len(parsed.content) > 10000,
        f"{len(parsed.content)} characters",
    )
    # Headings carry a zone prefix once a document-type profile claims the file
    # (`본칙 · 제1조(목적)`), which is the whole point of zones: it keeps repealed
    # 신구조문대비표 text from sharing a heading with the live article.
    article_headings = sum(
        1 for chunk in parsed.chunks if chunk.heading.split(" · ")[-1].startswith("제")
    )
    check(
        "a real HWP bill sections on its 조문",
        article_headings > 20,
        f"{article_headings} article headings",
    )
    check(
        "a claimed bill carries zone-prefixed headings",
        any(" · " in chunk.heading for chunk in parsed.chunks)
        and any(chunk.heading.startswith("부칙") for chunk in parsed.chunks),
        sorted({c.heading.split(" · ")[0] for c in parsed.chunks if c.heading})[:5],
    )
    check(
        "extraction leaves no replacement characters",
        "�" not in parsed.content,
        f"{parsed.content.count(chr(0xFFFD))} replacement characters",
    )


def test_chunk_export() -> None:
    """The JSON export is a projection of gold, never an input to the index."""
    print("chunk export")

    paths = get_paths()
    directory = paths.processed / "chunks"
    # A tree that INSTALLED its index (`make fetch-index`) never ran a build, so
    # there is no export to find and its absence is a state rather than a fault.
    # The lake is the discriminator, because the export is a projection of it: no
    # lake, no build, no export. `data/processed` itself is NOT the discriminator
    # -- it is created empty by `Paths.ensure()`, which is exactly the wrong answer
    # this check first shipped with. Getting it wrong is not theoretical: `fetch`
    # runs this file as its acceptance test, so a hard failure here rolled back
    # every install on a fresh clone until a real clone was measured.
    if not paths.ducklake_catalog.exists():
        skip("data/processed/chunks exists", "no lake: this index was installed, not built here")
        return
    check("data/processed/chunks exists", directory.is_dir(), str(directory))
    if not directory.is_dir():
        return

    exported: dict = {}
    invalid: list = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = DocumentChunks.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - reported as a check failure
            invalid.append(f"{path.name}: {type(error).__name__}")
            continue
        for chunk in document.chunks:
            exported[chunk.chunk_id] = chunk
        if document.chunk_count != len(document.chunks):
            invalid.append(f"{path.name}: chunk_count {document.chunk_count} != {len(document.chunks)}")

    check("every exported file validates against the schema", not invalid, invalid[:3])

    # The decisive property: the export and the index are the SAME set, because
    # both are projections of lake.gold.chunks. If the export were an input to
    # the indexer instead, the two could drift and the blocking audits would no
    # longer gate what is served.
    connection = connect_index(paths.index_sqlite, read_only=True)
    try:
        indexed = {row[0] for row in connection.execute("SELECT chunk_id FROM chunks")}
    finally:
        connection.close()
    check(
        "exported chunk ids match the index exactly",
        set(exported) == indexed,
        f"index-only={len(indexed - set(exported))} export-only={len(set(exported) - indexed)}",
    )

    # No timestamp anywhere, or the data plane would be dirty after every build.
    source = (paths.root / "pipeline" / "export.py").read_text(encoding="utf-8")
    check(
        "the exporter records no wall-clock time",
        "datetime" not in source and "time.time" not in source,
        "export.py references a clock, so its output would change every run",
    )


def test_graph_query() -> None:
    print("graph_query")

    resolved = resolve_node("vector_store")
    check("resolve_node finds the vector_store node", bool(resolved), "no candidates")

    impact = graph_query("vector_store", direction="upstream", max_depth=3)
    reached = {hit["node_id"]: hit["depth"] for hit in impact}
    print(f"       upstream of vector_store: {reached}")
    check("search_core is affected at depth 1", reached.get("search_core") == 1, str(reached))
    check(
        "service_api is affected at depth 2 (multi-hop)",
        reached.get("service_api") == 2,
        str(reached),
    )

    shallow = graph_query("vector_store", direction="upstream", max_depth=1)
    shallow_ids = {hit["node_id"] for hit in shallow}
    check(
        "max_depth=1 excludes the two-hop node",
        "service_api" not in shallow_ids,
        str(shallow_ids),
    )

    downstream = graph_query("architecture-overview", direction="downstream", max_depth=3)
    downstream_reached = {hit["node_id"]: hit["depth"] for hit in downstream}
    print(f"       downstream of architecture-overview: {downstream_reached}")
    check(
        "document dependency chain traverses two hops",
        downstream_reached.get("chunking-rules") == 2,
        str(downstream_reached),
    )

    # Assert the property, not an exact node set: dropping another importer of
    # vector_store into the inbox must not fail this test.
    filtered = graph_query(
        "vector_store", direction="upstream", max_depth=3, relations=["imports"]
    )
    filtered_ids = {hit["node_id"] for hit in filtered}
    unfiltered_ids = set(reached)
    check(
        "relation filter keeps the known imports chain",
        {"search_core", "service_api"} <= filtered_ids,
        str(sorted(filtered_ids)),
    )
    check(
        "relation filter never widens the result",
        filtered_ids <= unfiltered_ids,
        str(sorted(filtered_ids - unfiltered_ids)),
    )
    check(
        "every filtered hit was reached over an imports edge",
        all(hit["via_relations"] == "imports" for hit in filtered),
        str([(hit["node_id"], hit["via_relations"]) for hit in filtered]),
    )

    isolated = graph_query("no_such_node_anywhere", direction="upstream", max_depth=3)
    check("unknown start node returns no rows rather than raising", isolated == [], str(isolated))


def test_graph_rag() -> None:
    """graph_rag_search: augment, never re-rank, with honest edge provenance.

    Every assertion is a property, not a corpus pin: the fixture docstrings
    name each other, so "reachable only through the graph" is not a claim the
    fixtures can support — what is asserted instead is that the machinery
    produces real edges as provenance and never disturbs the direct ranking.
    """
    print("graph_rag")
    from pipeline.graph_rag import graph_rag_search

    # 1. The direct section is hybrid_search, bit-identical, at every depth.
    #    This is the core guarantee of the augment-don't-re-rank design: the
    #    graph arm may add context, it must never reorder or displace a hit.
    plain = hybrid_search("hybrid_search", limit=5)
    for depth in (0, 1, 2):
        payload = graph_rag_search(
            "hybrid_search", limit=5, depth=depth, include_fixtures=True
        )
        check(
            f"depth={depth} keeps the direct section bit-identical to hybrid_search",
            [hit["chunk_id"] for hit in payload["results"]]
            == [hit["chunk_id"] for hit in plain],
            f"{[h['chunk_id'] for h in payload['results']]} vs {[h['chunk_id'] for h in plain]}",
        )
    check(
        "depth=0 returns no related section",
        graph_rag_search("hybrid_search", limit=5, depth=0, include_fixtures=True)["related"] == [],
    )

    # 2. Direct doc<->doc edges surface as related context. Seeding on the
    #    definition of hybrid_search (search_core, asserted rank 1 above) must
    #    relate vector_store (search_core imports it) and service_api (imports
    #    search_core) through `imports` edges, both directions of the chain.
    payload = graph_rag_search(
        "hybrid_search", limit=5, seed_limit=1, depth=1, max_related_docs=8, include_fixtures=True
    )
    related_docs = {row["doc_id"] for row in payload["related"]}
    check(
        "an imports edge relates the imported module (vector_store)",
        "vector_store" in related_docs,
        str(sorted(related_docs)),
    )
    check(
        "an imports edge relates the importer (service_api)",
        "service_api" in related_docs,
        str(sorted(related_docs)),
    )
    check(
        "seed documents never appear in their own related section",
        not {row["doc_id"] for row in payload["related"]} & set(payload["seed_docs"]),
        f"seeds={payload['seed_docs']}",
    )

    # 3. Provenance is real: every link on every related row must exist in
    #    `edges` with the stated relation, and every related chunk must be a
    #    representative row of the retrieval surface (a chunk_id the vector and
    #    keyword halves actually index).
    connection = connect_index(get_paths().index_sqlite, read_only=True)
    try:
        edge_set = {
            (row[0], row[1], row[2])
            for row in connection.execute("SELECT source_id, target_id, relation FROM edges")
        }
        surface_ids = {row[0] for row in connection.execute("SELECT chunk_id FROM chunks_fts")}
    finally:
        connection.close()
    dangling = []
    off_surface = []
    for row in payload["related"]:
        if row["chunk_id"] not in surface_ids:
            off_surface.append(row["chunk_id"])
        for link in row["connection"]["links"]:
            if link["kind"] == "direct":
                pair_ok = (
                    (link["seed_doc"], row["doc_id"], link["relation"]) in edge_set
                    or (row["doc_id"], link["seed_doc"], link["relation"]) in edge_set
                )
            else:
                seed_side = (
                    (link["seed_doc"], link["shared_node"], link["relations"][0]) in edge_set
                    or (link["shared_node"], link["seed_doc"], link["relations"][0]) in edge_set
                )
                partner_side = (
                    (row["doc_id"], link["shared_node"], link["relations"][1]) in edge_set
                    or (link["shared_node"], row["doc_id"], link["relations"][1]) in edge_set
                )
                pair_ok = seed_side and partner_side
            if not pair_ok:
                dangling.append((row["doc_id"], link))
    check("every related link cites an edge that exists", not dangling, str(dangling[:2]))
    check("every related chunk is on the retrieval surface", not off_surface, str(off_surface[:5]))
    check(
        "every related row carries a selection tier",
        all(row["selection"] in {"query_match", "lexical_overlap", "doc_head"} for row in payload["related"]),
        str({row.get("selection") for row in payload["related"]}),
    )

    # 4. The DEFAULT read path hides fixtures from the related section exactly
    #    as it hides them from results. On a fixture-only build the seed search
    #    returns nothing and the guarantee holds vacuously.
    default_payload = graph_rag_search("architecture overview vector_store", limit=5, depth=1)
    leaked = sorted(
        {row["doc_id"] for row in default_payload["related"]} & _fixture_doc_ids()
    )
    check("default related section hides smoke fixtures", not leaked, str(leaked))

    # 5. Determinism: the same call twice returns the identical payload, which
    #    is what makes the tool safe to cache and to diff.
    import json as _json

    again = graph_rag_search(
        "hybrid_search", limit=5, seed_limit=1, depth=1, include_fixtures=True
    )
    check(
        "two identical graph_rag calls return identical payloads",
        _json.dumps(payload, sort_keys=True, ensure_ascii=False)
        == _json.dumps(again, sort_keys=True, ensure_ascii=False),
    )

    # 6. graph_rag_search attaches the reachability diagnostic to expansion on
    #    the real read path (the synthetic test pins _discover_related; this pins
    #    the caller wiring the unit test cannot reach).
    check(
        "graph_rag_search attaches a reachability diagnostic to expansion",
        isinstance(payload["expansion"].get("reachability"), dict)
        and "reason" in payload["expansion"]["reachability"],
        str(payload["expansion"].get("reachability")),
    )


def test_reachability_diagnostics() -> None:
    """graph_rag reachability diagnostics: read-only, honest 'why not' reasons.

    Runs against a synthetic in-memory graph so the reason logic AND the
    isolation guarantee (diagnostics never change `related`) are pinned even on
    a fresh clone with no built index. Provenance guard: the payload carries
    only counts and pivot-node ids, never a document id.
    """
    print("reachability diagnostics")
    import json as _json
    import sqlite3 as _sqlite3

    from pipeline.graph_rag import _discover_related

    def graph(edges):
        conn = _sqlite3.connect(":memory:")
        conn.row_factory = _sqlite3.Row
        conn.executescript(
            "CREATE TABLE nodes (node_id TEXT PRIMARY KEY, label TEXT);"
            "CREATE TABLE edges (source_id TEXT, target_id TEXT, relation TEXT,"
            " weight REAL, evidence TEXT);"
        )
        names = set()
        for s, t, rel, w in edges:
            names.update((s, t))
            conn.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (s, t, rel, w, f"{s}->{t}"))
        conn.executemany("INSERT INTO nodes VALUES (?,?)", [(n, n) for n in sorted(names)])
        conn.commit()
        return conn

    docs = frozenset({"billA", "billB"})

    # A. co-citation: billA and billB both delegate_to statuteX (a non-document).
    conn = graph([("billA", "statuteX", "delegates_to", 1.0),
                  ("billB", "statuteX", "delegates_to", 1.0)])
    related, trunc, diag = _discover_related(conn, ["billA"], docs, 1, None, frozenset(), 8)
    check("co-citation reaches the sibling document", set(related) == {"billB"}, str(sorted(related)))
    check(
        "reachability reports the hit (1 usable partner, 1 pivot, no hub drop)",
        diag["reason"].startswith("1 related")
        and diag["usable_partner_docs"] == 1
        and diag["pivots_found"] == 1
        and diag["pivots_hub_dropped"] == [],
        str(diag),
    )
    # isolation: diagnostics on vs off leave related/truncated byte-identical.
    r_on, t_on, _ = _discover_related(conn, ["billA"], docs, 1, None, frozenset(), 8, collect_diagnostics=True)
    r_off, t_off, d_off = _discover_related(conn, ["billA"], docs, 1, None, frozenset(), 8, collect_diagnostics=False)
    check(
        "diagnostics are side-effect free (related/truncated identical on vs off)",
        r_on == r_off and t_on == t_off and d_off is None,
        "related or truncated diverged with diagnostics off",
    )
    conn.close()

    # B. hub drop: 27 docs cite statuteX -> raw degree 27 > HUB_DOC_CAP -> skipped.
    hub_edges = [("billA", "statuteX", "delegates_to", 1.0)]
    hub_docs = {"billA"}
    for i in range(26):
        node = f"doc{i:02d}"
        hub_edges.append((node, "statuteX", "delegates_to", 1.0))
        hub_docs.add(node)
    conn = graph(hub_edges)
    related, trunc, diag = _discover_related(conn, ["billA"], frozenset(hub_docs), 1, None, frozenset(), 8)
    check("a hub pivot yields no related docs", related == {}, str(sorted(related)))
    dropped = diag["pivots_hub_dropped"]
    check(
        "reachability names the hub-dropped pivot with its RAW degree (27)",
        len(dropped) == 1 and dropped[0]["node"] == "statuteX"
        and dropped[0]["degree"] == 27 and "hub cap" in diag["reason"],
        str(dropped) + " | " + diag["reason"],
    )
    conn.close()

    # C. mentions-only seed: the sole edge is suppressed by default.
    conn = graph([("billA", "statuteX", "mentions", 1.0)])
    related, trunc, diag = _discover_related(conn, ["billA"], docs, 1, None, frozenset(), 8)
    check("mentions-only seed reaches nothing", related == {}, str(related))
    check(
        "reachability blames suppressed (mentions) edges",
        diag["seed_edges"] == {"total": 1, "usable": 0} and "suppressed" in diag["reason"],
        str(diag),
    )
    conn.close()

    # D. the only co-citation partner is itself a seed -> excluded, reported.
    conn = graph([("billA", "statuteX", "delegates_to", 1.0),
                  ("billB", "statuteX", "delegates_to", 1.0)])
    related, trunc, diag = _discover_related(conn, ["billA", "billB"], docs, 1, None, frozenset(), 8)
    check("a partner that is a seed is not surfaced", related == {}, str(related))
    check(
        "reachability reports the excluded seed partner",
        diag["partners_excluded"]["already_seed"] >= 1 and "already a seed" in diag["reason"],
        str(diag),
    )
    conn.close()

    # E. provenance/fixture guard: no document id ever appears in the payload,
    #    and the hidden exclusion IS surfaced as a count (positive half).
    conn = graph([("billA", "statuteX", "delegates_to", 1.0),
                  ("billB", "statuteX", "delegates_to", 1.0)])
    _, _, diag = _discover_related(conn, ["billA"], docs, 1, None, frozenset({"billB"}), 8)
    blob = _json.dumps(diag, ensure_ascii=False)
    check(
        "reachability never emits a document id (counts + pivot ids only)",
        "billA" not in blob and "billB" not in blob and "doc00" not in blob,
        blob,
    )
    check(
        "a hidden co-citation partner is surfaced as a count",
        diag["partners_excluded"]["hidden"] == 1,
        str(diag["partners_excluded"]),
    )
    conn.close()

    # F. a pivot cited ONLY by the seed is NOT a shared pivot: the seed's own
    #    citation must not be miscounted as an excluded sibling, and the reason
    #    must name the honest cause (no co-citation sibling).
    conn = graph([("billA", "statuteX", "delegates_to", 1.0)])
    related, _, diag = _discover_related(conn, ["billA"], docs, 1, None, frozenset(), 8)
    check("a self-cited pivot yields no related docs", related == {}, str(related))
    check(
        "the seed's own citation is not miscounted as an excluded partner",
        diag["partners_excluded"]["already_seed"] == 0,
        str(diag["partners_excluded"]),
    )
    check(
        "reachability reports the honest cause (no co-citation sibling)",
        "no co-citation sibling" in diag["reason"],
        diag["reason"],
    )
    conn.close()

    # G. two hub pivots (inserted Z-before-A): the hub-drop list is emitted
    #    sorted by node id, and a populated hub_dropped list leaks no doc id.
    g_edges = []
    g_docs = {"billA"}
    for pivot in ("statZ", "statA"):
        g_edges.append(("billA", pivot, "delegates_to", 1.0))
        for i in range(26):
            node = f"doc{pivot}{i:02d}"
            g_edges.append((node, pivot, "delegates_to", 1.0))
            g_docs.add(node)
    conn = graph(g_edges)
    _, _, diag = _discover_related(conn, ["billA"], frozenset(g_docs), 1, None, frozenset(), 8)
    order = [p["node"] for p in diag["pivots_hub_dropped"]]
    check("hub-dropped pivots are emitted sorted by node id", order == ["statA", "statZ"], str(order))
    blob = _json.dumps(diag, ensure_ascii=False)
    check(
        "a populated hub_dropped list leaks no document id",
        "billA" not in blob and "docstatA00" not in blob and "docstatZ00" not in blob,
        blob,
    )
    conn.close()


def test_reranker() -> None:
    """The opt-in cross-encoder reranker: off by default, offline fail-loud,
    deterministic when on, and it never disturbs the default read path.

    Model-gated: the reranking assertions are skipped when the reranker model is
    not in the local HF cache, so `make smoke` stays hermetic on a fresh clone.
    Run `make warm-rerank` once to exercise them. The fail-loud assertion is
    model-independent and always runs.
    """
    print("reranker")
    from pipeline import get_settings
    from pipeline.reranker import OnnxReranker, get_reranker

    settings = get_settings()

    # 1. Offline fail-loud (model-independent): a missing model raises with an
    #    actionable message, never a silent network download (invariant 7).
    try:
        OnnxReranker("bogus/nonexistent-reranker-xyz", allow_download=False)
        check("reranker read path raises on a missing model", False, "no error raised")
    except RuntimeError as error:
        check(
            "reranker read path raises on a missing model (no network)",
            "warm-rerank" in str(error) and "invariant 7" in str(error),
            str(error)[:80],
        )

    try:
        get_reranker(settings, allow_download=False)
    except Exception:
        print("       (reranker model not cached; run `make warm-rerank`. reranking checks skipped)")
        return

    query = "hybrid_search"

    # 2. rerank=False is the clean fused core -- no rerank annotation leaks onto
    #    it. (The default read path follows .env's RERANK; smoke pins the core via
    #    the rerank=False wrapper above, so it is stable whether RERANK is on or off.)
    off = hybrid_search(query, limit=5, rerank=False)
    check("rerank=False path carries no rerank annotation", all("reranked" not in hit for hit in off))

    reranked = hybrid_search(query, limit=5, rerank=True)
    check("rerank returns `limit` rows", len(reranked) == 5, len(reranked))
    check(
        "every reranked row carries CE provenance",
        all(
            row.get("reranked") and "ce_score" in row and "ce_rank" in row and "retrieval_rank" in row
            for row in reranked
        ),
        "missing rerank annotation",
    )

    # 3. Deterministic across runs (single-thread + logit rounding + chunk_id
    #    tie-break), because the reranker sits on the query-answer path.
    again = [row["chunk_id"] for row in hybrid_search(query, limit=5, rerank=True)]
    check("rerank is deterministic across runs", again == [row["chunk_id"] for row in reranked])


def test_batch_and_ghost_index(paths) -> None:
    """The batch entry point's contract, and the guard against a deleted index."""
    import os
    import shutil
    import subprocess
    import sys
    import tempfile

    from pipeline.build_rag import assert_index_present, connect_index

    print("batch queries / ghost index")

    # A held connection survives its file being unlinked and keeps answering from
    # the deleted copy -- and assert_index_current cannot see it, because the
    # signature it reads comes from that same dead inode.
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "index.sqlite"
        shutil.copy(paths.index_sqlite, copy)
        connection = connect_index(copy, read_only=True)
        try:
            before = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            assert_index_present(connection)
            check("the guard passes while the index is where it was opened", True)
            os.remove(copy)
            import sqlite3 as _sqlite3

            replacement = _sqlite3.connect(copy)
            replacement.execute("CREATE TABLE chunks (x)")
            replacement.commit()
            replacement.close()
            still = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            check(
                "a held connection really does keep serving the deleted copy",
                still == before,
                f"{before} -> {still}",
            )
            try:
                assert_index_present(connection)
                check("the guard refuses a replaced index", False, "no error raised")
            except ValueError as error:
                check("the guard refuses a replaced index", "REPLACED" in str(error) or "DELETED" in str(error))
        finally:
            connection.close()

    # The batch entry point must emit exactly one line per query, and each line
    # must be what `jq -c` of the single-query form produces.
    with tempfile.TemporaryDirectory() as directory:
        query_file = Path(directory) / "q.txt"
        query_file.write_text("# a comment\n전매제한\n\n예치금\n", encoding="utf-8")
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "agent.tools.hybrid_search", "--queries-from", str(query_file)],
            capture_output=True, text=True, check=False, cwd=paths.root,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        check("comments and blank lines are not queries", len(lines) == 2, f"{len(lines)} line(s)")
        check("the batch run exits 0", completed.returncode == 0, completed.stderr[-200:])
        one = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "agent.tools.hybrid_search", "전매제한"],
            capture_output=True, text=True, check=False, cwd=paths.root,
        )
        import json as _json

        check(
            "a batch line carries the same payload as the one-shot form",
            lines and _json.loads(lines[0]) == _json.loads(one.stdout),
        )
        empty = Path(directory) / "empty.txt"
        empty.write_text("# nothing but a comment\n", encoding="utf-8")
        refused = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "agent.tools.hybrid_search", "--queries-from", str(empty)],
            capture_output=True, text=True, check=False, cwd=paths.root,
        )
        check("a query file with no queries is an error", refused.returncode != 0)


def test_runtime_and_cache(paths) -> None:
    """The accelerator layer's two invariants: it degrades, and it does not lie.

    Neither needs a GPU to check, which is the point -- these are the properties a
    CPU-only CI runner on any of the three platforms can hold the design to.
    """
    import sqlite3

    from pipeline import runtime
    from pipeline.build_rag import get_embedder, resolve_precision
    from pipeline.vector_cache import (
        SAMPLE_MIN_COSINE,
        SignatureMismatch,
        verify_sample,
    )

    print("runtime / vector cache")

    # 1. A provider that is not registered must fail LOUDLY at selection, never
    #    silently produce vectors from somewhere else.
    import os as _os

    previous = _os.environ.get("ORT_PROVIDER")
    _os.environ["ORT_PROVIDER"] = "DefinitelyNotAnExecutionProvider"
    runtime.detect.cache_clear()
    try:
        runtime.detect("int8")
        check("an unregistered ORT_PROVIDER is rejected", False, "no error raised")
    except RuntimeError as error:
        check("an unregistered ORT_PROVIDER is rejected", "not registered" in str(error))
    finally:
        if previous is None:
            _os.environ.pop("ORT_PROVIDER", None)
        else:
            _os.environ["ORT_PROVIDER"] = previous
        runtime.detect.cache_clear()

    # 2. The default asset must keep the pipeline on the CPU even where a GPU
    #    exists: int8 on a GPU EP was MEASURED slower than one CPU thread, so
    #    "a GPU appeared" must not silently change what the build produces.
    profile = runtime.detect("int8")
    check(
        "the int8 asset stays on the CPU even when a GPU provider is registered",
        profile.provider == runtime.CPU_EP,
        profile.describe(),
    )

    # 3. A synced device profile from another machine must never be trusted.
    import json as _json

    stale = paths.processed / "device_profile.smoke.json"
    with runtime.profile_writes_allowed():
        runtime.save_profile(stale, profile)
    payload = _json.loads(stale.read_text(encoding="utf-8"))
    payload["fingerprint"] = "some other machine"
    stale.write_text(_json.dumps(payload), encoding="utf-8")
    check("a device profile from another machine is ignored", runtime.load_profile(stale) is None)
    stale.unlink(missing_ok=True)

    # 4. The vector cache must hand back what the model would have produced. A
    #    poisoned cache does not raise -- it degrades retrieval quietly -- so the
    #    guard is a re-encode of a sample, not a schema check.
    if not paths.vector_cache.exists():
        skip("vector cache sample re-encodes to the same passage", "no cache on this machine yet")
        return
    settings = get_settings()
    if settings.embedding_provider == "hashing":
        # The hashing provider is pure Python and re-encoding it proves nothing
        # about the ONNX path, but it is also the default, so skip rather than
        # give a green tick that means nothing.
        skip(
            "vector cache sample re-encodes to the same passage",
            "EMBEDDING_PROVIDER=hashing is pure Python; re-encoding it proves nothing about the ONNX path",
        )
        return
    connection = sqlite3.connect(str(paths.vector_cache))
    try:
        stored = connection.execute("SELECT count(*) FROM vectors").fetchone()[0]
    finally:
        connection.close()
    if not stored:
        skip("vector cache sample re-encodes to the same passage", "the cache holds no vectors")
        return
    embedder = get_embedder(settings)
    # embed_text, NOT content: those differ (heading prefix) and the cache is keyed
    # on the string that is actually encoded. Sampling `content` finds nothing in
    # the cache and turns this whole check into a vacuous pass -- which is exactly
    # what it did on the first run.
    from pipeline.build_rag import open_lake

    lake = open_lake(paths)
    try:
        texts = [
            row[0]
            for row in lake.execute(
                "SELECT embed_text FROM lake.gold.chunks ORDER BY chunk_id LIMIT 200"
            ).fetchall()
        ]
    finally:
        lake.close()
    try:
        checked, worst = verify_sample(embedder, paths.vector_cache, embedder.dimensions, texts)
    except SignatureMismatch as error:
        # Reported, not swallowed, and the cache survives to be inspected.
        check("the vector cache belongs to this embedder", False, str(error))
        return
    check(
        f"vector cache sample re-encodes to the same passage ({checked} sampled, "
        f"precision={resolve_precision(settings)}, floor {SAMPLE_MIN_COSINE})",
        checked > 0 and worst >= SAMPLE_MIN_COSINE,
        f"worst cosine {worst:.6f}"
        + ("; nothing sampled -- the cache and the sample disagree about the key" if not checked else ""),
    )


def _connect_index(paths):
    from pipeline.build_rag import connect_index

    return connect_index(paths.index_sqlite, read_only=True)


def main() -> int:
    paths = get_paths()
    settings = get_settings()
    print(f"smoke test: node_role={settings.node_role} provider={settings.embedding_provider}")
    print()

    try:
        test_index_present(paths)
        print()
        test_hybrid_search()
        print()
        test_korean_retrieval()
        print()
        test_alias_expansion()
        print()
        test_retrieval_internals()
        print()
        test_chunking()
        print()
        test_binary_extraction()
        test_ooxml_extraction()
        test_format_ladder()
        test_twin_detector()
        print()
        test_chunk_export()
        print()
        test_graph_query()
        print()
        test_graph_rag()
        print()
        test_reachability_diagnostics()
        print()
        test_reranker()
        print()
        test_runtime_and_cache(paths)
        print()
        test_batch_and_ghost_index(paths)
    except Exception:
        traceback.print_exc()
        return 1

    print()
    if SKIPS:
        print(f"skipped ({len(SKIPS)}):")
        for skipped in SKIPS:
            print(f"  - {skipped}")
        print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
