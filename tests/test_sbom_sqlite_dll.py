"""W-SBOM-1: CPython's Windows distribution ships ``sqlite3.dll`` and the frozen sidecar bundles it once any code imports ``sqlite3`` (the mission,
authority and AOS-DD code does). ``packaging/sbom/analyze.py`` had no rule for it, so SBOM pass 1 failed with ``FAIL unattributed: pkg:sqlite3.dll``
on Windows and nothing on the other platforms noticed. SQLite is its own component (it is in the public domain; it is not CPython and not
PSF-licensed), so it is attributed to its own component, with the same treatment as the other CPython-bundled native files (``libffi-8.dll``,
the Microsoft runtime DLLs): a classification rule, an SBOM component that says what it is, a notices entry, a source-offer reference.

Synthetic inputs only (a listing, a hand-made inventory); no Windows, no network, no build. Static regression tests: they fail on the
analyze.py of the 0.6.0-rc1 candidate (b9aca114).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

from sbom import analyze, generate_sbom, verify_sbom  # noqa: E402

LISTING = """Options in 'x' (PKG/CArchive):
 pyi-contents-directory _internal
Contents of 'x' (PKG/CArchive):
 struct
 pyimod01_archive
 pyi_rth_pkgutil
 sidecar_main
 python312.dll
 VCRUNTIME140.dll
 libffi-8.dll
 _sqlite3.pyd
 sqlite3.dll
 PYZ.pyz

Contents of 'PYZ.pyz' (PYZ):
 sqlite3
 sqlite3.dbapi2
 os
 json.decoder
