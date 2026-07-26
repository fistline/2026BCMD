"""Text extraction for binary document formats.

Korean government and National Assembly material is published as HWP, so this is
the seam that turns those bytes into something the chunker can read. It is kept
separate from `pipeline/chunking.py` because chunking is pure and dependency-free
and should stay that way: only this module needs a third-party parser.

Formats fall into three groups, and the group decides how much is trusted:

  HWP / HWPX      the native corpus. HWP needs `hwpkit`; HWPX is a zip of XML and
                  is read with the standard library alone.
  OOXML           `.docx` / `.xlsx` / `.pptx` are also zips of XML, so they are
                  read the same stdlib way. The extraction is a SWEEP over every
                  text node rather than a structural parse, which is what makes
                  it pick up tables, text boxes and footnotes without knowing the
                  schema.
  legacy binary   `.doc` / `.xls` / `.ppt` (OLE compound files) have no usable
                  pure-python reader, so they go through the optional
                  `office-oxide` parser. It is opt-in (`uv sync --extra legacy`)
                  because no document in the corpus uses these formats yet, and
                  because a young parser earns its trust from `_validate` below
                  rather than from its version number.

Two rules govern everything here:

  * NEVER write to stdout. This code runs inside a Singer tap, whose stdout IS
    the record stream. A stray print corrupts the pipe and the loader fails with
    a JSON error a long way from the cause.
  * Fail loudly, never partially. A password-protected file or a parser that
    yields replacement characters must raise, because a half-extracted statute
    produces a chunk that is retrievable and wrong, which is worse than a
    document that is visibly missing.

Extraction IS cached, keyed on the sha256 of the input bytes. `parse_document`
runs three times per file per build (the documents, chunks and relations streams
each walk the raw zone). At ~0.04 s per HWP bill that repetition was free and the
cache was deliberately absent; PDF is the format that comment warned about, at
~0.7 s for a 161-page bill, where the same three passes cost seconds per document.
The key is the content, not the path, so the cache cannot change what is
extracted -- identical bytes have exactly one extraction either way.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

HWP_SUFFIX = ".hwp"
HWPX_SUFFIX = ".hwpx"
DOCX_SUFFIX = ".docx"
XLSX_SUFFIX = ".xlsx"
PPTX_SUFFIX = ".pptx"
DOC_SUFFIX = ".doc"
XLS_SUFFIX = ".xls"
PPT_SUFFIX = ".ppt"
PDF_SUFFIX = ".pdf"

# Office Open XML: a zip of XML parts, read with the standard library.
OOXML_SUFFIXES = frozenset({DOCX_SUFFIX, XLSX_SUFFIX, PPTX_SUFFIX})
# Office 97-2003 OLE compound files, read by the optional `office-oxide` parser.
LEGACY_SUFFIXES = frozenset({DOC_SUFFIX, XLS_SUFFIX, PPT_SUFFIX})

# Must equal chunking.BINARY_SUFFIXES. The two are separate because chunking
# stays importable without this module; `pipeline/smoke_test.py` asserts they
# agree, so a format added to one and forgotten in the other fails the build
# instead of being silently unreadable.
BINARY_SUFFIXES = (
    frozenset({HWP_SUFFIX, HWPX_SUFFIX, PDF_SUFFIX}) | OOXML_SUFFIXES | LEGACY_SUFFIXES
)

# HWP 5.0 FileHeader: a 32-byte signature, 4 bytes of version, then 4 bytes of
# flags. Bit 1 marks the document as password protected, which means the body
# streams are encrypted and any text we managed to read would be garbage.
_HWP_SIGNATURE = b"HWP Document File"
_HWP_FLAG_PASSWORD = 0x02

# OLE/CFB container magic. An ENCRYPTED OOXML file is not a zip at all: Office
# wraps the encrypted package in an OLE compound file, so `zipfile` reports it as
# corrupt. Checking the magic tells a password-protected file apart from a truly
# damaged one, which is the difference between "remove the password" and "the
# bytes are broken".
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OOXML_ENCRYPTED_STREAM = "EncryptedPackage"

# (suffix, sha256 of the bytes) -> extracted text. Process-local and unbounded:
# it lives for one `make build`, whose working set is the corpus it was going to
# read anyway. Keyed on content so it cannot mask a changed file.
_EXTRACTION_CACHE: dict = {}

# Control characters that must not survive extraction. Tab, newline and carriage
# return are legitimate; everything else in C0 signals that record boundaries
# were misread and the text is structurally wrong.
_BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REPLACEMENT_CHAR = "�"
# Above this fraction of replacement characters the parse is not recoverable.
_MAX_REPLACEMENT_RATIO = 0.005


class ExtractionError(RuntimeError):
    """Base class for every extraction failure, so callers can catch one thing."""


class ExtractionProtected(ExtractionError):
    """The document is password protected and cannot be read."""


class ExtractionEmpty(ExtractionError):
    """The parser ran but produced no usable text."""


class ExtractionCorrupt(ExtractionError):
    """The parser produced text that is structurally wrong."""


class ExtractionUnavailable(ExtractionError):
    """The parser is not installed in the environment that is running."""


def _require(module: str):
    """Import a parser dependency, or explain exactly how to get it.

    Meltano runs the tap in its OWN virtualenv, built from `pip_url: -e .`. Adding
    a dependency to pyproject.toml updates the project venv but NOT the plugin
    venv, so the tap keeps running against the older install until the plugins
    are reinstalled. Without this the failure surfaces as a bare
    ModuleNotFoundError buried in Meltano's log output.
    """
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as error:
        raise ExtractionUnavailable(
            f"the {module!r} parser is not installed in this environment. "
            "If this came from the Meltano tap, its plugin venv is stale: "
            "run `make setup` (which reinstalls plugins) and build again."
        ) from error


def _check_hwp_protected(raw_bytes: bytes, rel_path: str) -> None:
    """Raise if the HWP FileHeader marks the document as password protected."""
    olefile = _require("olefile")

    with tempfile.NamedTemporaryFile(suffix=HWP_SUFFIX, delete=False) as handle:
        handle.write(raw_bytes)
        temp_path = handle.name
    try:
        if not olefile.isOleFile(temp_path):
            raise ExtractionCorrupt(f"{rel_path} is not a valid HWP (OLE) file")
        ole = olefile.OleFileIO(temp_path)
        try:
            if not ole.exists("FileHeader"):
                raise ExtractionCorrupt(f"{rel_path} has no HWP FileHeader stream")
            header = ole.openstream("FileHeader").read(40)
        finally:
            ole.close()
    finally:
        os.unlink(temp_path)

    if not header.startswith(_HWP_SIGNATURE):
        raise ExtractionCorrupt(f"{rel_path} has an unexpected HWP signature")
    flags = int.from_bytes(header[36:40], "little")
    if flags & _HWP_FLAG_PASSWORD:
        raise ExtractionProtected(
            f"{rel_path} is password protected. Remove the password and drop it in again."
        )


def _extract_hwp(raw_bytes: bytes, rel_path: str) -> str:
    """Extract text from a binary HWP 5.0 file.

    hwpkit's API takes a path rather than bytes, and the raw zone must not be
    handed out directly, so the bytes go through a temporary file that is always
    removed.
    """
    _check_hwp_protected(raw_bytes, rel_path)

    extract_text_from_hwp = _require("hwpkit").extract_text_from_hwp

    with tempfile.NamedTemporaryFile(suffix=HWP_SUFFIX, delete=False) as handle:
        handle.write(raw_bytes)
        temp_path = handle.name
    try:
        return extract_text_from_hwp(temp_path)
    except ExtractionError:
        raise
    except Exception as error:
        raise ExtractionCorrupt(f"{rel_path} could not be parsed as HWP: {error}") from error
    finally:
        os.unlink(temp_path)


def _local_name(tag: str) -> str:
    """Strip the XML namespace, which differs between producer versions."""
    return tag.rsplit("}", 1)[-1]


def _attr(element, name: str):
    """Read an attribute by LOCAL name, ignoring its namespace prefix.

    `r:id` on an OOXML sheet reference is namespace-qualified in the file but the
    prefix is producer-chosen, so matching the expanded name would be brittle in
    exactly the way `_local_name` exists to avoid.
    """
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value
    return None


def _paragraph_texts(root, breaks: bool = False) -> list:
    """Text of every paragraph under `root`, in document order, without repeats.

    Paragraphs NEST: an HWPX table lives inside a `<hp:p>` and its cells hold
    their own `<hp:p>`, and a Word text box holds DrawingML `<a:p>` inside a
    `<w:p>`. A naive "every element whose local name is p, joined from every
    descendant `t`" therefore emits cell and text-box content TWICE -- once from
    the outer paragraph's descendant walk and once from the inner paragraph. That
    duplicate is invisible downstream: the chunk count stays plausible and the
    text reads correctly, while the 신구조문대비표 in a bill is indexed twice and
    can fill a top-K on its own.

    So an outer paragraph consumes its descendants, and a nested paragraph is
    skipped. `id()` is safe as the identity key because `root` holds every
    element alive for the whole walk.

    `breaks` maps `<w:br>`/`<w:cr>` to a newline and `<w:tab>` to a tab, which is
    what keeps a Word line break from silently glueing two lines together. Those
    tags do not exist in HWPX, so the flag costs nothing there.
    """
    consumed: set = set()
    paragraphs: list = []
    for element in root.iter():
        if _local_name(element.tag) != "p" or id(element) in consumed:
            continue
        pieces: list = []
        for node in element.iter():
            consumed.add(id(node))
            name = _local_name(node.tag)
            if name == "t" and node.text:
                pieces.append(node.text)
            elif breaks and name in ("br", "cr"):
                pieces.append("\n")
            elif breaks and name == "tab":
                pieces.append("\t")
        paragraphs.append("".join(pieces))
    return paragraphs


def _open_zip(raw_bytes: bytes, rel_path: str, label: str):
    """Open a zip-backed document, telling "encrypted" apart from "corrupt"."""
    import io

    try:
        return zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as error:
        if raw_bytes.startswith(_OLE_MAGIC):
            _raise_if_ooxml_encrypted(raw_bytes, rel_path)
        raise ExtractionCorrupt(f"{rel_path} is not a valid {label} (zip) file") from error


def _raise_if_ooxml_encrypted(raw_bytes: bytes, rel_path: str) -> None:
    """Raise ExtractionProtected if these OLE bytes are an encrypted package."""
    import io

    olefile = _require("olefile")
    try:
        ole = olefile.OleFileIO(io.BytesIO(raw_bytes))
    except Exception:
        return
    try:
        encrypted = ole.exists(_OOXML_ENCRYPTED_STREAM)
    finally:
        ole.close()
    if encrypted:
        raise ExtractionProtected(
            f"{rel_path} is password protected. Remove the password and drop it in again."
        )


def _parse_part(archive, name: str, rel_path: str):
    """Parse one XML part out of an OOXML/HWPX zip, or say which part broke."""
    try:
        return ElementTree.fromstring(archive.read(name))
    except ElementTree.ParseError as error:
        raise ExtractionCorrupt(f"{rel_path}:{name} is not well-formed XML") from error


def _numbered_parts(archive, prefix: str) -> list:
    """Parts named `<prefix><N>.xml`, ordered NUMERICALLY.

    Lexicographic order puts slide10 before slide2, which would reorder a deck
    and move every chunk id in it. The trailing digits are the sort key, and a
    part without them sorts last on its name so the order stays total.
    """
    matched: list = []
    for name in archive.namelist():
        lowered = name.lower()
        if not lowered.startswith(prefix) or not lowered.endswith(".xml"):
            continue
        digits = re.search(r"(\d+)\.xml\Z", lowered)
        matched.append(((0, int(digits.group(1)), "") if digits else (1, 0, lowered), name))
    return [name for _, name in sorted(matched)]


def _extract_hwpx(raw_bytes: bytes, rel_path: str) -> str:
    """Extract text from an HWPX file using only the standard library.

    HWPX is a zip whose body lives in `Contents/section*.xml`. Text runs are
    `<hp:t>` inside `<hp:p>` paragraphs. Element names are matched on their local
    name so a namespace change in a future Hancom release does not silently
    yield an empty document.
    """
    archive = _open_zip(raw_bytes, rel_path, "HWPX")
    sections = _numbered_parts(archive, "contents/section")
    if not sections:
        raise ExtractionCorrupt(f"{rel_path} has no Contents/section*.xml streams")

    paragraphs: list = []
    for section in sections:
        paragraphs.extend(_paragraph_texts(_parse_part(archive, section, rel_path)))
    return "\n".join(paragraphs)


def _extract_docx(raw_bytes: bytes, rel_path: str) -> str:
    """Extract text from a DOCX file using only the standard library.

    Body first, then footnotes and endnotes, each in numeric part order. Headers
    and footers are deliberately EXCLUDED: they are page furniture repeated on
    every page, and including them is what makes a document's fingerprint drift
    away from the same document exported as PDF -- the cross-format collapse in
    `silver.documents` depends on those two agreeing.

    No Markdown headings are synthesised from Word heading styles. `w:pStyle`
    values are template- and locale-dependent (`Heading1`, `1`, `제목1`), and an
    invented `#` would land in `content` and change the fingerprint. Korean legal
    text sections on its own 제N조 markers in `chunking._KO_SECTION_RE`, which is
    exactly how HWP already behaves.
    """
    archive = _open_zip(raw_bytes, rel_path, "DOCX")
    names = set(archive.namelist())
    if "word/document.xml" not in names:
        raise ExtractionCorrupt(f"{rel_path} has no word/document.xml stream")

    parts = ["word/document.xml"]
    parts.extend(_numbered_parts(archive, "word/footnotes"))
    parts.extend(_numbered_parts(archive, "word/endnotes"))

    paragraphs: list = []
    for part in parts:
        paragraphs.extend(_paragraph_texts(_parse_part(archive, part, rel_path), breaks=True))
    return "\n".join(paragraphs)


def _extract_pptx(raw_bytes: bytes, rel_path: str) -> str:
    """Extract text from a PPTX file using only the standard library.

    Each slide gets an ATX heading (`# 슬라이드 3`) before its text, and speaker
    notes follow their slide under a heading of their own. Unlike DOCX, a deck
    carries NO section markers in its own content, so without these the whole
    file is one unheaded blob that the sliding window cuts at arbitrary points
    and that retrieval can cite no location for. A deck has no cross-format twin
    to keep a fingerprint comparable with, so the synthesised heading costs
    nothing that DOCX could not afford.
    """
    archive = _open_zip(raw_bytes, rel_path, "PPTX")
    slides = _numbered_parts(archive, "ppt/slides/slide")
    if not slides:
        raise ExtractionCorrupt(f"{rel_path} has no ppt/slides/slide*.xml streams")
    notes = {
        _slide_number(name): name
        for name in _numbered_parts(archive, "ppt/notesslides/notesslide")
    }

    lines: list = []
    for position, slide in enumerate(slides, start=1):
        lines.append(f"# 슬라이드 {position}")
        lines.extend(_paragraph_texts(_parse_part(archive, slide, rel_path)))
        note = notes.get(_slide_number(slide))
        if note is None:
            continue
        note_lines = [text for text in _paragraph_texts(_parse_part(archive, note, rel_path)) if text.strip()]
        if note_lines:
            lines.append(f"## 슬라이드 {position} 노트")
            lines.extend(note_lines)
    return "\n".join(lines)


def _slide_number(name: str):
    """Trailing digits of a slide part name, used to pair notes with slides."""
    digits = re.search(r"(\d+)\.xml\Z", name.lower())
    return int(digits.group(1)) if digits else None


def _extract_xlsx(raw_bytes: bytes, rel_path: str) -> str:
    """Extract text from an XLSX file using only the standard library.

    Sheets are emitted in WORKBOOK DECLARATION order, not zip order, so two
    machines that rezip the same workbook produce the same text and the same
    chunk ids. Each sheet gets an ATX heading with its real name and each row is
    tab-separated, which is the same reasoning as PPTX: a workbook carries no
    section markers of its own.

    Cell values come from the shared-string table (`t="s"`), an inline string
    (`t="inlineStr"`), a cached formula result (`t="str"`), or the raw `<v>`.
    Booleans are spelled out, because a bare 0/1 is not searchable text.
    """
    archive = _open_zip(raw_bytes, rel_path, "XLSX")
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names:
        raise ExtractionCorrupt(f"{rel_path} has no xl/workbook.xml stream")

    shared: list = []
    if "xl/sharedStrings.xml" in names:
        root = _parse_part(archive, "xl/sharedStrings.xml", rel_path)
        for item in root:
            if _local_name(item.tag) != "si":
                continue
            shared.append(
                "".join(node.text for node in item.iter() if _local_name(node.tag) == "t" and node.text)
            )

    lines: list = []
    for sheet_name, part in _workbook_sheets(archive, rel_path, names):
        lines.append(f"# 시트: {sheet_name}")
        root = _parse_part(archive, part, rel_path)
        for row in root.iter():
            if _local_name(row.tag) != "row":
                continue
            cells = [
                _cell_text(cell, shared, rel_path)
                for cell in row
                if _local_name(cell.tag) == "c"
            ]
            if any(cell.strip() for cell in cells):
                lines.append("\t".join(cells).rstrip("\t"))
    return "\n".join(lines)


def _workbook_sheets(archive, rel_path: str, names: set) -> list:
    """(sheet name, part path) in workbook declaration order.

    The declared `r:id` is resolved through `xl/_rels/workbook.xml.rels`. When a
    producer omits the rels part, the fallback pairs declaration order with
    numeric sheet order, which is what every mainstream writer emits anyway.
    """
    root = _parse_part(archive, "xl/workbook.xml", rel_path)
    declared = [
        (_attr(sheet, "name") or "sheet", _attr(sheet, "id"))
        for sheet in root.iter()
        if _local_name(sheet.tag) == "sheet"
    ]
    if not declared:
        raise ExtractionCorrupt(f"{rel_path} declares no sheets in xl/workbook.xml")

    targets: dict = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rels = _parse_part(archive, "xl/_rels/workbook.xml.rels", rel_path)
        for relationship in rels.iter():
            if _local_name(relationship.tag) != "Relationship":
                continue
            # A Target is usually relative to xl/ ("worksheets/sheet1.xml") but
            # may be written absolute ("/xl/worksheets/sheet1.xml"). Prefixing
            # blindly would double the prefix and silently miss every sheet, so
            # the part is only rebased when it is not already rooted at xl/.
            target = (_attr(relationship, "Target") or "").lstrip("/").replace("../", "")
            targets[_attr(relationship, "Id")] = (
                target if target.startswith("xl/") else "xl/" + target
            )

    ordered = _numbered_parts(archive, "xl/worksheets/sheet")
    sheets: list = []
    for position, (sheet_name, rel_id) in enumerate(declared):
        part = targets.get(rel_id)
        if part not in names:
            part = ordered[position] if position < len(ordered) else None
        if part is not None:
            sheets.append((sheet_name, part))
    if not sheets:
        raise ExtractionCorrupt(f"{rel_path} has no readable xl/worksheets/sheet*.xml streams")
    return sheets


def _cell_text(cell, shared: list, rel_path: str) -> str:
    """One cell's text, resolved against the shared-string table."""
    kind = _attr(cell, "t")
    if kind == "inlineStr":
        return "".join(
            node.text for node in cell.iter() if _local_name(node.tag) == "t" and node.text
        )
    value = next((node.text for node in cell if _local_name(node.tag) == "v"), None)
    if value is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError) as error:
            raise ExtractionCorrupt(
                f"{rel_path}: a cell references shared string {value!r}, "
                f"which is not in the {len(shared)}-entry table"
            ) from error
    if kind == "b":
        return "TRUE" if value.strip() == "1" else "FALSE"
    return value


