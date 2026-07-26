"""Deterministic document parsing, chunking and relation extraction.

Pure functions only: no I/O, no network, no global state. The same input bytes
always produce the same chunk ids, so re-running the pipeline is idempotent
(§5 "Idempotent, resumable pipeline").

Consumed by `pipeline/tap_inbox_documents.py` (the Meltano SDK extractor), which
emits the resulting records as Singer streams.
"""

from __future__ import annotations

import ast
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Iterator

# --------------------------------------------------------------------------
# Tunables. Sizes are in characters, not model tokens: chunking must not depend
# on a tokenizer download, because the build has to run with no network.
# --------------------------------------------------------------------------
MAX_CHUNK_CHARS = 1200
CHUNK_OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 40
CHARS_PER_TOKEN_ESTIMATE = 4


class ChunkingError(RuntimeError):
    """A structural invariant of chunking was violated.

    Raised, not returned: a section a profile declared that produced no chunk is
    a silent data loss (the 부칙/시행일 case), and the build that writes the
    index the agent trusts must stop rather than ship the gap.
    """

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst"})
PYTHON_SUFFIXES = frozenset({".py"})
# Binary formats are decoded by pipeline/extract.py, which is the only module
# that needs a third-party parser. Everything here stays pure, so the list is
# restated rather than imported; smoke_test asserts the two copies agree.
BINARY_SUFFIXES = frozenset(
    {".hwp", ".hwpx", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".pdf"}
)
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES | PYTHON_SUFFIXES | BINARY_SUFFIXES
# Which rendition of one document is the AUTHORITATIVE one, highest first. An
# editable original outranks a derived copy, and a modern container outranks the
# legacy binary it was converted from; PDF is last because it is what all the
# others are exported TO.
#
# Two places consume this and a third restates it:
#   * `watcher.seed_inbox` seeds only the top-ranked rendition of a curated twin.
#   * `report_dupes` pairs an original with its PDF for the twin measurement.
#   * `transform/models/silver/documents.sql` repeats the ladder in SQL, because
#     a model cannot import Python. `smoke_test` asserts the two agree, so the
#     copy cannot drift.
FORMAT_PRIORITY = ("hwp", "hwpx", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "pdf")

# Formats that a document-type profile may claim: extracted document text, not
# authored notes. `.md` and `.rst` keep the pure Markdown path, so a short memo
# about a contract can never be routed away from front-matter and wiki-link
# relation extraction.
ROUTABLE_SUFFIXES = BINARY_SUFFIXES | frozenset({".txt"})