"""
INVENTORY = {"pyinstaller": {"name": "pyinstaller", "version": "6.22.3", "norm": "pyinstaller", "source": "test", "licence": {"expression": "GPL-2.0-or-later"},
                             "top_level": ["PyInstaller"], "requires": [], "wheel_sha256": "ab" * 32, "wheel_file": "pyinstaller-6.22.3.whl"}}


def inputs(tmp_path, facts=None, listing=LISTING):
    (tmp_path / "listing.txt").write_text(listing)
    (tmp_path / "inv.json").write_text(json.dumps(INVENTORY))
    (tmp_path / "facts.json").write_text(json.dumps({"python_version": "3.12.10", **(facts or {})}))
    return ["--pkg-listing", str(tmp_path / "listing.txt"), "--inventory-json", str(tmp_path / "inv.json"), "--facts", str(tmp_path / "facts.json")]


def build(tmp_path, **kw):
    a = inputs(tmp_path, **kw)
    out = tmp_path / "out"
    rc = generate_sbom.main(a + ["--out", str(out)])
    return rc, a, (json.loads((out / "sbom.cdx.json").read_text()) if (out / "sbom.cdx.json").exists() else None)


def props(c):
    return {p["name"]: p["value"] for p in c.get("properties", [])}


# ------------------------------------------------------------------------------------------------ the rule
@pytest.mark.parametrize("name", ["sqlite3.dll", "SQLITE3.DLL", "_internal/sqlite3.dll", "_internal\\SQLite3.dll", "libsqlite3.so.0", "libsqlite3.0.dylib", "libsqlite3-0.dll"])
def test_the_sqlite_library_has_its_own_rule_and_is_not_cpython(name):
    rule, hint = analyze.classify_pkg(name)
    assert rule == "sqlite" and hint == name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def test_the_neighbouring_files_keep_their_rules():
    c = analyze.classify_pkg
    assert c("_sqlite3.pyd")[0] == "cpython" and c("python3.12/lib-dynload/_sqlite3.cpython-312-darwin.so")[0] == "cpython"
    assert c("sqlite3/dbapi2.py") == ("top-level", "sqlite3")
    assert c("libffi-8.dll")[0] == "libffi" and c("VCRUNTIME140.dll")[0] == "ms-dll" and c("python312.dll")[0] == "cpython" and c("libcrypto-3.dll")[0] == "openssl-lib"
    assert c("sqlite3.dll.manifest")[0] != "sqlite" and c("my_sqlite3.dll")[0] != "sqlite" and c("sqlite3.dll.py")[0] != "sqlite"


def test_a_listing_with_sqlite3_dll_no_longer_leaves_it_unattributed(tmp_path):
    rc, a, bom = build(tmp_path)
    assert rc == 0, "generate_sbom must attribute every archive entry"
    assert verify_sbom.main(["--sbom", str(tmp_path / "out" / "sbom.cdx.json")] + a) == 0       # pass 1 / the verifier's own gate


def test_the_component_says_what_it_is(tmp_path):
    rc, a, bom = build(tmp_path)
    c = next(c for c in bom["components"] if c["name"] == "SQLite")
    p = props(c)
    assert c["type"] == "library" and p["redevops:key"] == "sqlite:sqlite3.dll"
    assert "version" not in c and "purl" not in c                                                  # no version is invented
    assert c["licenses"] == [{"license": {"name": "Public domain (declared by the SQLite project)"}}]
    assert p["redevops:licence-status"] == "declared" and "sqlite.org/copyright" in p["redevops:licence-source"]
    role = p["redevops:role"]
    assert "sqlite3.dll" in role and "_sqlite3" in role and "separate from CPython" in role and "not covered by the CPython licence" in role
    assert "version not discoverable" in p["redevops:limitations"]
    names = {x["name"] for x in bom["components"]}
    assert "CPython" in names and "libffi" in names                                                # the neighbours are untouched
    assert next(x for x in bom["components"] if x["name"] == "CPython")["properties"] is not None


def test_the_file_is_attributed_to_sqlite_and_not_to_cpython(tmp_path):
    from sbom.archive import ListingArchive
    (tmp_path / "l.txt").write_text(LISTING)
    res = analyze.analyze(ListingArchive(tmp_path / "l.txt"), INVENTORY, {"python_version": "3.12.10"})
    assert res.attribution["pkg:sqlite3.dll"] == "sqlite:sqlite3.dll" and res.attribution["pkg:_sqlite3.pyd"] == "cpython"
    assert res.unattributed == []


def test_the_version_is_recorded_only_when_a_named_source_gives_it(tmp_path):
    rc, a, bom = build(tmp_path, facts={"sqlite_version": "3.45.1"})
    c = next(c for c in bom["components"] if c["name"] == "SQLite")
    assert c["version"] == "3.45.1" and c["purl"] == "pkg:generic/sqlite@3.45.1"
    assert "sqlite_version 3.45.1" in props(c)["redevops:evidence"]
    assert "limitations" not in " ".join(props(c))                                                   # nothing left to caveat about the version


def test_the_source_id_string_inside_the_file_is_recorded_as_evidence_when_the_bytes_are_available():
    data = b"\x00\x01SQLite format 3\x00 junk 2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257cc467a \x00 more"
    assert analyze.probe_sqlite(data) == [("2024-01-30 16:01:20", "e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257cc467a")]
    assert analyze.probe_sqlite(b"nothing here") == []


def test_the_verifier_fails_when_the_component_is_dropped(tmp_path, capsys):
    rc, a, bom = build(tmp_path)
    bom["components"] = [c for c in bom["components"] if c["name"] != "SQLite"]
    (tmp_path / "edited.json").write_text(json.dumps(bom))
    assert verify_sbom.main(["--sbom", str(tmp_path / "edited.json")] + a) == 1
    assert "SQLite" in capsys.readouterr().out


def test_without_the_dll_there_is_no_sqlite_component(tmp_path):
    rc, a, bom = build(tmp_path, listing=LISTING.replace(" sqlite3.dll\n", ""))
    assert rc == 0 and "SQLite" not in {c["name"] for c in bom["components"]}


# ------------------------------------------------------------------------------------------------ the notices
def _load(name):
    spec = importlib.util.spec_from_file_location(f"_notices_{name}", ROOT / "packaging" / "notices" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_notices_know_the_sqlite_component_and_the_interim_sbom_lists_it_for_windows(tmp_path):
    comps = json.loads((ROOT / "packaging/notices/components.json").read_text())
    sc = next(s for s in comps["static_components"] if s["name"] == "sqlite")
    assert sc["present_in"] == ["windows"] and "ublic domain" in sc["declared"] and sc["decision"] == "D-LIC-6"
    assert sc["texts"] == [] and sc["missing_reason"] and "unverified" in sc
    interim = _load("interim_sbom")
    doc = interim.build(None, [], ROOT / "packaging/notices/components.json", "windows", set(), None)
    assert "sqlite" in {c["name"] for c in doc["components"]}
    assert "sqlite" not in {c["name"] for c in interim.build(None, [], ROOT / "packaging/notices/components.json", "macos", set(), None)["components"]}


def test_the_notices_report_the_component_as_a_decision_item_with_its_declared_licence(tmp_path):
    rc, a, bom = build(tmp_path)
    (tmp_path / "sbom.json").write_text(json.dumps(bom))
    gen = _load("generate_notices")
    records, missing = gen.run(tmp_path / "sbom.json", [], [], ROOT / "packaging/notices/texts", ROOT / "packaging/notices/components.json", tmp_path / "notices", "t", [])
    rec = next(r for r in records if r["name"] == "SQLite")
    assert rec["decision"] == "D-LIC-6" and "ublic domain" in rec["declared"] and rec["status"] == "text-not-found"
    assert any("public domain" in x.lower() for x in rec["reasons"])
    text = (tmp_path / "notices" / "THIRD_PARTY_NOTICES.txt").read_text()
    assert "SQLite" in text and "NOT FOUND" in text.split("SQLite", 1)[1][:800]
    chk = _load("check_notices_vs_sbom")
    assert chk.check(tmp_path / "sbom.json", tmp_path / "notices" / "notices-report.json", None, []) == []


# ------------------------------------------------------------------------------------------------ the source offer
def run_script(rel, *args, cwd):
    return subprocess.run([sys.executable, "-I", str(ROOT / rel), *args], capture_output=True, text=True, cwd=str(cwd))


def test_collect_source_refs_gives_sqlite_an_honest_unresolved_entry_and_the_offer_generator_accepts_it(tmp_path):
    rc, a, bom = build(tmp_path)
    (tmp_path / "sbom.json").write_text(json.dumps(bom))
    r = run_script("packaging/tools/collect_source_refs.py", "--sbom", str(tmp_path / "sbom.json"), "--out", str(tmp_path / "refs.json"), "--no-download", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    ref = json.loads((tmp_path / "refs.json").read_text())["components"]["sqlite:sqlite3.dll"]
    assert ref["name"] == "SQLite" and "source" not in ref and "public domain" in ref["unresolved"].lower() and "not read" in ref["unresolved"].lower()
    out = tmp_path / "offer.md"
    r = run_script("packaging/sbom/generate_source_offer.py", "--sbom", str(tmp_path / "sbom.json"), "--sources", str(tmp_path / "refs.json"), "--out", str(out),
                   "--template", str(ROOT / "packaging/SOURCE-OFFER.md"), cwd=tmp_path)
    # CPython/libffi/Microsoft entries are not in this synthetic refs file: only the SQLite row is under test
    assert "SQLite" in out.read_text() and "sqlite:sqlite3.dll" not in (r.stdout.split("NO source-reference entry for:")[1] if "NO source-reference entry for:" in r.stdout else "")


def test_the_offer_generator_still_refuses_a_shipped_component_with_no_reference(tmp_path):
    rc, a, bom = build(tmp_path)
    (tmp_path / "sbom.json").write_text(json.dumps(bom))
    (tmp_path / "empty.json").write_text(json.dumps({"components": {}}))
    r = run_script("packaging/sbom/generate_source_offer.py", "--sbom", str(tmp_path / "sbom.json"), "--sources", str(tmp_path / "empty.json"), "--out", str(tmp_path / "o.md"),
                   "--template", str(ROOT / "packaging/SOURCE-OFFER.md"), cwd=tmp_path)
    assert r.returncode == 1 and "sqlite:sqlite3.dll" in r.stdout


def test_with_the_bytes_of_the_archive_the_source_id_becomes_evidence_on_the_component():
    from sbom.archive import Archive

    class Bytes(Archive):
        pkg = ["sidecar_main", "python312.dll", "_sqlite3.pyd", "sqlite3.dll"]
        pyz = ["sqlite3"]
        source = "bytes"
        has_bytes = True

        def read(self, name):
            return b"\x00SQLite format 3\x00 2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257cc467a\x00" if name == "sqlite3.dll" else b"MZ"
    res = analyze.analyze(Bytes(), INVENTORY, {"python_version": "3.12.10"})
    c = res.comps["sqlite:sqlite3.dll"]
    assert any('SQLITE_SOURCE_ID "2024-01-30 16:01:20 e876e51a' in e for e in c.evidence) and c.version is None
    assert any("version not discoverable" in x for x in c.limitations)
