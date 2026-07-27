"""Graph-augmented hybrid retrieval: hybrid_search plus graph-related context.

`graph_rag_search` returns two sections and deliberately does NOT fuse them:

  results   exactly what `hybrid_search` returns, bit-identical. The direct
            arms (vector + keyword, RRF) already carry the content evidence.
  related   passages from documents the graph connects to the seed documents,
            each row carrying the actual edges that justify its presence.

They stay separate because the graph arm is not independent evidence about the
same question — it is a doc-level filter over the same retrieval — so summing
the two into one RRF list double-counts whatever the direct arm already found
and can only reorder its head below its tail (measured during design review:
with rrf_k=60 a graph-only chunk can never displace a direct top-8 at any
weight below ~0.9). Augmenting instead of re-ranking keeps the precision the
eval floor protects and makes the graph contribution inspectable.

Documents relate in two ways:

  direct       an edge whose both endpoints are indexed documents (imports,
               front-matter depends_on, wiki links).
  co_citation  both documents assert an edge onto the same non-document node
               with the same orientation (seed -> X <- other, seed <- X -> other).
               This is the only bill-to-bill connectivity that exists: bills
               never edge to each other, they edge to shared statute nodes
               (amends / delegates_to), so the walk down-then-up is done here
               as one join. `nodes.doc_id` is never used for it — for a
               referenced node that column is MIN() over asserting documents,
               an attribution artifact, not provenance.

Hub nodes are damped: a statute half the corpus delegates to relates everything
to everything and means nothing. Nodes connected to more than HUB_DOC_CAP
documents are skipped; the rest weigh in at 1/log2(2+degree).

When the related section comes back empty or short, `expansion.reachability`
says why in read-only terms — no usable seed edges, every shared pivot over the
hub cap (each named with its raw degree), partners that were themselves seeds or
hidden — so an unreachable answer is inspectable instead of silent. It carries
counts and pivot-node ids only, never a document id and never nodes.doc_id, and
it is derived from the walk's own structures without influencing `related`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence

from pipeline import Paths, Settings, get_paths, get_settings
from pipeline.aliases import expand_query
from pipeline.build_rag import (
    COLLECTION_CANDIDATES,
    _features,
    _fixture_doc_ids,
    _search_once,
    connect_index,
    hybrid_search,
)

DEPTH_CEILING = 2
DEFAULT_MAX_RELATED_DOCS = 5
PER_DOC_CHUNKS = 2
HUB_DOC_CAP = 24
# `mentions` is the weakest assertion in the system (a backticked identifier
# anywhere in prose) and dominates edge counts; as a co-citation carrier it
# relates near-everything to near-everything, so it is out by default.
DEFAULT_EXCLUDED_RELATIONS = frozenset({"mentions"})
# Evidence snippets joined into the connective query variant are capped so one
# verbose citation cannot drown the original question's terms.
MAX_CONNECTIVE_VARIANTS = 4


def _require_graph(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not {"nodes", "edges"} <= tables:
        raise RuntimeError(
            "the graph half of data/serving/index.sqlite is missing "
            f"(tables present: {sorted(tables)}). Rebuild it with `make build`."
        )


def _document_ids(connection: sqlite3.Connection) -> frozenset:
    """doc_ids that actually own chunks — the only endpoints worth surfacing.

    Kind labels on nodes are not used for this: what makes a neighbour useful
    is that its passages are retrievable, and `chunks` is the ground truth for
    that regardless of how the entity extractor classified the node.
    """
    return frozenset(
        row[0] for row in connection.execute("SELECT DISTINCT doc_id FROM chunks")
    )


def _edges_touching(connection: sqlite3.Connection, node_ids: Sequence[str]) -> list:
    rows = connection.execute(
        """
        SELECT source_id, target_id, relation, weight, evidence
        FROM edges
        WHERE source_id IN (SELECT value FROM json_each(:ids))
           OR target_id IN (SELECT value FROM json_each(:ids))
        ORDER BY source_id, target_id, relation
        """,
        {"ids": json.dumps(sorted(set(node_ids)))},
    ).fetchall()
    return [dict(row) for row in rows]


def _node_labels(connection: sqlite3.Connection, node_ids: Sequence[str]) -> dict:
    if not node_ids:
        return {}
    rows = connection.execute(
        "SELECT node_id, label FROM nodes "
        "WHERE node_id IN (SELECT value FROM json_each(:ids))",
        {"ids": json.dumps(sorted(set(node_ids)))},
    ).fetchall()
    return {row["node_id"]: row["label"] or row["node_id"] for row in rows}


def _reachability_summary(
    *,
    seeds: int,
    seed_edges_total: int,
    seed_edges_usable: int,
    direct_neighbours: int,
    pivots_found: int,
    hub_dropped: list,
    usable_partners: int,
    excluded_seed: int,
    excluded_hidden: int,
    related_found: int,
    truncated: bool,
) -> dict:
    """Explain, read-only, why the related set came out the size it did.

    Turns a silent empty `related` into an inspectable reason: the graph only
    knows declared edges, co-citation needs a shared pivot, hubs are capped, and
    the walk starts only from the retrieval seeds. Every input is a count or a
    pivot-node identity (never a document id, never nodes.doc_id), so this never
    re-surfaces a hidden fixture nor leaks the MIN()-attributed doc_id column.
    """
    if related_found > 0:
        reason = f"{related_found} related document(s) found"
        if truncated:
            reason += "; more candidates than the cap were dropped (truncated)"
    elif seeds == 0:
        reason = "retrieval returned no seed documents"
    elif seed_edges_usable == 0:
        reason = (
            f"seed documents have {seed_edges_total} edge(s), all suppressed as "
            "mentions/excluded relations"
            if seed_edges_total
            else "seed documents have no graph edges (the relation may be prose-only)"
        )
    elif pivots_found == 0 and direct_neighbours == 0:
        reason = "seed edges reach no shared pivot or indexed neighbour"
    else:
        # Edges exist but nothing became related. Name EVERY blocker present so a
        # genuine hub drop is never masked by a trivial self-/sibling exclusion.
        parts = []
        if hub_dropped:
            parts.append(
                f"{len(hub_dropped)} shared pivot(s) over the hub cap ({HUB_DOC_CAP} docs)"
            )
        if excluded_seed or excluded_hidden:
            bits = []
            if excluded_seed:
                bits.append(f"{excluded_seed} already a seed")
            if excluded_hidden:
                bits.append(f"{excluded_hidden} hidden")
            parts.append("co-citation partner(s) excluded (" + ", ".join(bits) + ")")
        if (
            pivots_found - len(hub_dropped) > 0
            and usable_partners == 0
            and not (excluded_seed or excluded_hidden)
        ):
            parts.append(
                "shared pivot(s) cited by no other indexed document (no co-citation sibling)"
            )
        reason = (
            "no related documents: " + "; ".join(parts)
            if parts
            else "no admissible related documents"
        )

    return {
        "reason": reason,
        "seeds": seeds,
        "seed_edges": {"total": seed_edges_total, "usable": seed_edges_usable},
        "direct_neighbours": direct_neighbours,
        "pivots_found": pivots_found,
        "pivots_hub_dropped": hub_dropped,
        "usable_partner_docs": usable_partners,
        "partners_excluded": {"already_seed": excluded_seed, "hidden": excluded_hidden},
        "related_found": related_found,
        "truncated": truncated,
    }


def _discover_related(
    connection: sqlite3.Connection,
    seed_docs: Sequence[str],
    documents: frozenset,
    depth: int,
    relations: Sequence[str] | None,
    hidden_docs: frozenset,
    max_related_docs: int,
    collect_diagnostics: bool = True,
) -> tuple:
    """Find related documents with honest provenance.

    Returns (related, truncated, reachability): related is a dict
    doc_id -> {"score": float, "connections": [rows]} ordered later by the
    caller; truncated reports whether the doc cap dropped candidates;
    reachability is a read-only diagnostic (or None when collect_diagnostics is
    False) explaining WHY the related set is empty/small. It is derived from the
    same structures the walk uses and never influences `related` — an equality
    test pins related/truncated identical with diagnostics on and off.
    """
    seed_set = set(seed_docs)
    # Read-only diagnostic accumulators. Every write below targets one of these
    # and never `related`/`add`, so the related output is byte-identical whether
    # or not diagnostics are collected.
    _direct_docs: set = set()
    _hub_dropped: dict = {}
    _usable_partners: set = set()
    _excluded_seed: set = set()
    _excluded_hidden: set = set()
    allowed = (lambda r: r not in DEFAULT_EXCLUDED_RELATIONS) if relations is None else (
        lambda r: r in set(relations)
    )

    def admissible(doc_id: str) -> bool:
        return doc_id in documents and doc_id not in seed_set and doc_id not in hidden_docs

    related: dict = {}

    def add(doc_id: str, score: float, connection_row: dict) -> None:
        entry = related.setdefault(doc_id, {"score": 0.0, "connections": []})
        entry["score"] += score
        entry["connections"].append(connection_row)

    # -- depth 1: direct doc<->doc edges, plus co-citation through shared nodes.
    _all_seed_edges = _edges_touching(connection, seed_docs)
    seed_edges = [edge for edge in _all_seed_edges if allowed(edge["relation"])]

    intermediates: dict = {}
    for edge in seed_edges:
        if edge["source_id"] in seed_set:
            other, direction = edge["target_id"], "outgoing"
        else:
            other, direction = edge["source_id"], "incoming"
        if other in documents:
            if admissible(other):
                _direct_docs.add(other)
                add(
                    other,
                    edge["weight"],
                    {
                        "kind": "direct",
                        "relation": edge["relation"],
                        "direction": direction,
                        "seed_doc": edge["source_id"] if direction == "outgoing" else edge["target_id"],
                        "evidence": edge["evidence"],
                        "depth": 1,
                    },
                )
        elif other not in seed_set:
            # Non-document endpoint: a co-citation pivot candidate. Orientation
            # matters: seed->X pairs with other->X, X->seed with X->other.
            intermediates.setdefault((other, direction), []).append(edge)

    pivot_ids = sorted({node for (node, _direction) in intermediates})
    pivot_edges = [edge for edge in _edges_touching(connection, pivot_ids) if allowed(edge["relation"])] if pivot_ids else []
    labels = _node_labels(connection, pivot_ids)

    # Documents connected to each pivot with each orientation, for the hub gauge
    # and the pairing step. Direction is named from the pivot's point of view.
    pivot_docs: dict = {}
    for edge in pivot_edges:
        if edge["target_id"] in labels and edge["source_id"] in documents:
            pivot_docs.setdefault((edge["target_id"], "cited_by"), []).append(edge)
        if edge["source_id"] in labels and edge["target_id"] in documents:
            pivot_docs.setdefault((edge["source_id"], "cites"), []).append(edge)

    for (pivot, seed_direction), seed_side in sorted(intermediates.items()):
        # seed ->X (outgoing) pairs with other docs citing X; X-> seed pairs
        # with other docs X cites.
        pair_key = (pivot, "cited_by" if seed_direction == "outgoing" else "cites")
        partners = pivot_docs.get(pair_key, [])
        # The seeds that reached THIS pivot: a seed cites its own pivot too, so it
        # appears among the pivot's partners as a self-reference, not a sibling.
        pivot_seeds = {
            e["source_id"] if seed_direction == "outgoing" else e["target_id"]
            for e in seed_side
        }
        degree = len({e["source_id"] if pair_key[1] == "cited_by" else e["target_id"] for e in partners})
        if degree > HUB_DOC_CAP:
            _hub_dropped.setdefault(
                pivot, {"node": pivot, "label": labels.get(pivot, pivot), "degree": degree}
            )
            continue
        damping = 1.0 / math.log2(2 + degree)
        for partner_edge in partners:
            other = (
                partner_edge["source_id"]
                if pair_key[1] == "cited_by"
                else partner_edge["target_id"]
            )
            if not admissible(other):
                # Attribute the exclusion reason directly (not via the collapsed
                # admissible() bool). Count a seed partner as a LOST sibling only
                # when a DIFFERENT seed reached this pivot; a seed pairing only
                # with itself is no shared pivot, so recording it would misreport
                # "reach no shared pivot" as an exclusion.
                if other in seed_set:
                    if pivot_seeds - {other}:
                        _excluded_seed.add(other)
                elif other in hidden_docs:
                    _excluded_hidden.add(other)
                continue
            _usable_partners.add(other)
            for seed_edge in seed_side:
                seed_doc = (
                    seed_edge["source_id"]
                    if seed_direction == "outgoing"
                    else seed_edge["target_id"]
                )
                add(
                    other,
                    min(seed_edge["weight"], partner_edge["weight"]) * damping,
                    {
                        "kind": "co_citation",
                        "shared_node": pivot,
                        "shared_node_label": labels.get(pivot, pivot),
                        "relations": [seed_edge["relation"], partner_edge["relation"]],
                        "seed_doc": seed_doc,
                        "evidence": [seed_edge["evidence"], partner_edge["evidence"]],
                        "depth": 1,
                    },
                )

    # -- depth 2: one more hop over direct doc edges only. Co-citation already
    # is a two-edge pattern, so chaining it again would be depth 4 in disguise.
    if depth >= 2 and related:
        hop_docs = sorted(related)
        for edge in _edges_touching(connection, hop_docs):
            if not allowed(edge["relation"]):
                continue
            if edge["source_id"] in related and admissible(edge["target_id"]) and edge["target_id"] not in related:
                via = edge["source_id"]
                add(
                    edge["target_id"],
                    related[via]["score"] * edge["weight"] * 0.5,
                    {
                        "kind": "direct",
                        "relation": edge["relation"],
                        "direction": "outgoing",
                        "seed_doc": via,
                        "evidence": edge["evidence"],
                        "depth": 2,
                    },
                )
            elif edge["target_id"] in related and admissible(edge["source_id"]) and edge["source_id"] not in related:
                via = edge["target_id"]
                add(
                    edge["source_id"],
                    related[via]["score"] * edge["weight"] * 0.5,
                    {
                        "kind": "direct",
                        "relation": edge["relation"],
                        "direction": "incoming",
                        "seed_doc": via,
                        "evidence": edge["evidence"],
                        "depth": 2,
                    },
                )

    ordered = sorted(related.items(), key=lambda item: (-item[1]["score"], item[0]))
    truncated = len(ordered) > max_related_docs
    result = dict(ordered[:max_related_docs])

    reachability = None
    if collect_diagnostics:
        reachability = _reachability_summary(
            seeds=len(seed_set),
            seed_edges_total=len(_all_seed_edges),
            seed_edges_usable=len(seed_edges),
            direct_neighbours=len(_direct_docs),
            pivots_found=len(pivot_ids),
            hub_dropped=[_hub_dropped[node] for node in sorted(_hub_dropped)],
            usable_partners=len(_usable_partners),
            excluded_seed=len(_excluded_seed),
            excluded_hidden=len(_excluded_hidden),
            related_found=len(result),
            truncated=truncated,
        )
    return result, truncated, reachability


def _connective_variants(entry: dict) -> list:
    """Query variants carrying the connection itself, not the user's wording.

    The verbatim citation evidence (e.g. `「전자금융거래법」에 따른`) shares
    characters with whatever article in the neighbour cites the same statute,
    which is exactly the passage worth showing when the user's own vocabulary
    matches nothing in that document.
    """
    variants: list = []
    for connection_row in entry["connections"]:
        if connection_row["kind"] == "co_citation":
            for evidence in connection_row["evidence"]:
                if evidence and evidence not in variants:
                    variants.append(evidence)
            label = connection_row["shared_node_label"].replace("-", " ")
            if label and label not in variants:
                variants.append(label)
        elif connection_row.get("evidence") and connection_row["evidence"] not in variants:
            variants.append(connection_row["evidence"])
    return variants[:MAX_CONNECTIVE_VARIANTS]


def _representative_chunks(connection: sqlite3.Connection, doc_id: str) -> list:
    """The doc's rows that exist on the retrieval surface (chunks_fts members).

    Emitting any other chunk_id would name a row no retrieval surface indexes,
    and boilerplate deduplicated under another document's representative is
    exactly the content not worth surfacing as that document's context.
    """
    rows = connection.execute(
        """
        SELECT chunks.chunk_id, chunks.doc_id, chunks.rel_path, chunks.collection,
               chunks.title, chunks.heading, chunks.doc_type, chunks.chunk_index,
               chunks.content
        FROM chunks
        JOIN chunks_fts ON chunks_fts.chunk_id = chunks.chunk_id
        WHERE chunks.doc_id = :doc_id
        ORDER BY chunks.chunk_index
        """,
        {"doc_id": doc_id},
    ).fetchall()
    return [dict(row) for row in rows]


def _select_chunks(
    connection: sqlite3.Connection,
    settings: Settings,
    related: dict,
    query_variants: Sequence[str],
    per_doc_chunks: int,
    include_fixtures: bool,
) -> dict:
    """Pick the passages to show per related document, best evidence first.

    Three strictly ordered tiers, each labelled on the row:

    `query_match` — the user's own (alias-expanded) wording found it. Only the
    original query variants score this tier: the connective vocabulary must not
    compete here, or the shared statute's name drowns the article that actually
    answers the question whenever both exist in the same document (observed on
    the bill corpus: the 예치금 article lost to 금융위 boilerplate).

    `lexical_overlap` — the user's wording found nothing, so the CONNECTIVE
    vocabulary is tried: the citing evidence and the shared node's name as
    retrieval variants, then a deterministic token-overlap scan of the
    document's own representative chunks. This is the tier that reaches an
    article sharing no characters with the question.

    `doc_head` — nothing matched; the first representative chunk, shown only
    because the graph insists the document is related. Honest last resort.
    """
    doc_ids = set(related)
    per_variant_rows: dict = {}
    for variant in query_variants:
        rows = _search_once(
            variant,
            COLLECTION_CANDIDATES,
            COLLECTION_CANDIDATES,
            connection,
            settings,
            collection=None,
            include_fixtures=include_fixtures,
        )
        for rank, row in enumerate(rows, start=1):
            if row["doc_id"] in doc_ids:
                per_variant_rows.setdefault(row["doc_id"], {}).setdefault(
                    row["chunk_id"], {"row": row, "score": 0.0}
                )["score"] += 1.0 / (settings.rrf_k + rank)

    def _evidenced(bucket: dict, variants: Sequence[str]) -> dict:
        # KNN fills its k slots however distant the chunks are, so an in-window
        # row with no keyword match and no token overlap with the variants that
        # scored it is indistinguishable from filler. Demand token evidence.
        wanted: set = set()
        for variant in variants:
            wanted |= set(_features(variant))
        return {
            chunk_id: item
            for chunk_id, item in bucket.items()
            if item["row"]["keyword_rank"] is not None
            or wanted & set(_features(item["row"]["content"]))
        }

    def _top(bucket: dict, label: str) -> list:
        ordered = sorted(bucket.items(), key=lambda item: (-item[1]["score"], item[0]))
        return [
            {**item["row"], "selection": label}
            for _chunk_id, item in ordered[:per_doc_chunks]
        ]

    selected: dict = {}
    for doc_id, entry in related.items():
        # Tier 1: the user's own wording, and nothing else.
        candidates = _evidenced(per_variant_rows.get(doc_id, {}), query_variants)
        if candidates:
            selected[doc_id] = _top(candidates, "query_match")
            continue

        # Tier 2a: connective-vocabulary retrieval (citing evidence + shared
        # node name), scored on its own — a fallback, never a competitor.
        connective_variants = _connective_variants(entry)
        connective: dict = {}
        for variant in connective_variants:
            rows = _search_once(
                variant,
                COLLECTION_CANDIDATES,
                COLLECTION_CANDIDATES,
                connection,
                settings,
                collection=None,
                include_fixtures=include_fixtures,
            )
            for rank, row in enumerate(rows, start=1):
                if row["doc_id"] == doc_id:
                    connective.setdefault(
                        row["chunk_id"], {"row": row, "score": 0.0}
                    )["score"] += 1.0 / (settings.rrf_k + rank)
        connective = _evidenced(connective, connective_variants)
        if connective:
            selected[doc_id] = _top(connective, "lexical_overlap")
            continue

        # Tier 2b: deterministic token-overlap scan of the doc's own surface
        # rows against every variant, for what the candidate window missed.
        surface = _representative_chunks(connection, doc_id)
        wanted: set = set()
        for variant in list(query_variants) + connective_variants:
            wanted |= set(_features(variant))
        scored = []
        for row in surface:
            overlap = len(wanted & set(_features(row["content"])))
            if overlap > 0:
                scored.append((overlap, row))
        if scored:
            scored.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
            selected[doc_id] = [
                {**row, "selection": "lexical_overlap"}
                for _overlap, row in scored[:per_doc_chunks]
            ]
            continue

        # Tier 3: nothing matched any wording.
        if surface:
            selected[doc_id] = [{**surface[0], "selection": "doc_head"}]

    return selected


def graph_rag_search(
    query: str,
    limit: int = 5,
    candidates: int = 40,
    seed_limit: int = 5,
    depth: int | None = None,
    relations: Sequence[str] | None = None,
    max_related_docs: int = DEFAULT_MAX_RELATED_DOCS,
    per_doc_chunks: int = PER_DOC_CHUNKS,
    collection: str | None = None,
    include_fixtures: bool = False,
    expand: bool | None = None,
    connection: sqlite3.Connection | None = None,
    paths: Paths | None = None,
    settings: Settings | None = None,
) -> dict:
    """Hybrid retrieval plus graph-related context, in separate sections.

    `results` is exactly what `hybrid_search` returns — same rows, same order.
    `related` carries passages from graph-connected documents. The seed search
    honours `collection`; related discovery deliberately does not, because
    crossing collections is what the graph is for — a bill in one folder
    delegating to a statute another folder's filing also cites. Each related
    row says which edges put it there.

    depth: 0 skips the graph entirely, 1 (default) is direct edges plus
    co-citation, 2 adds one more hop over direct document edges.
    """
    paths = paths or get_paths()
    settings = settings or get_settings()
    depth = settings.graph_depth if depth is None else depth
    if not 0 <= depth <= DEPTH_CEILING:
        raise ValueError(f"depth must be between 0 and {DEPTH_CEILING}, got {depth}")

    owns_connection = connection is None
    if connection is None:
        if not paths.index_sqlite.exists():
            raise FileNotFoundError(
                f"{paths.index_sqlite} does not exist. Build it with `make build`."
            )
        connection = connect_index(paths.index_sqlite, read_only=True)

    try:
        results = hybrid_search(
            query,
            limit=max(limit, seed_limit),
            candidates=candidates,
            connection=connection,
            settings=settings,
            expand=expand,
            collection=collection,
            include_fixtures=include_fixtures,
        )
        seed_docs = list(dict.fromkeys(hit["doc_id"] for hit in results[:seed_limit]))
        results = results[:limit]

        payload = {
            "query": query,
            "results": results,
            "seed_docs": seed_docs,
            "related": [],
            "expansion": {"depth": depth, "truncated": False},
        }
        if depth == 0:
            payload["expansion"]["reachability"] = {
                "reason": "graph expansion disabled (depth=0)"
            }
            return payload
        if not seed_docs:
            payload["expansion"]["reachability"] = {
                "reason": "retrieval returned no seed documents",
                "seeds": 0,
            }
            return payload

        _require_graph(connection)
        documents = _document_ids(connection)
        hidden = frozenset() if include_fixtures else _fixture_doc_ids()

        related, truncated, reachability = _discover_related(
            connection,
            seed_docs,
            documents,
            depth,
            relations,
            hidden,
            max_related_docs,
        )
        payload["expansion"]["truncated"] = truncated
        payload["expansion"]["reachability"] = reachability
        if not related:
            return payload

        use_expansion = settings.alias_expansion if expand is None else expand
        query_variants = expand_query(query) if use_expansion else [query]
        chunks = _select_chunks(
            connection,
            settings,
            related,
            query_variants,
            per_doc_chunks,
            include_fixtures,
        )

        for doc_id, entry in related.items():
            for row in chunks.get(doc_id, []):
                payload["related"].append(
                    {
                        **row,
                        "connection": {
                            "score": round(entry["score"], 4),
                            "links": entry["connections"],
                        },
                    }
                )
        return payload
    finally:
        if owns_connection:
            connection.close()