_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+?\.(?:md|markdown|py|txt|rst))\)")
_IDENTIFIER_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`")
_INLINE_LIST_RE = re.compile(r"\A\[(.*)\]\Z", re.DOTALL)

# Korean statute section markers, used when a document has no Markdown headings
# to split on - which is every statute that arrives as extracted HWP or PDF text.
#
# The parenthesised title is REQUIRED for an article marker, which is what keeps
# an inline cross-reference out: "제4조제1항에 따른 신고를" starts a line and
# begins with 제4조, but is a reference, not a heading. Requiring `(제목)` after
# it costs nothing in real statutes, where every article carries one.
_KO_SECTION_RE = re.compile(
    r"^[ \t]*("
    r"제\s*\d+\s*조(?:의\s*\d+)?\s*\([^)\n]{1,80}\)"
    r"|부\s*칙"
    r"|별\s*표(?:\s*\d+)?"
    r")",
    re.MULTILINE,
)

# Relation verbs recognised in document front matter. Key = front-matter field,
# value = the canonical edge label written to the graph.
FRONT_MATTER_RELATIONS = {
    "depends_on": "depends_on",
    "uses": "uses",
    "supersedes": "supersedes",
    "related": "related_to",
    "related_to": "related_to",
}


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit of text plus the offsets it came from."""

    chunk_id: str
    doc_id: str
    chunk_index: int
    heading: str
    content: str
    char_start: int
    char_end: int
    token_estimate: int


@dataclass(frozen=True)
class Relation:
    """One directed edge asserted by a document."""

    relation_id: str
    doc_id: str
    source_entity: str
    source_kind: str
    relation: str
    target_entity: str
    target_kind: str
    evidence: str


@dataclass
class ParsedDocument:
    """Everything the extractor needs to emit for a single source file."""

    doc_id: str
    rel_path: str
    doc_type: str
    title: str
    content: str
    content_sha256: str
    byte_size: int
    front_matter: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    content_fingerprint: str = field(default="")

    def __post_init__(self):
        # Content identity for cross-format duplicate detection, computed once
        # here so every construction path (binary, python, markdown, and the
        # profile router) gets it for free. It hashes the EXTRACTED content, which
        # is never mutated after construction (only `.chunks`/`.relations` are set
        # later), so the fingerprint can never desync from the text it describes.
        if not self.content_fingerprint:
            self.content_fingerprint = sha256_hex(
                normalize_for_fingerprint(self.content).encode("utf-8")
            )


# --------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    """Content hash used for immutability checks and stable ids."""
    return hashlib.sha256(data).hexdigest()


# A document shorter than this (after whitespace normalisation) never dedups on
# its content fingerprint: two short, unrelated notes can coincide, and the cost
# of double-indexing a tiny document is negligible. Must match the literal floor
# in transform/models/silver/documents.sql.
FINGERPRINT_MIN_CHARS = 200
_FINGERPRINT_WS_RE = re.compile(r"\s+")


def normalize_for_fingerprint(text: str) -> str:
    """Deterministic content key for cross-format duplicate detection.

    NFC plus whitespace-collapse ONLY, deliberately conservative. It does NOT
    strip running headers, page numbers or 의안번호, so a born-digital PDF whose
    extracted text carries that page furniture will not yet match its HWP twin;
    that gap is measured with `make dupes` and closed (stronger normalisation, or
    a MinHash escalation) before PDF renditions are trusted -- see AGENTS.md. Case
    is preserved, so two distinct code identifiers can never fold together.
    """
    return _FINGERPRINT_WS_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


# Suffixes that `normalise_entity` also strips. A document whose format is NOT
# in this set carries its format as an id suffix (`-hwp`), which keeps a `.hwp`
# and its `.hwpx` twin from collapsing onto the same document.
_ID_STRIPPED_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".py"})
_ENTITY_SUFFIX_RE = re.compile(
    r"\.(" + "|".join(sorted(s[1:] for s in _ID_STRIPPED_SUFFIXES)) + r")\Z",
    re.IGNORECASE,
)
# `\w` under re.UNICODE keeps Hangul, Han and kana. The previous `[^a-z0-9_]`
# deleted every Hangul syllable, so a Korean filename slugged to the empty
# string and fell through to an opaque sha256 stub - and two different Korean
# bills whose names shared an ASCII prefix collided on the same id.
_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


def document_id(rel_path: str) -> str:
    """Stable slug for a document, derived from its path inside data/raw.

    Must agree with `normalise_entity` for every supported suffix: an `import
    search_core` edge and the document `search_core.py` have to resolve to the
    same graph node, or edges dangle at ids no node carries. The suffix table is
    pinned by an assertion in `pipeline/smoke_test.py`.
    """
    stem = unicodedata.normalize("NFC", rel_path.rsplit("/", 1)[-1])
    fmt = ""
    for suffix in sorted(SUPPORTED_SUFFIXES, key=len, reverse=True):
        if stem.lower().endswith(suffix):
            if suffix not in _ID_STRIPPED_SUFFIXES:
                fmt = "-" + suffix[1:]
            stem = stem[: -len(suffix)]
            break
    slug = _SLUG_RE.sub("-", stem.lower()).strip("-")
    return (slug + fmt) if slug else sha256_hex(rel_path.encode("utf-8"))[:16]


def normalise_entity(name: str) -> str:
    """Canonical node id. Lower-cased, punctuation collapsed to single dashes.

    Deliberately strips only the text suffixes. A binary format such as `.hwp`
    is left in place so that it slugs to the same `-hwp` tail `document_id`
    appends, which is what keeps the two functions in agreement.
    """
    cleaned = unicodedata.normalize("NFC", name.strip().strip("`'\""))
    cleaned = _ENTITY_SUFFIX_RE.sub("", cleaned)
    cleaned = _SLUG_RE.sub("-", cleaned).strip("-")
    return cleaned.lower()


