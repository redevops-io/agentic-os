"""W3 — the file-format family (``file`` provider, ``FILE_NATIVE``).

Real temp files (:mod:`tempfile`), no network, no third-party deps. Exercises CSV round-trip,
XLSX read (a minimal valid ``.xlsx`` built in-test with :mod:`zipfile`), Markdown create/read,
DOCX text read (a minimal ``.docx`` built in-test), governance (a write refuses without an
envelope), and the catalog liveness projection.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from agentic_os.integrations.execution import GovernedEnvelope
from agentic_os.integrations.file_formats import (
    FileFormatAdapter,
    StdlibFileEngine,
    file_format_adapter,
)
from agentic_os.integrations.productivity import default_registry, file_provider


def _envelope(capability: str) -> GovernedEnvelope:
    return GovernedEnvelope(intent_hash="deadbeef" * 8, capability=capability, provider="file", tier=2)


# ── minimal in-test fixtures (a few required Open XML parts, authored with zipfile) ──────────
def _write_minimal_xlsx(path: Path) -> None:
    """A minimal valid .xlsx: content types, a workbook, one worksheet, a shared-string table.
    Row 1 = ["Name", "Qty"] (shared strings 0/1); row 2 = ["Widget", 7] ("Widget"=idx 2, 7 numeric)."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '</Types>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">'
        '<si><t>Name</t></si><si><t>Qty</t></si><si><t>Widget</t></si></sst>'
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>7</v></c></row>'
        '</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def _write_minimal_docx(path: Path) -> None:
    """A minimal .docx with two paragraphs, one carrying two <w:t> runs."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        '<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t xml:space="preserve"> world</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Second line.</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("word/document.xml", document)


# ── tests ────────────────────────────────────────────────────────────────────────────────────
def test_csv_round_trip_and_observe(tmp_path):
    adapter = file_format_adapter()
    path = str(tmp_path / "grid.csv")
    grid = [["Name", "Qty"], ["Widget", "7"], ["Gadget", "12"]]

    wrote = adapter.execute("sheet.write", {"path": path, "values": grid}, _envelope("sheet.write"))
    assert wrote.ok and wrote.data["rows_written"] == 3

    read = adapter.execute("sheet.read", {"path": path}, None)  # read needs no envelope
    assert read.ok and read.data["values"] == grid

    obs = adapter.observe(read.provider_object_id)
    assert obs.found and obs.data["parsed"] is True


def test_xlsx_read_from_in_test_zip(tmp_path):
    path = tmp_path / "book.xlsx"
    _write_minimal_xlsx(path)
    adapter = file_format_adapter()

    res = adapter.execute("sheet.read", {"path": str(path)}, None)
    assert res.ok
    # shared strings resolved, numeric cell kept as text; sparse grid preserved
    assert res.data["values"] == [["Name", "Qty"], ["Widget", "7"]]
    assert adapter.observe(str(path)).found is True


def test_xlsx_write_is_deferred_with_a_clear_error(tmp_path):
    adapter = file_format_adapter()
    res = adapter.execute("sheet.write",
                          {"path": str(tmp_path / "out.xlsx"), "values": [["a"]]},
                          _envelope("sheet.write"))
    assert not res.ok and "xlsx write needs the optional file-formats engine" in res.error


def test_markdown_create_then_read(tmp_path):
    adapter = file_format_adapter()
    path = str(tmp_path / "note.md")
    body = "# Title\n\nA paragraph.\n"

    created = adapter.execute("document.create", {"path": path, "text": body},
                              _envelope("document.create"))
    assert created.ok and Path(path).read_text(encoding="utf-8") == body

    read = adapter.execute("document.read", {"path": path}, None)
    assert read.ok and read.data["text"] == body


def test_docx_text_read_from_in_test_zip(tmp_path):
    path = tmp_path / "doc.docx"
    _write_minimal_docx(path)
    adapter = file_format_adapter()

    res = adapter.execute("document.read", {"path": str(path)}, None)
    assert res.ok
    assert res.data["text"] == "Hello world\nSecond line."


def test_docx_create_is_deferred_with_a_clear_error(tmp_path):
    adapter = file_format_adapter()
    res = adapter.execute("document.create",
                          {"path": str(tmp_path / "out.docx"), "text": "x"},
                          _envelope("document.create"))
    assert not res.ok and "docx create needs the optional file-formats engine" in res.error


def test_write_refuses_without_an_envelope(tmp_path):
    adapter = file_format_adapter()
    path = str(tmp_path / "guard.csv")
    refused = adapter.execute("sheet.write", {"path": path, "values": [["a"]]}, envelope=None)
    assert not refused.ok and "envelope required" in refused.error
    assert not Path(path).exists()  # nothing was written

    # the same write with a governed envelope succeeds
    ok = adapter.execute("sheet.write", {"path": path, "values": [["a"]]}, _envelope("sheet.write"))
    assert ok.ok and Path(path).exists()


def test_document_create_also_refuses_without_an_envelope(tmp_path):
    adapter = file_format_adapter()
    path = str(tmp_path / "guard.md")
    refused = adapter.execute("document.create", {"path": path, "text": "hi"}, envelope=None)
    assert not refused.ok and "envelope required" in refused.error
    assert not Path(path).exists()


def test_connect_is_a_no_op_no_auth():
    adapter = file_format_adapter()
    conn = adapter.connect({}, "")
    assert conn.provider == "file" and conn.connected is True
    assert adapter.health().healthy is True


def test_unsupported_capability_and_extension_are_clear_results(tmp_path):
    adapter = file_format_adapter()
    # a capability the file provider does not implement
    r1 = adapter.execute("slides.create", {"path": str(tmp_path / "x.pptx")}, _envelope("slides.create"))
    assert not r1.ok and "does not implement" in r1.error
    # an unsupported extension for a supported capability — a result, never a crash
    r2 = adapter.execute("sheet.read", {"path": str(tmp_path / "x.parquet")}, None)
    assert not r2.ok and "cannot read a sheet" in r2.error
    # a missing 'path'
    r3 = adapter.execute("sheet.read", {}, None)
    assert not r3.ok and "missing 'path'" in r3.error


def test_observe_missing_or_unparseable_file():
    adapter = file_format_adapter()
    assert adapter.observe("/no/such/file.csv").found is False
    assert adapter.observe("").found is False


def test_capabilities_advertise_write_flags():
    caps = {c.name: c.write for c in FileFormatAdapter().capabilities()}
    assert caps == {"sheet.read": False, "sheet.write": True,
                    "document.read": False, "document.create": True}


def test_engine_is_injectable():
    adapter = file_format_adapter(engine=StdlibFileEngine())
    assert isinstance(adapter.engine, StdlibFileEngine)


def test_catalog_file_provider_is_live_for_its_caps():
    assert file_provider().is_capability_live("sheet.write") is True
    manifest = default_registry().to_manifest()
    assert "file" in manifest.buildable("sheet.write")
    assert set(manifest.buildable("sheet.write")) >= {"google", "microsoft", "file"}
    # document.edit is still planned for file (partial liveness)
    assert file_provider().is_capability_live("document.edit") is False
