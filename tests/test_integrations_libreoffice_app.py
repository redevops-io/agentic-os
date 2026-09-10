"""W4 — the LibreOffice App adapter (``libreoffice`` provider, ``LOCAL_HEADLESS``).

Two layers:

* **Unit (a fake engine, always runs)** — a :class:`LibreOfficeEngine` whose ``convert()`` just writes
  a stub output file. Asserts the adapter dispatches each capability to a convert with the right target
  extension, returns the output path as ``provider_object_id``, refuses the authoring writes
  (``sheet.write`` / ``document.create``) without an envelope, renders without one, observes the
  output, and reflects the (mocked) engine presence in ``connect``/``health``. No soffice needed.
* **Real smoke (guarded)** — skipped unless ``libreoffice``/``soffice`` is installed; here it IS, so it
  runs: a real CSV → XLSX via ``sheet.write`` (a valid zip whose namelist includes ``xl/workbook.xml``)
  and a real text → PDF via ``document.create`` / ``slides.render`` (a ``%PDF`` magic header).

The real conversions use a workspace under ``Path.home()``: a sandboxed/snap-packaged ``soffice`` can
only reach paths inside the user's home, so a ``/tmp``-based ``tmp_path`` would not be readable by the
engine (the adapter co-locates its temp source with the destination for exactly this reason).
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

import pytest

from agentic_os.integrations.execution import GovernedEnvelope
from agentic_os.integrations.libreoffice_app import (
    LibreOfficeAdapter,
    LibreOfficeError,
    SofficeEngine,
    libreoffice_adapter,
)
from agentic_os.integrations.productivity import default_registry, libreoffice_provider

_HAVE_SOFFICE = bool(shutil.which("libreoffice") or shutil.which("soffice"))


def _envelope(capability: str) -> GovernedEnvelope:
    return GovernedEnvelope(intent_hash="feed" * 16, capability=capability,
                            provider="libreoffice", tier=2)


# ── a fake engine: records each convert and writes a stub output file (no soffice) ───────────
@dataclass
class FakeEngine:
    installed: bool = True
    calls: List[Tuple[str, str, str]] = field(default_factory=list)
    payload: bytes = b"stub-output-bytes"

    def available(self) -> bool:
        return self.installed

    def convert(self, src_path: str, target_ext: str, outdir: str) -> str:
        if not self.installed:
            raise LibreOfficeError("LibreOffice is not installed — LOCAL_HEADLESS needs the local engine")
        self.calls.append((src_path, target_ext, outdir))
        out = Path(outdir) / (Path(src_path).stem + "." + target_ext.lstrip("."))
        out.write_bytes(self.payload)
        return str(out)


# ══════════════════════════════ Unit tests (fake engine) ═════════════════════════════════════
def test_sheet_write_converts_rows_to_xlsx(tmp_path):
    engine = FakeEngine()
    adapter = libreoffice_adapter(engine=engine)
    dest = tmp_path / "report.xlsx"
    grid = [["Name", "Qty"], ["Widget", "7"]]

    res = adapter.execute("sheet.write", {"path": str(dest), "values": grid}, _envelope("sheet.write"))
    assert res.ok and res.data["target"] == "xlsx" and res.data["rows_written"] == 2
    # dispatched a convert to xlsx from a .csv source …
    assert len(engine.calls) == 1
    src, target, _ = engine.calls[0]
    assert target == "xlsx" and Path(src).suffix == ".csv"
    # … and the output landed at the requested destination (provider_object_id)
    assert res.provider_object_id == str(dest.resolve()) and dest.exists()
    assert adapter.observe(res.provider_object_id).found is True


def test_document_create_docx_and_pdf_pick_target_from_destination(tmp_path):
    engine = FakeEngine()
    adapter = libreoffice_adapter(engine=engine)

    docx = adapter.execute("document.create",
                           {"path": str(tmp_path / "memo.docx"), "text": "# Hi\n\nBody"},
                           _envelope("document.create"))
    assert docx.ok and docx.data["target"] == "docx"
    assert engine.calls[-1][1] == "docx"

    pdf = adapter.execute("document.create",
                          {"path": str(tmp_path / "memo.pdf"), "content": "Body",
                           "source_format": "md"},
                          _envelope("document.create"))
    assert pdf.ok and pdf.data["target"] == "pdf" and pdf.data["source_format"] == "md"
    assert engine.calls[-1][1] == "pdf"
    assert Path(pdf.provider_object_id).suffix == ".pdf"


def test_slides_render_exports_pdf_without_an_envelope(tmp_path):
    engine = FakeEngine()
    adapter = libreoffice_adapter(engine=engine)
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"a source presentation")

    # slides.render is a non-mutating export → runs with NO envelope
    res = adapter.execute("slides.render", {"path": str(src)}, envelope=None)
    assert res.ok and res.data["target"] == "pdf"
    assert engine.calls[-1][1] == "pdf"
    assert Path(res.provider_object_id).suffix == ".pdf" and adapter.observe(res.provider_object_id).found


def test_authoring_writes_refuse_without_an_envelope(tmp_path):
    engine = FakeEngine()
    adapter = libreoffice_adapter(engine=engine)

    r1 = adapter.execute("sheet.write", {"path": str(tmp_path / "x.xlsx"), "values": [["a"]]}, None)
    assert not r1.ok and "envelope required" in r1.error
    r2 = adapter.execute("document.create", {"path": str(tmp_path / "x.docx"), "text": "hi"}, None)
    assert not r2.ok and "envelope required" in r2.error
    # nothing was authored and the engine was never driven
    assert engine.calls == []
    assert not (tmp_path / "x.xlsx").exists() and not (tmp_path / "x.docx").exists()


def test_unsupported_capability_and_targets_are_clear_results(tmp_path):
    adapter = libreoffice_adapter(engine=FakeEngine())
    # a UNO-bridge capability the adapter does not implement → a clear result naming UNO
    r1 = adapter.execute("document.edit", {"path": str(tmp_path / "x.docx")}, _envelope("document.edit"))
    assert not r1.ok and "does not implement" in r1.error and "UNO" in r1.error
    # sheet.write to a non-xlsx destination → a clear result
    r2 = adapter.execute("sheet.write", {"path": str(tmp_path / "x.ods"), "values": [["a"]]},
                         _envelope("sheet.write"))
    assert not r2.ok and "author" in r2.error and ".xlsx" in r2.error
    # document.create to an unsupported target → a clear result
    r3 = adapter.execute("document.create", {"path": str(tmp_path / "x.rtf"), "text": "hi"},
                         _envelope("document.create"))
    assert not r3.ok and "docx" in r3.error and "pdf" in r3.error
    # a missing 'path'
    r4 = adapter.execute("sheet.write", {"values": [["a"]]}, _envelope("sheet.write"))
    assert not r4.ok and "missing 'path'" in r4.error
    # slides.render of a non-existent source → a clear result, never a crash
    r5 = adapter.execute("slides.render", {"path": str(tmp_path / "nope.pptx")}, None)
    assert not r5.ok and "not found" in r5.error


def test_capabilities_advertise_write_flags():
    caps = {c.name: c.write for c in LibreOfficeAdapter().capabilities()}
    # exactly the three LIVE caps; the two authoring ones are writes, render is not
    assert caps == {"sheet.write": True, "document.create": True, "slides.render": False}


def test_connect_and_health_reflect_engine_presence(tmp_path):
    up = libreoffice_adapter(engine=FakeEngine(installed=True))
    conn = up.connect({}, "")
    assert conn.provider == "libreoffice" and conn.connected is True
    assert up.health().healthy is True

    down = libreoffice_adapter(engine=FakeEngine(installed=False))
    assert down.connect({}, "").connected is False
    assert down.health().healthy is False
    # with no engine, a write yields a clear "not installed" result (never a crash)
    res = down.execute("document.create", {"path": str(tmp_path / "y.pdf"), "text": "hi"},
                       _envelope("document.create"))
    assert not res.ok and "not installed" in res.error


def test_engine_is_injectable_default_is_soffice():
    assert isinstance(libreoffice_adapter().engine, SofficeEngine)
    fake = FakeEngine()
    assert libreoffice_adapter(engine=fake).engine is fake


def test_observe_missing_or_empty_output(tmp_path):
    adapter = libreoffice_adapter(engine=FakeEngine())
    assert adapter.observe("").found is False
    assert adapter.observe(str(tmp_path / "nope.xlsx")).found is False
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert adapter.observe(str(empty)).found is False  # exists but zero-length ⇒ not reconciled


# ── catalog wiring ───────────────────────────────────────────────────────────────────────────
def test_catalog_libreoffice_provider_is_live_for_its_caps():
    assert libreoffice_provider().is_capability_live("sheet.write") is True
    assert libreoffice_provider().is_capability_live("slides.render") is True
    manifest = default_registry().to_manifest()
    # W4 joins google/microsoft/file as a buildable sheet.write provider …
    assert set(manifest.buildable("sheet.write")) >= {"google", "microsoft", "file", "libreoffice"}
    # … and is the sole live slides.render (PDF export) path
    assert set(manifest.buildable("slides.render")) == {"libreoffice"}
    # document.edit stays planned (UNO)
    assert libreoffice_provider().is_capability_live("document.edit") is False


# ══════════════════════ Real smoke (guarded — runs iff soffice is installed) ═════════════════
@pytest.fixture()
def home_workspace():
    """A scratch dir under ``Path.home()`` — reachable by a sandboxed/snap soffice, unlike /tmp."""
    d = Path(tempfile.mkdtemp(prefix="lo_w4_smoke_", dir=str(Path.home())))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not _HAVE_SOFFICE, reason="LibreOffice not installed")
def test_real_csv_to_xlsx_is_a_valid_workbook(home_workspace):
    adapter = libreoffice_adapter()  # the real SofficeEngine
    dest = home_workspace / "report.xlsx"
    grid = [["Name", "Qty"], ["Widget", "7"], ["Gadget", "12"]]

    res = adapter.execute("sheet.write", {"path": str(dest), "values": grid}, _envelope("sheet.write"))
    assert res.ok, res.error
    out = Path(res.provider_object_id)
    assert out.exists() and out.stat().st_size > 0
    # a real xlsx is a zip carrying the workbook part
    with zipfile.ZipFile(out) as zf:
        assert "xl/workbook.xml" in zf.namelist()
    assert adapter.observe(res.provider_object_id).found is True


@pytest.mark.skipif(not _HAVE_SOFFICE, reason="LibreOffice not installed")
def test_real_text_to_pdf_via_document_create(home_workspace):
    adapter = libreoffice_adapter()
    dest = home_workspace / "memo.pdf"
    res = adapter.execute("document.create",
                          {"path": str(dest), "text": "Hello world.\nSecond line.\n",
                           "source_format": "txt"},
                          _envelope("document.create"))
    assert res.ok, res.error
    out = Path(res.provider_object_id)
    assert out.exists() and out.read_bytes()[:5] == b"%PDF-"


@pytest.mark.skipif(not _HAVE_SOFFICE, reason="LibreOffice not installed")
def test_real_slides_render_to_pdf(home_workspace):
    adapter = libreoffice_adapter()
    # author a source doc first (text → docx), then render it to PDF (no envelope on the render)
    src = home_workspace / "deck.docx"
    made = adapter.execute("document.create",
                           {"path": str(src), "text": "A slide of text.", "source_format": "txt"},
                           _envelope("document.create"))
    assert made.ok, made.error

    res = adapter.execute("slides.render", {"path": str(src)}, envelope=None)
    assert res.ok, res.error
    out = Path(res.provider_object_id)
    assert out.suffix == ".pdf" and out.read_bytes()[:5] == b"%PDF-"