def _relation_id(doc_id: str, source: str, relation: str, target: str) -> str:
    raw = "|".join((doc_id, source, relation, target))
    return sha256_hex(raw.encode("utf-8"))[:24]


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------
def parse_front_matter(text: str) -> tuple[dict, str, int]:
    """Split a leading `---` fenced block off the document.

    Returns (fields, body, body_offset). Supports the flat subset actually used
    by this platform: `key: scalar` and `key: [a, b]` plus `- item` lists. A full
    YAML parser is deliberately avoided so that parsing stays dependency-free
    and byte-for-byte reproducible.
    """
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text, 0

    fields: dict = {}
    pending_key = None
    for raw_line in match.group(1).splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and pending_key is not None:
            fields.setdefault(pending_key, [])
            if not isinstance(fields[pending_key], list):
                fields[pending_key] = [fields[pending_key]]
            fields[pending_key].append(_scalar(line.lstrip()[2:]))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        pending_key = key
        if not value:
            fields[key] = []
            continue
        inline = _INLINE_LIST_RE.match(value)
        if inline:
            items = [_scalar(part) for part in inline.group(1).split(",")]
            fields[key] = [item for item in items if item != ""]
        else:
            fields[key] = _scalar(value)
    return fields, text[match.end():], match.end()


def _scalar(value: str) -> str:
    return value.strip().strip("'\"").strip()


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
def _heading_sections(body: str) -> list:
    """Split into (heading, start, end) sections by ATX or Korean article markers.

    Markdown headings and 제N조 markers are merged into one ordered list, so a
    statute that arrives as plain extracted text sections on its articles rather
    than being blind-windowed. A Markdown heading that happens to contain an
    article marker (`## 제3조(신고)`) matches only the ATX pattern, because the
    Korean pattern is anchored to the start of the line and `#` is not
    whitespace.
    """
    marks = [(match.start(), match.group(2).strip()) for match in _HEADING_RE.finditer(body)]
    marks.extend(
        (match.start(), match.group(1).strip()) for match in _KO_SECTION_RE.finditer(body)
    )
    if not marks:
        return [("", 0, len(body))]

    marks.sort(key=lambda mark: mark[0])
    sections = []
    if marks[0][0] > 0:
        sections.append(("", 0, marks[0][0]))
    for position, (start, heading) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(body)
        sections.append((heading, start, end))
    return sections


def _split_window(text: str, start_offset: int) -> Iterator[tuple[str, int, int]]:
    """Sliding window over one section, preferring paragraph boundaries."""
    length = len(text)
    if length <= MAX_CHUNK_CHARS:
        yield text, start_offset, start_offset + length
        return

    cursor = 0
    while cursor < length:
        window_end = min(cursor + MAX_CHUNK_CHARS, length)
        if window_end < length:
            break_at = text.rfind("\n\n", cursor + MIN_CHUNK_CHARS, window_end)
            if break_at == -1:
                break_at = text.rfind("\n", cursor + MIN_CHUNK_CHARS, window_end)
            # Take the break only if the next window starts strictly ahead of
            # this one. `rfind` returns the LAST break in the window, which can
            # sit less than one overlap ahead of the cursor; accepting it then
            # advances the cursor by exactly 1 and the same break is chosen
            # again, forever. A 3 KB body with a single blank line produced 114
            # chunks of lengths 1159, 150, 149, 148 before this guard.
            if break_at != -1 and break_at - CHUNK_OVERLAP_CHARS > cursor:
                window_end = break_at
        piece = text[cursor:window_end]
        yield piece, start_offset + cursor, start_offset + window_end
        if window_end >= length:
            return
        cursor = max(window_end - CHUNK_OVERLAP_CHARS, cursor + 1)


