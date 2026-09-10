"""LibreOffice App adapter (W4) — the ``libreoffice`` provider, ``LOCAL_HEADLESS``.

A second LOCAL adapter (the cloud counterparts are :mod:`agentic_os.integrations.google_app` and
:mod:`agentic_os.integrations.microsoft_app`; the sibling LOCAL one is
:mod:`agentic_os.integrations.file_formats`, ``FILE_NATIVE``). Where ``file`` parses/writes formats
in pure stdlib, this adapter drives a **headless office engine** on the box
(``soffice``/``libreoffice --headless``). It shares the LOCAL shape with ``file``:

* there is **no OAuth, no cloud, no CredentialRef** — ``connect()`` is a no-op that only checks the
  engine is installed (there is no token to resolve, no secret to carry),
* an operation acts on a **local file path** passed in the request, and
* it is **stdlib-only** (:mod:`subprocess` / :mod:`shutil` / :mod:`pathlib` / :mod:`tempfile`) — the
  office logic lives in the external engine, not in a third-party Python library.

Its superpower is **format conversion**, which fills exactly the authoring gaps W3 deferred: ``file``
can write CSV but not XLSX, and ``.md``/``.txt`` but not DOCX/PDF. LibreOffice authors all of those by
round-tripping a simple source through ``--convert-to``:

* ``sheet.write``     — author a real **XLSX** from CSV content (rows → temp ``.csv`` → ``--convert-to
  xlsx``). This is the honest fix for W3's deferred XLSX write.
* ``document.create`` — author **DOCX or PDF** from text/markdown/html input (input → temp source →
  ``--convert-to docx|pdf``; the target is read off the destination extension).
* ``slides.render``   — render an existing presentation/document to **PDF** (``--convert-to pdf``). A
  read/export: it does not mutate the source, so (like W3's read/render) it needs no envelope.

The two authoring capabilities are **writes**: they refuse when ``envelope is None`` (a local write is
still a governed, consequential action). ``slides.render`` is non-mutating and runs without one.

**Deferred to the UNO bridge (PLANNED):** ``document.edit``, cell-level ``sheet`` editing, and
``slides.create``/``slides.read`` content authoring need python-uno + a running ``soffice`` socket
(the scripting bridge), not one-shot ``--convert-to``. They stay PLANNED for ``libreoffice`` — no UNO
bridge is built here.

An unknown or unsupported ``(capability, target)`` returns a clear error *result*, never a crash.

**Path note (headless engine confinement):** the engine acts on real paths on disk, and a
sandboxed/snap-packaged ``soffice`` can only reach paths inside the user's home. The adapter therefore
places its temp *source* file in the **same directory as the requested destination** ``path`` — the
caller's chosen location — rather than a private ``/tmp`` the engine may not be allowed to read.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple

# The provider-independent logical capability ids this adapter fulfils (W0 contracts).
from .productivity import DocCapability

#: How long a single headless conversion may take before we give up (seconds). LibreOffice's first
#: cold start (profile bootstrap) is slow; keep this generous so a real conversion is not flaky.
_DEFAULT_TIMEOUT_S: float = 180.0

#: The destination extensions ``document.create`` will author (target read off the ``path`` suffix).
_DOC_CREATE_TARGETS: Tuple[str, ...] = ("docx", "pdf")
#: The source formats ``document.create`` accepts as input (temp source is written with this suffix).
_DOC_SOURCE_EXTS: Tuple[str, ...] = ("txt", "md", "html", "htm")


class LibreOfficeError(Exception):
    """A LibreOffice operation could not be fulfilled (engine absent, conversion failed/timed out, or
    an unsupported target). Surfaced as ``ok=False`` / ``found=False`` — never raised through
    ``execute``/``observe``."""


# ── The result / observation / connection / health shapes (self-contained, attribute-read) ──
@dataclass(frozen=True)
class AppCapability:
    """One capability the adapter advertises. Carries ``.name`` and ``.write`` (what the runner's
    ``_is_write`` reads) plus a coarse risk ``tier``."""

    name: str
    write: bool = False
    tier: int = 0


@dataclass(frozen=True)
class LibreOfficeResult:
    """The normalized outcome of one ``execute``. ``provider_object_id`` is the (absolute) output
    file path verification re-observes. No token/secret is ever involved (local engine, no auth)."""

    ok: bool
    capability: str
    provider_object_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class LibreOfficeObservation:
    resource_ref: str
    found: bool = False
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LibreOfficeConnection:
    provider: str
    connected: bool
    detail: str = ""


@dataclass(frozen=True)
class LibreOfficeHealth:
    healthy: bool
    detail: str = ""


def _find_binary() -> Optional[str]:
    """Locate the headless office binary, preferring ``libreoffice`` then ``soffice``."""
    return shutil.which("libreoffice") or shutil.which("soffice")


# ── The engine seam (a Protocol) — tests inject a fake; production shells out to soffice ──
class LibreOfficeEngine(Protocol):
    """The deterministic seam over the headless office engine. ``convert`` turns the file at
    ``src_path`` into a ``target_ext`` document written into ``outdir`` and returns the output path;
    it raises :class:`LibreOfficeError` when the engine is absent or the conversion yields no file.
    ``available()`` reports whether the local engine is installed (drives ``connect``/``health``)."""

    def available(self) -> bool: ...
    def convert(self, src_path: str, target_ext: str, outdir: str) -> str: ...


@dataclass
class SofficeEngine:
    """The default :class:`LibreOfficeEngine` — shells out to the headless binary
    (``libreoffice``/``soffice``). Each conversion runs with an **isolated user profile** inside
    ``outdir`` (``-env:UserInstallation=...``) so concurrent conversions do not collide on the
    single-instance profile lock. ``binary`` is resolved lazily via :func:`shutil.which` unless
    injected; ``timeout`` bounds a single conversion."""

    binary: Optional[str] = None
    timeout: float = _DEFAULT_TIMEOUT_S

    def available(self) -> bool:
        return (self.binary or _find_binary()) is not None

    def _resolve_binary(self) -> str:
        binary = self.binary or _find_binary()
        if not binary:
            raise LibreOfficeError(
                "LibreOffice is not installed — LOCAL_HEADLESS needs the local engine "
                "(install libreoffice/soffice, or inject a LibreOfficeEngine)")
        return binary

    def convert(self, src_path: str, target_ext: str, outdir: str) -> str:
        binary = self._resolve_binary()
        ext = target_ext.lstrip(".").lower()
        out_dir = Path(outdir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Isolated profile inside outdir (an engine-reachable, caller-chosen location) so parallel
        # headless runs don't fight over the default single-instance lock.
        profile = out_dir / ".lo_profile"
        cmd = [
            binary, "--headless", "--norestore", "--nologo", "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", ext, "--outdir", str(out_dir), str(src_path),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise LibreOfficeError(
                f"LibreOffice conversion to {ext!r} timed out after {self.timeout:g}s") from e
        except OSError as e:
            raise LibreOfficeError(f"could not launch LibreOffice ({binary}): {e}") from e
        out_path = out_dir / (Path(src_path).stem + "." + ext)
        if not out_path.exists() or out_path.stat().st_size == 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise LibreOfficeError(
                f"LibreOffice produced no {ext!r} output for {src_path!r} "
                f"(exit {proc.returncode}): {detail[:400]}")
        return str(out_path)


@dataclass
class LibreOfficeAdapter:
    """The ``libreoffice`` provider App adapter — LOCAL headless conversion, no auth. ``engine`` is
    the injectable :class:`LibreOfficeEngine` (defaults to :class:`SofficeEngine`). The authoring
    writes (``sheet.write`` / ``document.create``) execute under a
    :class:`~agentic_os.integrations.execution.GovernedEnvelope` (a local write is still governed);
    the adapter refuses them with no envelope. ``slides.render`` is a non-mutating export and runs
    without one."""

    provider: str = "libreoffice"
    engine: LibreOfficeEngine = field(default_factory=SofficeEngine)

    # ── the advertised (LIVE) surface. document.edit / slides.create / slides.read need the UNO
    #    bridge and stay PLANNED (see module docstring) — they are deliberately NOT advertised here. ──
    def capabilities(self) -> Tuple[AppCapability, ...]:
        return (
            AppCapability(DocCapability.SHEET_WRITE.value, write=True, tier=2),
            AppCapability(DocCapability.DOCUMENT_CREATE.value, write=True, tier=2),
            AppCapability(DocCapability.SLIDES_RENDER.value, write=False, tier=1),
        )

    def _capability(self, name: str) -> Optional[AppCapability]:
        for c in self.capabilities():
            if c.name == name:
                return c
        return None

    # ── connect: a NO-OP — no OAuth, no token; only checks the engine is installed ─────────
    def connect(self, config: Any, credential_ref: str = "") -> LibreOfficeConnection:
        if self.engine.available():
            return LibreOfficeConnection(self.provider, True, "no auth required (local headless engine)")
        return LibreOfficeConnection(
            self.provider, False,
            "LibreOffice is not installed — LOCAL_HEADLESS needs the local engine")

    # ── execute (authoring writes refuse without an envelope; render does not) ─────────────
    def execute(self, capability: str, request: Any, envelope: Optional[object]) -> LibreOfficeResult:
        cap = self._capability(capability)
        if cap is None:
            return LibreOfficeResult(
                False, capability,
                error=f"libreoffice does not implement {capability!r} "
                      f"(document.edit / slides.create / slides.read need the UNO bridge — planned)")
        if cap.write and envelope is None:
            return LibreOfficeResult(
                False, capability,
                error="execution envelope required for a consequential (write) capability")
        req: Dict[str, Any] = dict(request or {})
        path = str(req.get("path", ""))
        if not path:
            return LibreOfficeResult(False, capability, error="request is missing 'path'")
        try:
            if capability == DocCapability.SHEET_WRITE.value:
                return self._author_sheet(path, req)
            if capability == DocCapability.DOCUMENT_CREATE.value:
                return self._author_document(path, req)
            if capability == DocCapability.SLIDES_RENDER.value:
                return self._render(path, req)
        except LibreOfficeError as e:  # unsupported/failed conversion → a clear result, never a crash
            return LibreOfficeResult(False, capability, error=str(e))
        except OSError as e:  # missing source, permission, … → a result
            return LibreOfficeResult(False, capability, error=f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — any failure is a result, not an exception
            return LibreOfficeResult(False, capability, error=f"{type(e).__name__}: {e}")
        return LibreOfficeResult(False, capability, error="unhandled capability")

    # ── sheet.write: rows/CSV → temp .csv → --convert-to xlsx → destination ────────────────
    def _author_sheet(self, path: str, req: Dict[str, Any]) -> LibreOfficeResult:
        dest = Path(path)
        target = dest.suffix.lstrip(".").lower() or "xlsx"
        if target != "xlsx":
            raise LibreOfficeError(
                f"libreoffice sheet.write authors .xlsx (got {dest.suffix!r}); "
                f"set an .xlsx destination path")
        csv_text = self._csv_text(req)
        out_path = self._convert_from_source(dest, csv_text, "csv", "xlsx")
        rows = req.get("values") or req.get("rows")
        return LibreOfficeResult(
            True, DocCapability.SHEET_WRITE.value, provider_object_id=out_path,
            data={"path": out_path, "target": "xlsx",
                  "rows_written": (len(list(rows)) if rows is not None else None)})

    # ── document.create: text/md/html → temp source → --convert-to docx|pdf → destination ──
    def _author_document(self, path: str, req: Dict[str, Any]) -> LibreOfficeResult:
        dest = Path(path)
        target = dest.suffix.lstrip(".").lower()
        if target not in _DOC_CREATE_TARGETS:
            raise LibreOfficeError(
                f"libreoffice document.create authors {_DOC_CREATE_TARGETS} (got {dest.suffix!r}); "
                f"set a .docx or .pdf destination path")
        text = str(req.get("text", req.get("content", "")))
        source_ext = str(req.get("source_format", req.get("source_ext", "txt"))).lstrip(".").lower()
        if source_ext not in _DOC_SOURCE_EXTS:
            raise LibreOfficeError(
                f"libreoffice document.create takes {_DOC_SOURCE_EXTS} input (got {source_ext!r})")
        out_path = self._convert_from_source(dest, text, source_ext, target)
        return LibreOfficeResult(
            True, DocCapability.DOCUMENT_CREATE.value, provider_object_id=out_path,
            data={"path": out_path, "target": target, "source_format": source_ext,
                  "bytes_written": len(text.encode("utf-8"))})

    # ── slides.render: an existing source doc → --convert-to pdf (export, no mutation) ─────
    def _render(self, path: str, req: Dict[str, Any]) -> LibreOfficeResult:
        src = Path(path)
        if not src.exists() or not src.is_file():
            raise LibreOfficeError(f"slides.render source not found: {path!r}")
        # Optional explicit destination; default a .pdf beside the source.
        out_req = str(req.get("output", req.get("out_path", "")))
        outdir = str(Path(out_req).parent) if out_req else str(src.parent)
        produced = self.engine.convert(str(src), "pdf", outdir)
        final = self._finalize(produced, out_req) if out_req else produced
        return LibreOfficeResult(
            True, DocCapability.SLIDES_RENDER.value, provider_object_id=final,
            data={"path": final, "target": "pdf", "source": str(src)})

    # ── shared authoring helper: write a temp source beside the destination, convert, place ──
    def _convert_from_source(self, dest: Path, source_text: str, source_ext: str,
                             target_ext: str) -> str:
        parent = dest.parent if str(dest.parent) else Path(".")
        parent.mkdir(parents=True, exist_ok=True)
        # The temp source shares the destination's directory so a confined (snap) engine can read it.
        workdir = tempfile.mkdtemp(prefix=".lo_", dir=str(parent))
        try:
            src_path = Path(workdir) / f"source.{source_ext}"
            src_path.write_text(source_text, encoding="utf-8")
            produced = self.engine.convert(str(src_path), target_ext, workdir)
            return self._finalize(produced, str(dest))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _finalize(produced: str, dest: str) -> str:
        """Move the engine's output (named after the temp source) to the caller's destination."""
        produced_p = Path(produced)
        dest_p = Path(dest)
        if produced_p.resolve() == dest_p.resolve():
            return LibreOfficeAdapter._ref(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced_p), str(dest_p))
        return LibreOfficeAdapter._ref(str(dest_p))

    @staticmethod
    def _csv_text(req: Dict[str, Any]) -> str:
        """CSV text for the temp source — from an explicit ``csv``/``content`` string, or built from
        a ``values``/``rows`` grid with :mod:`csv` (quoting handled)."""
        import csv as _csv
        import io
        raw = req.get("csv", req.get("content"))
        if isinstance(raw, str) and raw:
            return raw
        rows = req.get("values") or req.get("rows") or []
        buf = io.StringIO()
        writer = _csv.writer(buf)
        for row in rows:
            writer.writerow(list(row))
        return buf.getvalue()

    @staticmethod
    def _ref(path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return path

    # ── observe: reconcile — found ⇔ the output file exists AND is non-empty ────────────────
    def observe(self, resource_ref: str) -> LibreOfficeObservation:
        if not resource_ref:
            return LibreOfficeObservation(resource_ref, found=False)
        p = Path(resource_ref)
        if not p.exists() or not p.is_file():
            return LibreOfficeObservation(resource_ref, found=False)
        size = p.stat().st_size
        if size == 0:
            return LibreOfficeObservation(resource_ref, found=False, data={"exists": True, "size": 0})
        return LibreOfficeObservation(
            resource_ref, found=True, data={"exists": True, "size": size})

    # ── health: reports whether the local engine is installed ──────────────────────────────
    def health(self) -> LibreOfficeHealth:
        if self.engine.available():
            return LibreOfficeHealth(True, "libreoffice/soffice present")
        return LibreOfficeHealth(
            False, "LibreOffice is not installed — LOCAL_HEADLESS needs the local engine")


def libreoffice_adapter(engine: Optional[LibreOfficeEngine] = None) -> LibreOfficeAdapter:
    """Build a :class:`LibreOfficeAdapter` (the ``libreoffice`` provider). No credential ref, no
    resolver — the local headless engine needs no auth. Pass ``engine`` to inject a fake in tests or
    a preconfigured :class:`SofficeEngine` (custom binary/timeout) in production."""
    return LibreOfficeAdapter(engine=engine or SofficeEngine())
