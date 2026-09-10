"""File-format family App adapter (W3) — the ``file`` provider, ``FILE_NATIVE``.

The LOCAL counterpart to the cloud App adapters (:mod:`agentic_os.integrations.google_app`,
:mod:`agentic_os.integrations.microsoft_app`). Where those act on a provider's documents through a
REST/Graph API under an OAuth token, this adapter acts on a **local file path** passed in the request.
That is the key difference and it shapes the whole module:

* there is **no OAuth, no cloud, no CredentialRef** — ``connect()`` is a no-op that returns connected
  (there is no token to resolve, no secret to carry),
* an operation reads/writes a file at ``request["path"]`` directly, dispatched on its extension, and
* it is **stdlib-only** (``csv`` / ``zipfile`` / ``xml.etree.ElementTree`` / ``pathlib``) — no
  third-party file-format library.

It satisfies the :class:`~agentic_os.integrations.execution.AdapterPort` structurally (``provider`` /
``capabilities`` / ``connect`` / ``execute`` / ``observe`` / ``health``) so the W4 runner drives it with
no import coupling, and it mirrors the cloud adapters' STRUCTURE — a client seam (:class:`FileFormatEngine`
``Protocol`` + an injectable default :class:`StdlibFileEngine`), a ``*_adapter()`` factory, capability
objects carrying ``.name`` / ``.write``, writes that refuse when ``envelope is None`` (a local file *write*
is still a governed, consequential action), and ``observe()`` that reconciles by re-reading.

Capabilities implemented (LIVE) and the formats each supports:

* ``sheet.read``   — CSV (``csv``) and XLSX (parse the zip's ``xl/worksheets`` + ``xl/sharedStrings``).
* ``sheet.write``  — CSV (write a grid). **XLSX write is deferred** (a faithful stdlib xlsx author is not
  practical) — a clear error, left for a follow-on richer-format engine.
* ``document.read``   — ``.md`` / ``.txt`` (text) and ``.docx`` (extract ``<w:t>`` runs from the zip's
  ``word/document.xml``).
* ``document.create`` — ``.md`` / ``.txt`` (write text). **DOCX create is deferred** (fidelity needs the
  optional engine) — a clear error, left for a follow-on.

``document.edit`` and the ``slides.*`` family stay PLANNED for ``file`` (see :mod:`.productivity`). An
unknown or unsupported ``(capability, extension)`` pair returns a clear error *result*, never a crash.
"""
from __future__ import annotations

import csv as _csv
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple
from xml.etree import ElementTree as ET

# The provider-independent logical capability ids this adapter fulfils (W0 contracts).
from .productivity import DocCapability

#: The file extensions each operation supports, by (capability). Membership here is what the adapter
#: dispatches on; a path whose extension is absent gets a clear "unsupported" result. XLSX-write and
#: DOCX-create are deliberately absent (deferred to a follow-on richer-format engine — see module docstring).
_SHEET_READ_EXTS: Tuple[str, ...] = (".csv", ".xlsx")
_SHEET_WRITE_EXTS: Tuple[str, ...] = (".csv",)          # xlsx write deferred
_DOC_READ_EXTS: Tuple[str, ...] = (".md", ".txt", ".docx")
_DOC_CREATE_EXTS: Tuple[str, ...] = (".md", ".txt")     # docx create deferred


class FileFormatError(Exception):
    """A file operation could not be fulfilled (unsupported format, malformed file, or a deferred
    format). Surfaced as ``ok=False`` / ``found=False`` — never raised through ``execute``/``observe``."""


# ── The result / observation / connection / health shapes (self-contained, attribute-read) ──
@dataclass(frozen=True)
class AppCapability:
    """One capability the adapter advertises. Carries ``.name`` and ``.write`` (what the W4
    runner's ``_is_write`` reads) plus a coarse risk ``tier``."""

    name: str
    write: bool = False
    tier: int = 0


@dataclass(frozen=True)
class FileResult:
    """The normalized outcome of one ``execute``. ``provider_object_id`` is the (absolute) file
    path verification re-observes. No token/secret is ever involved (local files, no auth)."""

    ok: bool
    capability: str
    provider_object_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class FileObservation:
    resource_ref: str
    found: bool = False
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileConnection:
    provider: str
    connected: bool
    detail: str = ""


@dataclass(frozen=True)
class FileHealth:
    healthy: bool
    detail: str = ""


# ── The engine seam (a Protocol) — tests may drive a fake; production uses the stdlib engine ──
class FileFormatEngine(Protocol):
    """The deterministic seam over the local file formats. ``read_sheet`` returns a list-of-rows
    (each row a list of string cells); ``read_document`` returns text. Writers create/overwrite the
    file at ``path``. Each dispatches on the file extension and raises :class:`FileFormatError` for
    an unsupported or deferred format."""

    def read_sheet(self, path: str) -> List[List[str]]: ...
    def write_sheet(self, path: str, values: Sequence[Sequence[Any]]) -> None: ...
    def read_document(self, path: str) -> str: ...
    def create_document(self, path: str, text: str) -> None: ...