def chunk_text(doc_id: str, body: str, body_offset: int = 0, spans=None) -> list:
    """Heading-aware chunking with character offsets back into the raw file.

    `spans` lets a document-type profile supply the section boundaries instead of
    the generic heading scan. Everything after that - windowing, the sliver
    filter, offset arithmetic and chunk_id assignment - stays here, so a profile
    cannot affect idempotence.
    """
    profile_supplied = spans is not None
    chunks: list = []
    for heading, section_start, section_end in (spans if spans is not None else _heading_sections(body)):
        section = body[section_start:section_end]
        pieces = list(_split_window(section, section_start))
        # MIN_CHUNK_CHARS exists to discard slivers left over by the sliding
        # window, NOT to delete short-but-complete sections. Korean statutes end
        # with a 부칙 that is often ~30 characters ("이 법은 공포 후 1년이 경과한
        # 날부터 시행한다.") and carries the enforcement date, which is the single
        # most-queried fact about a statute. Applying the floor to it dropped
        # that date from 3 of 8 real bills, on a green build.
        whole_section = len(pieces) == 1
        produced_here = 0
        for piece, piece_start, piece_end in pieces:
            content = piece.strip()
            if not content:
                continue
            if len(content) < MIN_CHUNK_CHARS and not whole_section:
                continue
            index = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}#{index:04d}",
                    doc_id=doc_id,
                    chunk_index=index,
                    heading=heading,
                    content=content,
                    char_start=body_offset + piece_start,
                    char_end=body_offset + piece_end,
                    token_estimate=max(1, len(content) // CHARS_PER_TOKEN_ESTIMATE),
                )
            )
            produced_here += 1
        # A profile that named this section asserted it is a real unit. If the
        # window + sliver filter erased a section that HAS text, a queried fact
        # silently leaves the index -- the 부칙/시행일 loss (3 of 8 bills, green
        # build) generalised to the section grain. It is checked only for
        # profile-supplied spans, because those are a contract; a heuristic
        # heading scan that skips a sliver is not. Whitespace-only spans (the
        # leading gap before the first marker) legitimately produce nothing.
        # Note this cannot fire under today's filter (whole_section keeps a lone
        # short section, and a split section's first piece is always >= the
        # overlap); it is a regression guard that binds every profile, including
        # ones added later that have no fixture or eval judgment yet.
        if profile_supplied and produced_here == 0 and section.strip():
            raise ChunkingError(
                f"{doc_id}: profile-declared section {heading!r} carries "
                f"{len(section.strip())} non-space characters but produced no chunk. "
                "The sliver filter erased a section the profile claimed; the fact "
                "it held is now missing from the index. See MIN_CHUNK_CHARS and the "
                "whole_section guard above."
            )
    return chunks