def _extract_legacy(raw_bytes: bytes, rel_path: str, suffix: str) -> str:
    """Extract text from an Office 97-2003 binary (.doc/.xls/.ppt).

    These are OLE compound files whose text lives behind a piece table, and no
    maintained pure-python reader exists for .doc or .ppt, so this is the one
    place a young third-party parser is used. It is optional on purpose: nothing
    in the corpus is a legacy binary, so the wheel is not worth carrying by
    default, and `_require` turns a missing install into an actionable message
    rather than a traceback.

    The parser's output still goes through `_validate`, which is what keeps a
    misparse from being indexed: empty text, replacement characters or stray
    control bytes reject the DOCUMENT instead of shipping plausible-looking text
    that is wrong.
    """
    if not raw_bytes.startswith(_OLE_MAGIC):
        raise ExtractionCorrupt(f"{rel_path} is not a valid OLE compound file")

    module = _require("office_oxide")
    try:
        document = module.Document.from_bytes(raw_bytes, suffix.lstrip("."))
        return document.plain_text()
    except ExtractionError:
        raise
    except Exception as error:
        message = str(error)
        if "encrypt" in message.lower() or "password" in message.lower():
            raise ExtractionProtected(
                f"{rel_path} is password protected. Remove the password and drop it in again."
            ) from error
        raise ExtractionCorrupt(
            f"{rel_path} could not be parsed as {suffix.lstrip('.').upper()}: {error}"
        ) from error


