"""Per-document-type parsing profiles: the one place a new type is added.

A profile is DATA, not code. It declares four tables of compiled regexes —
REQUIRE, REJECT, MARKERS, EDGES — and the engine here turns them into the
section spans and relation tuples `pipeline.chunking` already consumes.

That distinction is the whole safety argument. A profile cannot do I/O, cannot
reach the network, cannot read the clock, and cannot construct a Chunk or assign
a chunk_id. Offset arithmetic, overlap windowing, CRLF normalisation and id
assignment stay in reviewed code, so every authored profile inherits idempotence
for free. Evidence strings are sliced out of the document by the engine at the
match span, so a profile physically cannot emit an evidence quote that is not
verbatim source text.

A model authors a profile once, offline, and it is reviewed and committed.
`make build` runs plain Python with no model in the loop. The naive version of
this pattern — generate a parser and trust it — was measured to LOSE to
per-document model extraction (EVAPORATE, VLDB 2024: -13.8 F1, with over 40% of
generated functions scoring under 25 F1). What turned it into a win was an
acceptance gate, which lives in `pipeline/doctypes/gate.py`. Author against the
gate, not against your own opinion of the regex.
"""

from __future__ import annotations

import importlib
import re
from typing import NamedTuple

# Adding a document type is one line here plus one module beside this file.
# Declaration order is the tiebreak when two profiles both claim a document; the
# gate reports any such overlap as an exclusivity failure.
PROFILE_MODULES = ("bill", "statute")

PROFILE_FIELDS = ("DOC_TYPE", "REQUIRE", "REJECT", "MARKERS", "EDGES")


class Marker(NamedTuple):
    """One structural boundary.

    role='zone'     partitions the document and prefixes every heading inside
                    it (`부칙 · 제1조(시행일)`). This is what stops 부칙 제1조
                    colliding with 본칙 제1조, and keeps repealed text in a
                    신구조문대비표 from sharing a heading with the live article.
    role='section'  an ordinary heading inside the current zone.
    once=True       take only the FIRST match. Zone labels recur inside quoted
                    material further down, and a later match would re-partition
                    the document at the wrong place.
    """

    role: str
    label: str
    pattern: re.Pattern
    once: bool = False


class EdgeRule(NamedTuple):
    """One relation template.

    `source` and `target` are format strings over the match's named groups plus
    `{doc_id}`. `evidence` is deliberately absent: the engine slices it from the
    document at the match span.
    """

    relation: str
    pattern: re.Pattern
    source: str
    source_kind: str
    target: str
    target_kind: str


class ProfileError(RuntimeError):
    """A registered profile does not satisfy the contract."""


def load_profiles() -> tuple:
    """Import and validate every registered profile, in declaration order."""
    profiles = []
    for name in PROFILE_MODULES:
        module = importlib.import_module(f"pipeline.doctypes.{name}")
        missing = [field for field in PROFILE_FIELDS if not hasattr(module, field)]
        if missing:
            raise ProfileError(f"profile {name!r} is missing {', '.join(missing)}")
        profiles.append(module)
    return tuple(profiles)


def classify(rel_path: str, text: str) -> object | None:
    """Return the profile that claims this document, or None.

    Boolean, not scored: every REQUIRE pattern must match and no REJECT pattern
    may. Every real routing decision in this domain is "two strong positives and
    no disqualifier", which is a conjunction; writing it as arithmetic with
    thresholds only hides that and invites tuning on a handful of samples.
    """
    haystack = f"{rel_path}\n{text[:8000]}"
    for profile in load_profiles():
        if any(pattern.search(haystack) for pattern in profile.REJECT):
            continue
        if all(pattern.search(haystack) for pattern in profile.REQUIRE):
            return profile
    return None


def _marker_hits(profile, text: str) -> list:
    """Every marker match, ordered by position, honouring `once`."""
    hits = []
    for marker in profile.MARKERS:
        for match in marker.pattern.finditer(text):
            hits.append((match.start(), marker, match))
            if marker.once:
                break
    hits.sort(key=lambda hit: (hit[0], hit[1].role != "zone"))
    # A zone and a section can match at the same offset (the body zone starts AT
    # 제1장, which is also a section marker). Keeping both would emit a zero-width
    # span and a duplicate heading, so the zone wins and the section is dropped.
    deduped = []
    for hit in hits:
        if deduped and deduped[-1][0] == hit[0]:
            continue
        deduped.append(hit)
    return deduped


def sections(profile, text: str) -> list:
    """Split text into (heading, start, end) spans that tile the document.

    Spans are consecutive and cover [0, len(text)), so no source character is
    dropped. A zone marker relabels everything after it until the next zone.
    """
    hits = _marker_hits(profile, text)
    if not hits:
        return [("", 0, len(text))]

    spans = []
    if hits[0][0] > 0:
        spans.append(("", 0, hits[0][0]))

    zone = ""
    for position, (start, marker, match) in enumerate(hits):
        end = hits[position + 1][0] if position + 1 < len(hits) else len(text)
        label = (match.group(0) or marker.label).strip()
        if marker.role == "zone":
            zone = marker.label
            heading = zone
        else:
            heading = f"{zone} · {label}" if zone else label
        spans.append((heading, start, end))
    return spans


def edges(profile, doc_id: str, text: str) -> list:
    """Apply the EDGES table. Returns (relation, source, source_kind, target,
    target_kind, evidence) tuples with evidence sliced from the source.

    Node names are namespaced with doc_id by the caller's format strings, which
    is not decorative: `silver.relations` de-duplicates on
    (source_entity, relation, target_entity) with no doc_id, so a bare `갑` from
    one contract would not merely merge with another's — it would delete it.
    """
    found = []
    for rule in profile.EDGES:
        for match in rule.pattern.finditer(text):
            fields = dict(match.groupdict())
            fields["doc_id"] = doc_id
            try:
                source = rule.source.format(**fields)
                target = rule.target.format(**fields)
            except (KeyError, IndexError):
                continue
            if not source.strip() or not target.strip():
                continue
            found.append(
                (
                    rule.relation,
                    source,
                    rule.source_kind,
                    target,
                    rule.target_kind,
                    # Verbatim by construction: the engine, not the profile,
                    # decides what the quote is.
                    text[match.start() : match.end()][:280],
                )
            )
    return found


def profile_signature() -> str:
    """Identity of the registered profile set, for index_signature.

    A profile decides section boundaries and therefore chunk ids, so two clones
    at different profile revisions must not silently share an index.
    """
    parts = []
    for profile in load_profiles():
        parts.append(
            f"{profile.DOC_TYPE}:{len(profile.REQUIRE)}/{len(profile.REJECT)}"
            f"/{len(profile.MARKERS)}/{len(profile.EDGES)}"
        )
    return ",".join(parts) or "none"


__all__ = [
    "EdgeRule",
    "Marker",
    "PROFILE_FIELDS",
    "PROFILE_MODULES",
    "ProfileError",
    "classify",
    "edges",
    "load_profiles",
    "profile_signature",
    "sections",
]
