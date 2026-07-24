"""Text extraction for binary document formats: HWP and HWPX.

Korean government and National Assembly material is published as HWP, so this is
the seam that turns those bytes into something the chunker can read. It is kept
separate from `pipeline/chunking.py` because chunking is pure and dependency-free
and should stay that way: only this module needs a third-party parser.

Two rules govern everything here:

  * NEVER write to stdout. This code runs inside a Singer tap, whose stdout IS
    the record stream. A stray print corrupts the pipe and the loader fails with
    a JSON error a long way from the cause.
  * Fail loudly, never partially. A password-protected file or a parser that
    yields replacement characters must raise, because a half-extracted statute
    produces a chunk that is retrievable and wrong, which is worse than a
    document that is visibly missing.

The extracted text is not cached. `parse_document` runs three times per file per
build (the documents, chunks and relations streams each walk the raw zone), and
measured extraction is ~0.04 s per bill, so the total is well under a second.
Revisit if a format lands whose parsing costs seconds.
"""

from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

HWP_SUFFIX = ".hwp"
HWPX_SUFFIX = ".hwpx"
BINARY_SUFFIXES = frozenset({HWP_SUFFIX, HWPX_SUFFIX})

# HWP 5.0 FileHeader: a 32-byte signature, 4 bytes of version, then 4 bytes of
# flags. Bit 1 marks the document as password protected, which means the body
# streams are encrypted and any text we managed to read would be garbage.
_HWP_SIGNATURE = b"HWP Document File"
_HWP_FLAG_PASSWORD = 0x02

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
    """Strip the XML namespace, which differs between HWPX producer versions."""
    return tag.rsplit("}", 1)[-1]


def _extract_hwpx(raw_bytes: bytes, rel_path: str) -> str:
    """Extract text from an HWPX file using only the standard library.

    HWPX is a zip whose body lives in `Contents/section*.xml`. Text runs are
    `<hp:t>` inside `<hp:p>` paragraphs. Element names are matched on their local
    name so a namespace change in a future Hancom release does not silently
    yield an empty document.
    """
    import io

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as error:
        raise ExtractionCorrupt(f"{rel_path} is not a valid HWPX (zip) file") from error

    sections = sorted(
        name
        for name in archive.namelist()
        if name.lower().startswith("contents/section") and name.lower().endswith(".xml")
    )
    if not sections:
        raise ExtractionCorrupt(f"{rel_path} has no Contents/section*.xml streams")

    paragraphs: list = []
    for section in sections:
        try:
            root = ElementTree.fromstring(archive.read(section))
        except ElementTree.ParseError as error:
            raise ExtractionCorrupt(f"{rel_path}:{section} is not well-formed XML") from error
        for element in root.iter():
            if _local_name(element.tag) != "p":
                continue
            runs = [
                node.text
                for node in element.iter()
                if _local_name(node.tag) == "t" and node.text
            ]
            paragraphs.append("".join(runs))
    return "\n".join(paragraphs)


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
    if suffix == HWP_SUFFIX:
        text = _extract_hwp(raw_bytes, rel_path)
    elif suffix == HWPX_SUFFIX:
        text = _extract_hwpx(raw_bytes, rel_path)
    else:
        raise ExtractionError(f"{rel_path}: no extractor for {suffix!r}")
    return _validate(text.replace("\r\n", "\n").replace("\r", "\n"), rel_path)