def _extract_pdf(raw_bytes: bytes, rel_path: str) -> str:
    """Extract the text layer of a born-digital PDF.

    Only the TEXT LAYER. A scanned page carries no text and raises through
    `_validate`, which is correct: the answer for a scan is `tools/ocr/`, whose
    output a human reviews before it lands in the inbox. Silently indexing an
    empty scan would make the document look present and unfindable.

    A PDF exported from HWP can also ship fonts with no ToUnicode map, in which
    case the glyphs decode to replacement characters. That is the same failure
    and takes the same route out: `_validate` rejects it, and OCR is the answer.
    """
    import io

    pypdf = _require("pypdf")
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    except Exception as error:
        raise ExtractionCorrupt(f"{rel_path} could not be opened as PDF: {error}") from error

    if reader.is_encrypted:
        # An empty user password is the "owner-locked but readable" case, which
        # is common for filings and is not a reason to refuse the document.
        try:
            opened = reader.decrypt("") if hasattr(reader, "decrypt") else 0
        except Exception:
            opened = 0
        if not opened:
            raise ExtractionProtected(
                f"{rel_path} is password protected. Remove the password and drop it in again."
            )

    pages: list = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as error:
            raise ExtractionCorrupt(
                f"{rel_path}: page {number} could not be read: {error}"
            ) from error
    return "\n".join(pages)