def chunk_python(doc_id: str, source: str) -> list:
    """Chunk Python by top-level definition, falling back to windowing.

    Keeping a function body in one chunk is what makes an exact-identifier
    keyword hit (FTS5) and a semantic hit (sqlite-vec) point at the same unit.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return chunk_text(doc_id, source)

    lines = source.splitlines(keepends=True)
    line_offsets = [0]
    for line in lines:
        line_offsets.append(line_offsets[-1] + len(line))

    spans: list = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end_lineno = getattr(node, "end_lineno", None) or node.lineno
        start = line_offsets[node.lineno - 1]
        end = line_offsets[min(end_lineno, len(lines))]
        spans.append((node.name, start, end))

    if not spans:
        return chunk_text(doc_id, source)

    chunks: list = []
    cursor = 0
    for name, start, end in spans:
        if start > cursor:
            chunks.extend(_python_chunk_range(doc_id, source, "module", cursor, start, len(chunks)))
        chunks.extend(_python_chunk_range(doc_id, source, name, start, end, len(chunks)))
        cursor = end
    if cursor < len(source):
        chunks.extend(_python_chunk_range(doc_id, source, "module", cursor, len(source), len(chunks)))
    return chunks


def _python_chunk_range(
    doc_id: str, source: str, heading: str, start: int, end: int, base_index: int
) -> list:
    produced: list = []
    for piece, piece_start, piece_end in _split_window(source[start:end], start):
        content = piece.strip("\n")
        if len(content.strip()) < MIN_CHUNK_CHARS:
            continue
        index = base_index + len(produced)
        produced.append(
            Chunk(
                chunk_id=f"{doc_id}#{index:04d}",
                doc_id=doc_id,
                chunk_index=index,
                heading=heading,
                content=content,
                char_start=piece_start,
                char_end=piece_end,
                token_estimate=max(1, len(content) // CHARS_PER_TOKEN_ESTIMATE),
            )
        )
    return produced


# --------------------------------------------------------------------------
# Relation extraction
# --------------------------------------------------------------------------
def _as_list(value) -> list:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or str(value).strip() == "":
        return []
    return [str(value)]


def extract_markdown_relations(doc_id: str, front_matter: dict, body: str) -> list:
    """Front-matter declarations, wiki links, and backticked identifiers."""
    relations: list = []
    seen: set = set()

    def add(source: str, source_kind: str, label: str, target: str, target_kind: str, why: str):
        source_norm = normalise_entity(source)
        target_norm = normalise_entity(target)
        if not source_norm or not target_norm or source_norm == target_norm:
            return
        key = (source_norm, label, target_norm)
        if key in seen:
            return
        seen.add(key)
        relations.append(
            Relation(
                relation_id=_relation_id(doc_id, source_norm, label, target_norm),
                doc_id=doc_id,
                source_entity=source_norm,
                source_kind=source_kind,
                relation=label,
                target_entity=target_norm,
                target_kind=target_kind,
                evidence=why[:280],
            )
        )

    for key, label in FRONT_MATTER_RELATIONS.items():
        for target in _as_list(front_matter.get(key)):
            add(doc_id, "document", label, target, "document", f"front matter {key}: {target}")

    for match in _WIKILINK_RE.finditer(body):
        add(doc_id, "document", "references", match.group(1), "document", match.group(0))

    for match in _MD_LINK_RE.finditer(body):
        add(doc_id, "document", "references", match.group(1), "document", match.group(0))

    for match in _IDENTIFIER_RE.finditer(body):
        add(doc_id, "document", "mentions", match.group(1), "symbol", f"`{match.group(1)}`")

    return relations


def extract_python_relations(doc_id: str, source: str) -> list:
    """Import edges and definition edges, parsed with the stdlib AST."""
    relations: list = []
    seen: set = set()

    def add(source_entity: str, source_kind: str, label: str, target: str, target_kind: str, why: str):
        source_norm = normalise_entity(source_entity)
        target_norm = normalise_entity(target)
        if not source_norm or not target_norm or source_norm == target_norm:
            return
        key = (source_norm, label, target_norm)
        if key in seen:
            return
        seen.add(key)
        relations.append(
            Relation(
                relation_id=_relation_id(doc_id, source_norm, label, target_norm),
                doc_id=doc_id,
                source_entity=source_norm,
                source_kind=source_kind,
                relation=label,
                target_entity=target_norm,
                target_kind=target_kind,
                evidence=why[:280],
            )
        )

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return relations

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(doc_id, "module", "imports", alias.name, "module", f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            add(doc_id, "module", "imports", node.module, "module", f"from {node.module} import")
            for alias in node.names:
                add(doc_id, "module", "uses", alias.name, "symbol", f"from {node.module} import {alias.name}")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(doc_id, "module", "defines", node.name, "symbol", f"def/class {node.name}")

    return relations


# --------------------------------------------------------------------------
# Top-level entry point
# --------------------------------------------------------------------------
def _parse_with_profile(doc_id, rel_path, suffix, text, raw_bytes):
    """Parse via a document-type profile, or return None if none claims it.

    One implementation for every routable format. The profile supplies section
    boundaries and edge templates; chunk_id assignment, offset arithmetic and
    relation-id hashing stay here, so an authored profile cannot affect
    idempotence. Profile edges are MERGED with the generic extractors rather
    than replacing them, so front-matter and wiki-link relations survive.
    """
    from pipeline.doctypes import classify, edges as profile_edges, sections as profile_sections

    profile = classify(rel_path, text)
    if profile is None:
        return None

    parsed = ParsedDocument(
        doc_id=doc_id,
        rel_path=rel_path,
        doc_type=suffix[1:] if suffix else "text",
        title=rel_path.rsplit("/", 1)[-1],
        # `content` is the readable text because that is what is searched;
        # `content_sha256` hashes the ORIGINAL bytes, so provenance points at the
        # immutable file rather than at a parser output that would change when
        # the parser is upgraded.
        content=text,
        content_sha256=sha256_hex(raw_bytes),
        byte_size=len(raw_bytes),
        front_matter={},
    )
    parsed.chunks = chunk_text(doc_id, text, spans=profile_sections(profile, text))

    relations = extract_markdown_relations(doc_id, {}, text)
    seen = {(item.source_entity, item.relation, item.target_entity) for item in relations}
    for relation, source, source_kind, target, target_kind, evidence in profile_edges(
        profile, doc_id, text
    ):
        source_norm = normalise_entity(source)
        target_norm = normalise_entity(target)
        key = (source_norm, relation, target_norm)
        if not source_norm or not target_norm or source_norm == target_norm or key in seen:
            continue
        seen.add(key)
        relations.append(
            Relation(
                relation_id=_relation_id(doc_id, source_norm, relation, target_norm),
                doc_id=doc_id,
                source_entity=source_norm,
                source_kind=source_kind,
                relation=relation,
                target_entity=target_norm,
                target_kind=target_kind,
                evidence=evidence,
            )
        )
    parsed.relations = relations
    return parsed


def parse_document(rel_path: str, raw_bytes: bytes) -> ParsedDocument:
    """Parse one immutable file from data/raw into documents/chunks/relations.

    Line endings are normalised to `\\n` first, so the same document committed
    from Windows and from macOS yields identical chunk ids. The cost is that
    `char_start`/`char_end` are offsets into the normalised text rather than into
    the raw bytes for CRLF files; nothing reads them back against the original.
    """
    doc_id = document_id(rel_path)
    suffix = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else ""

    if suffix in BINARY_SUFFIXES:
        # Imported lazily so the pure-python path never pays for the parser, and
        # so a missing optional dependency surfaces only when a binary document
        # is actually ingested.
        from pipeline.extract import extract_text

        text = extract_text(rel_path, raw_bytes)
        routed = _parse_with_profile(doc_id, rel_path, suffix, text, raw_bytes)
        if routed is not None:
            return routed
        parsed = ParsedDocument(
            doc_id=doc_id,
            rel_path=rel_path,
            doc_type=suffix[1:],
            title=rel_path.rsplit("/", 1)[-1],
            content=text,
            content_sha256=sha256_hex(raw_bytes),
            byte_size=len(raw_bytes),
            front_matter={},
        )
        parsed.chunks = chunk_text(doc_id, text)
        parsed.relations = extract_markdown_relations(doc_id, {}, text)
        return parsed

    text = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    if suffix in PYTHON_SUFFIXES:
        parsed = ParsedDocument(
            doc_id=doc_id,
            rel_path=rel_path,
            doc_type="python",
            title=rel_path.rsplit("/", 1)[-1],
            content=text,
            content_sha256=sha256_hex(raw_bytes),
            byte_size=len(raw_bytes),
            front_matter={},
        )
        parsed.chunks = chunk_python(doc_id, text)
        parsed.relations = extract_python_relations(doc_id, text)
        return parsed

    if suffix in ROUTABLE_SUFFIXES:
        routed = _parse_with_profile(doc_id, rel_path, suffix, text, raw_bytes)
        if routed is not None:
            return routed

    front_matter, body, body_offset = parse_front_matter(text)
    heading_match = _HEADING_RE.search(body)
    title = str(
        front_matter.get("title")
        or (heading_match.group(2).strip() if heading_match else "")
        or rel_path.rsplit("/", 1)[-1]
    )
    parsed = ParsedDocument(
        doc_id=doc_id,
        rel_path=rel_path,
        doc_type="markdown",
        title=title,
        content=text,
        content_sha256=sha256_hex(raw_bytes),
        byte_size=len(raw_bytes),
        front_matter=front_matter,
    )
    parsed.chunks = chunk_text(doc_id, body, body_offset)
    parsed.relations = extract_markdown_relations(doc_id, front_matter, body)
    return parsed


def iter_supported(paths: Iterable) -> Iterator:
    """Filter an iterable of pathlib paths down to the extensions we parse."""
    for path in paths:
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path