# ── XML / spreadsheet helpers (namespace-agnostic — match on local names) ────────────────────
def _local(tag: str) -> str:
    """The local name of a (possibly namespaced) ElementTree tag — ``{ns}c`` → ``c``."""
    return tag.rsplit("}", 1)[-1]


def _column_index(cell_ref: str) -> int:
    """0-based column index from an A1 cell reference (``"B2"`` → 1, ``"AA1"`` → 26)."""
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx - 1 if idx > 0 else 0


def _read_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    """The shared-string table: each ``<si>`` flattened to the concatenation of its ``<t>`` runs."""
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: List[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        out.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    return out


def _read_xlsx(path: str) -> List[List[str]]:
    """Parse the first worksheet of an XLSX into a row/col grid of strings (stdlib zip + XML)."""
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _read_shared_strings(zf)
            names = zf.namelist()
            sheet = "xl/worksheets/sheet1.xml"
            if sheet not in names:
                worksheets = sorted(n for n in names
                                    if n.startswith("xl/worksheets/") and n.endswith(".xml"))
                if not worksheets:
                    raise FileFormatError("xlsx has no worksheet part")
                sheet = worksheets[0]
            root = ET.fromstring(zf.read(sheet))
    except zipfile.BadZipFile as e:
        raise FileFormatError(f"not a valid xlsx (zip) file: {e}") from e
    except ET.ParseError as e:
        raise FileFormatError(f"malformed xlsx worksheet XML: {e}") from e

    sheet_data = next((e for e in root.iter() if _local(e.tag) == "sheetData"), None)
    if sheet_data is None:
        return []
    rows: List[List[str]] = []
    for row in sheet_data:
        if _local(row.tag) != "row":
            continue
        cells: Dict[int, str] = {}
        max_col = -1
        fallback_col = 0
        for c in row:
            if _local(c.tag) != "c":
                continue
            ref = c.get("r", "")
            col = _column_index(ref) if ref else fallback_col
            fallback_col = col + 1
            cell_type = c.get("t", "")
            value: Optional[str] = None
            for child in c:
                lt = _local(child.tag)
                if lt == "v":
                    value = child.text or ""
                elif lt == "is":  # inline string
                    value = "".join(t.text or "" for t in child.iter() if _local(t.tag) == "t")
            if cell_type == "s" and value is not None:  # shared-string index
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    value = ""
            cells[col] = value if value is not None else ""
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)])
    return rows


def _read_docx(path: str) -> str:
    """Extract the document text of a DOCX: ``<w:t>`` runs joined per ``<w:p>`` paragraph."""
    try:
        with zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                raise FileFormatError("docx has no word/document.xml part")
            root = ET.fromstring(zf.read("word/document.xml"))
    except zipfile.BadZipFile as e:
        raise FileFormatError(f"not a valid docx (zip) file: {e}") from e
    except ET.ParseError as e:
        raise FileFormatError(f"malformed docx document XML: {e}") from e
    paragraphs: List[str] = []
    for p in root.iter():
        if _local(p.tag) != "p":
            continue
        paragraphs.append("".join(t.text or "" for t in p.iter() if _local(t.tag) == "t"))
    return "\n".join(paragraphs)


@dataclass
class StdlibFileEngine:
    """The default :class:`FileFormatEngine` — pure standard library. CSV via :mod:`csv`, XLSX and
    DOCX via :mod:`zipfile` + :mod:`xml.etree.ElementTree`. Deferred formats raise a clear
    :class:`FileFormatError` (XLSX write, DOCX create) rather than a fragile hand-rolled author."""

    def read_sheet(self, path: str) -> List[List[str]]:
        ext = Path(path).suffix.lower()
        if ext == ".csv":
            with open(path, "r", newline="", encoding="utf-8") as fh:
                return [list(row) for row in _csv.reader(fh)]
        if ext == ".xlsx":
            return _read_xlsx(path)
        raise FileFormatError(
            f"file provider cannot read a sheet from {ext!r} files (supported: .csv, .xlsx)")

    def write_sheet(self, path: str, values: Sequence[Sequence[Any]]) -> None:
        ext = Path(path).suffix.lower()
        if ext == ".csv":
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = _csv.writer(fh)
                for row in values:
                    writer.writerow(list(row))
            return
        if ext == ".xlsx":
            raise FileFormatError(
                "xlsx write needs the optional file-formats engine (stdlib cannot faithfully "
                "author xlsx); write .csv, or use the follow-on richer-format engine")
        raise FileFormatError(
            f"file provider cannot write a sheet to {ext!r} files (supported: .csv)")

    def read_document(self, path: str) -> str:
        ext = Path(path).suffix.lower()
        if ext in (".md", ".txt"):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        if ext == ".docx":
            return _read_docx(path)
        raise FileFormatError(
            f"file provider cannot read a document from {ext!r} files (supported: .md, .txt, .docx)")

    def create_document(self, path: str, text: str) -> None:
        ext = Path(path).suffix.lower()
        if ext in (".md", ".txt"):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return
        if ext == ".docx":
            raise FileFormatError(
                "docx create needs the optional file-formats engine (stdlib cannot faithfully "
                "author docx); create .md/.txt, or use the follow-on richer-format engine")
        raise FileFormatError(
            f"file provider cannot create a document of {ext!r} (supported: .md, .txt)")