def _validate(text: str, rel_path: str) -> str:
    """Reject text that parsed without raising but is structurally wrong."""
    if not text or not text.strip():
        raise ExtractionEmpty(f"{rel_path} produced no text")

    replacements = text.count(_REPLACEMENT_CHAR)
    if replacements and replacements / len(text) > _MAX_REPLACEMENT_RATIO:
        raise ExtractionCorrupt(
            f"{rel_path} produced {replacements} replacement characters in "
            f"{len(text)} characters; the encoding or record layout was misread"
        )

    stray = _BAD_CONTROL_RE.findall(text)
    if stray:
        raise ExtractionCorrupt(
            f"{rel_path} produced {len(stray)} stray control characters; "
            "record boundaries were misread"
        )
    return text


def extract_text(rel_path: str, raw_bytes: bytes) -> str:
    """Extract plain text from a supported binary document.

    Raises an ExtractionError subclass rather than returning partial text, so a
    document that cannot be read is visibly absent instead of quietly wrong.
    """
    suffix = Path(rel_path).suffix.lower()
    key = (suffix, hashlib.sha256(raw_bytes).digest())
    cached = _EXTRACTION_CACHE.get(key)
    if cached is not None:
        return cached
    text = _extract_uncached(suffix, rel_path, raw_bytes)
    _EXTRACTION_CACHE[key] = text
    return text


def _extract_uncached(suffix: str, rel_path: str, raw_bytes: bytes) -> str:
    """Dispatch to the extractor for `suffix`, then validate what came back."""
    if suffix == HWP_SUFFIX:
        text = _extract_hwp(raw_bytes, rel_path)
    elif suffix == HWPX_SUFFIX:
        text = _extract_hwpx(raw_bytes, rel_path)
    elif suffix == DOCX_SUFFIX:
        text = _extract_docx(raw_bytes, rel_path)
    elif suffix == XLSX_SUFFIX:
        text = _extract_xlsx(raw_bytes, rel_path)
    elif suffix == PPTX_SUFFIX:
        text = _extract_pptx(raw_bytes, rel_path)
    elif suffix in LEGACY_SUFFIXES:
        text = _extract_legacy(raw_bytes, rel_path, suffix)
    elif suffix == PDF_SUFFIX:
        text = _extract_pdf(raw_bytes, rel_path)
    else:
        raise ExtractionError(f"{rel_path}: no extractor for {suffix!r}")
    return _validate(text.replace("\r\n", "\n").replace("\r", "\n"), rel_path)
