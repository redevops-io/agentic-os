"""Local, dependency-free OOXML / PDF inspection used for validation and read-back verification.

Documents are UNTRUSTED input: XML containing a DTD / entity declaration is refused, every ZIP member is size-capped
(zip-bomb guard) and nothing here ever executes or follows anything inside a document. Text returned from here is
*evidence*, never an instruction.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

from .errors import InvalidInput

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_PART = 64 * 1024 * 1024
MAX_PARTS = 5000

KIND_BY_EXT = {"docx": "docx", "xlsx": "xlsx", "pptx": "pptx"}
MAIN_PART = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
MEDIA = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
         "pdf": "application/pdf", "txt": "text/plain"}


def ext_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _zip(data: bytes) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InvalidInput("not a valid OOXML (zip) container") from None
    if len(z.infolist()) > MAX_PARTS:
        raise InvalidInput("too many parts in container")
    for zi in z.infolist():
        if zi.file_size > MAX_PART:
            raise InvalidInput("container part exceeds the size limit")
    return z


def _read_part(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        with z.open(name) as f:
            raw = f.read(MAX_PART + 1)
    except KeyError:
        raise InvalidInput(f"missing part {name}") from None
    if len(raw) > MAX_PART:
        raise InvalidInput("container part exceeds the size limit")
    return raw


def _has_dtd(raw: bytes) -> bool:
    """Scan the ENTIRE (size-capped) part, in its raw bytes and, for UTF-16/32 parts, in the decoded text."""
    low = raw.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        return True
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff") or b"\x00" in raw[:64]:
        for enc in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
            try:
                t = raw.decode(enc, "ignore").lower()
            except Exception:  # noqa: BLE001
                continue
            if "<!doctype" in t or "<!entity" in t:
                return True
    return False


def _parse(raw: bytes) -> ET.Element:
    if _has_dtd(raw):
        raise InvalidInput("XML with a DTD / entity declaration is refused")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise InvalidInput(f"malformed XML: {e}") from None


def validate_container(data: bytes, kind: str) -> None:
    """Raise InvalidInput unless ``data`` is a non-empty, well-formed OOXML package of ``kind``."""
    if not data:
        raise InvalidInput("empty file")
    if data[:4] != b"PK\x03\x04":
        raise InvalidInput(f"not a {kind}: missing ZIP magic")
    z = _zip(data)
    names = set(z.namelist())
    if "[Content_Types].xml" not in names:
        raise InvalidInput("missing [Content_Types].xml")
    main = MAIN_PART[kind]
    if main not in names:
        raise InvalidInput(f"missing main part {main}")
    _parse(_read_part(z, main))


def validate_pdf(data: bytes) -> None:
    if not data:
        raise InvalidInput("empty file")
    if not data.startswith(b"%PDF-"):
        raise InvalidInput("not a PDF: missing %PDF- header")
    if b"%%EOF" not in data[-2048:]:
        raise InvalidInput("PDF is truncated: no %%EOF trailer")


def validate_output(data: bytes, fmt: str) -> None:
    fmt = fmt.lower()
    if fmt == "pdf":
        validate_pdf(data)
    elif fmt in KIND_BY_EXT:
        validate_container(data, fmt)
    elif fmt in ("txt", "csv", "html"):
        if not data:
            raise InvalidInput("empty file")
    elif fmt in ("odt", "ods", "odp", "rtf"):
        if not data:
            raise InvalidInput("empty file")
        if fmt != "rtf" and data[:4] != b"PK\x03\x04":
            raise InvalidInput(f"not a {fmt}: missing ZIP magic")
    else:
        raise InvalidInput(f"unsupported output format {fmt!r}")


# ── docx ───────────────────────────────────────────────────────────────────────────────────────────────────
def docx_paragraphs(data: bytes, *, include_deleted: bool = False) -> "list[str]":
    """Visible paragraph texts of word/document.xml (body order). Deleted-by-revision text (w:delText) is skipped
    unless ``include_deleted``; inserted text counts."""
    z = _zip(data)
    root = _parse(_read_part(z, "word/document.xml"))
    body = root.find(f"{W}body")
    out: list[str] = []
    if body is None:
        return out
    for p in body.iter(f"{W}p"):
        parts: list[str] = []
        for el in p.iter():
            if el.tag == f"{W}t" and el.text:
                parts.append(el.text)
            elif el.tag == f"{W}delText" and include_deleted and el.text:
                parts.append(el.text)
            elif el.tag in (f"{W}tab",):
                parts.append("\t")
        out.append("".join(parts))
    return out


def docx_revisions(data: bytes) -> dict:
    z = _zip(data)
    root = _parse(_read_part(z, "word/document.xml"))
    return {"insertions": len(list(root.iter(f"{W}ins"))), "deletions": len(list(root.iter(f"{W}del"))),
            "content_controls": len(list(root.iter(f"{W}sdt")))}


# ── xlsx ───────────────────────────────────────────────────────────────────────────────────────────────────
_CELL = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")


def valid_cell_ref(ref: str) -> bool:
    return bool(_CELL.match(ref or ""))


def xlsx_cell(data: bytes, cell: str, sheet: int = 1) -> Optional[str]:
    """Value of ``cell`` on worksheet number ``sheet`` (1-based) or None if empty."""
    z = _zip(data)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = _parse(_read_part(z, "xl/sharedStrings.xml"))
        for si in root.iter(f"{S}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
    root = _parse(_read_part(z, f"xl/worksheets/sheet{sheet}.xml"))
    for c in root.iter(f"{S}c"):
        if c.get("r") == cell:
            v = c.find(f"{S}v")
            if c.get("t") == "inlineStr":
                return "".join(t.text or "" for t in c.iter(f"{S}t"))
            if v is None or v.text is None:
                return None
            return shared[int(v.text)] if c.get("t") == "s" else v.text
    return None


def describe(data: bytes, filename: str) -> dict:
    """Structural facts about the file (no content)."""
    ext = ext_of(filename)
    info: dict = {"format": ext, "size": len(data)}
    if ext in KIND_BY_EXT:
        validate_container(data, ext)
        z = _zip(data)
        info["parts"] = len(z.namelist())
        if ext == "docx":
            paras = docx_paragraphs(data)
            info.update(paragraphs=len(paras), characters=sum(len(p) for p in paras), **docx_revisions(data))
    return info


# ── out-of-scope parts: headers, footers, notes, comments, relationships ───────────────────────────────────────
_AUX = re.compile(r"^word/(header|footer|comments|footnotes|endnotes)[^/]*\.xml$")
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def docx_aux_texts(data: bytes) -> "list[str]":
    """Sorted, non-empty texts of header/footer/comment/footnote/endnote parts (part names are not compared: an
    editor may renumber them)."""
    z = _zip(data)
    out = []
    for name in sorted(z.namelist()):
        if _AUX.match(name):
            root = _parse(_read_part(z, name))
            t = "\n".join("".join(x.text or "" for x in p.iter(f"{W}t")) for p in root.iter(f"{W}p")).strip()
            if t:
                out.append(t)
    return sorted(out)


def external_targets(data: bytes) -> "list[str]":
    """Every EXTERNAL relationship target (hyperlinks, linked images, remote templates...) in the package."""
    z = _zip(data)
    out = []
    for name in z.namelist():
        if name.endswith(".rels"):
            root = _parse(_read_part(z, name))
            for r in root.iter(f"{REL}Relationship"):
                if (r.get("TargetMode") or "").lower() == "external":
                    out.append(r.get("Target") or "")
    return sorted(out)