@dataclass
class FileFormatAdapter:
    """The ``file`` provider App adapter — LOCAL file manipulation, no auth. ``engine`` is the
    injectable :class:`FileFormatEngine` (defaults to the stdlib engine). Writes execute under a
    :class:`~agentic_os.integrations.execution.GovernedEnvelope` (a local write is still governed);
    the adapter refuses a write with no envelope."""

    provider: str = "file"
    engine: FileFormatEngine = field(default_factory=StdlibFileEngine)

    # ── the advertised surface (W0 logical ids; .name + .write are what the runner reads) ──
    def capabilities(self) -> Tuple[AppCapability, ...]:
        return (
            AppCapability(DocCapability.SHEET_READ.value, write=False, tier=0),
            AppCapability(DocCapability.SHEET_WRITE.value, write=True, tier=2),
            AppCapability(DocCapability.DOCUMENT_READ.value, write=False, tier=0),
            AppCapability(DocCapability.DOCUMENT_CREATE.value, write=True, tier=2),
        )

    def _capability(self, name: str) -> Optional[AppCapability]:
        for c in self.capabilities():
            if c.name == name:
                return c
        return None

    # ── connect: a NO-OP — no OAuth, no token, no CredentialRef (local files) ─────────────
    def connect(self, config: Any, credential_ref: str = "") -> FileConnection:
        return FileConnection(self.provider, True, "no auth required (local file access)")

    # ── execute (writes refuse without an envelope) ──────────────────────────────────────
    def execute(self, capability: str, request: Any, envelope: Optional[object]) -> FileResult:
        cap = self._capability(capability)
        if cap is None:
            return FileResult(False, capability, error=f"file does not implement {capability!r}")
        if cap.write and envelope is None:
            return FileResult(False, capability,
                              error="execution envelope required for a consequential (write) capability")
        req: Dict[str, Any] = dict(request or {})
        path = str(req.get("path", ""))
        if not path:
            return FileResult(False, capability, error="request is missing 'path'")
        try:
            if capability == DocCapability.SHEET_READ.value:
                rows = self.engine.read_sheet(path)
                return FileResult(True, capability, provider_object_id=self._ref(path),
                                  data={"values": rows, "path": path})
            if capability == DocCapability.SHEET_WRITE.value:
                values = [list(r) for r in (req.get("values") or [])]
                self.engine.write_sheet(path, values)
                return FileResult(True, capability, provider_object_id=self._ref(path),
                                  data={"path": path, "rows_written": len(values)})
            if capability == DocCapability.DOCUMENT_READ.value:
                text = self.engine.read_document(path)
                return FileResult(True, capability, provider_object_id=self._ref(path),
                                  data={"text": text, "path": path})
            if capability == DocCapability.DOCUMENT_CREATE.value:
                text = str(req.get("text", req.get("content", "")))
                self.engine.create_document(path, text)
                return FileResult(True, capability, provider_object_id=self._ref(path),
                                  data={"path": path, "bytes_written": len(text.encode("utf-8"))})
        except FileFormatError as e:  # unsupported/deferred/malformed → a clear result, never a crash
            return FileResult(False, capability, error=str(e))
        except OSError as e:  # missing file, permission, … → a result
            return FileResult(False, capability, error=f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — any parse failure is a result, not an exception
            return FileResult(False, capability, error=f"{type(e).__name__}: {e}")
        return FileResult(False, capability, error="unhandled capability")

    @staticmethod
    def _ref(path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return path

    # ── observe: reconcile by re-reading — found ⇔ the file exists AND parses ──────────────
    def observe(self, resource_ref: str) -> FileObservation:
        if not resource_ref:
            return FileObservation(resource_ref, found=False)
        p = Path(resource_ref)
        if not p.exists() or not p.is_file():
            return FileObservation(resource_ref, found=False)
        ext = p.suffix.lower()
        try:
            if ext in _SHEET_READ_EXTS:
                self.engine.read_sheet(resource_ref)
            elif ext in _DOC_READ_EXTS:
                self.engine.read_document(resource_ref)
            else:
                # a format the adapter cannot parse: existence is all we can attest to
                return FileObservation(resource_ref, found=True, data={"exists": True, "parsed": False})
        except Exception:  # noqa: BLE001 — a malformed file is "not reconciled", not a raise
            return FileObservation(resource_ref, found=False)
        return FileObservation(resource_ref, found=True, data={"exists": True, "parsed": True})

    # ── health: stdlib-only, no external dependency ⇒ always ready ────────────────────────
    def health(self) -> FileHealth:
        return FileHealth(True, "ok")


def file_format_adapter(engine: Optional[FileFormatEngine] = None) -> FileFormatAdapter:
    """Build a :class:`FileFormatAdapter` (the ``file`` provider). No credential ref, no resolver —
    local file access needs no auth. Pass ``engine`` to inject a richer-format engine later."""
    return FileFormatAdapter(engine=engine or StdlibFileEngine())
