"""Query-side alias expansion for domain synonyms.

Character n-grams bridge morphology, never synonymy: 스테이블코인 and 자산연동형
denote the same thing and share no characters at all. This is the cheapest thing
that closes that gap, and it does so at query time only, so no re-index is
needed and `index_signature` is untouched.

Expansion is deliberately NOT done by OR-ing aliases into one FTS query. That was
measured and it is worse (nDCG@10 0.5953 against 0.7455) because OR-ing dilutes
the discriminative term among generic ones. Instead each alias variant is a
separate retrieval whose ranked list is fused, which is what
`pipeline.build_rag.multi_hybrid_search` does.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

ALIAS_FILE = Path(__file__).with_name("aliases.tsv")
MAX_VARIANTS = 8


@lru_cache(maxsize=1)
def load_groups(path: str = "") -> tuple:
    """Read the alias table into (group, surfaces) tuples, longest surface first.

    Cached: the file is small and read on every query.
    """
    source = Path(path) if path else ALIAS_FILE
    if not source.exists():
        return ()

    groups: dict = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) < 2:
            continue
        group, surface = parts[0].strip(), parts[1].strip()
        if group and surface:
            groups.setdefault(group, []).append(surface)

    # Longest surface first so 가치안정형 is matched before any shorter surface
    # that happens to be contained in it.
    return tuple(
        (group, tuple(sorted(set(surfaces), key=len, reverse=True)))
        for group, surfaces in sorted(groups.items())
    )


def _surface_pattern(surface: str) -> re.Pattern:
    """Match a surface tolerant of whitespace between its characters.

    Korean legal text glues and splits compounds inconsistently, so
    `가치안정형 디지털자산` and `가치안정형디지털자산` must both match.
    """
    return re.compile(r"\s*".join(re.escape(character) for character in surface))


def expand_query(query: str, max_variants: int = MAX_VARIANTS) -> list:
    """Return the query plus one variant per alias of the first group it hits.

    The original query is always first, so a caller that ignores the rest still
    behaves exactly as it did before. Only ONE group is expanded: expanding two
    groups multiplies the retrieval count for a measured loss.
    """
    normalised = unicodedata.normalize("NFC", query)
    variants = [normalised]

    for _group, surfaces in load_groups():
        matched = None
        for surface in surfaces:
            pattern = _surface_pattern(surface)
            if pattern.search(normalised):
                matched = (surface, pattern)
                break
        if matched is None:
            continue

        surface, pattern = matched
        for other in surfaces:
            if other == surface:
                continue
            candidate = pattern.sub(other, normalised, count=1)
            if candidate != normalised and candidate not in variants:
                variants.append(candidate)
        break

    return variants[:max_variants]


def alias_signature() -> str:
    """Stable identity of the alias table, for reporting and debugging.

    Not part of index_signature: expansion is query-side only, so changing the
    table cannot invalidate an existing index.
    """
    groups = load_groups()
    return "|".join(f"{group}:{len(surfaces)}" for group, surfaces in groups) or "empty"
