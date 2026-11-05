"""SBOM generator / verifier: classification rules, banner probes, and the verifier's failure modes.

Synthetic inputs only (no network, no PyInstaller needed), plus optional checks against the frozen v0.3.2
listings / a built sidecar when those exist on the machine (skipped otherwise).
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

from sbom import analyze, jsscan, pe, rustsbom  # noqa: E402
from sbom.archive import ListingArchive  # noqa: E402
from sbom import generate_sbom, verify_sbom  # noqa: E402

FROZEN = Path(os.environ["FROZEN_EVIDENCE_DIR"]) if os.environ.get("FROZEN_EVIDENCE_DIR") else Path("/nonexistent-frozen-evidence")


def inv_entry(name, version, top, lic="MIT"):
    return {"name": name, "version": version, "norm": name.lower().replace("_", "-"), "source": "test",
            "licence": {"expression": lic}, "top_level": top, "requires": [], "wheel_sha256": "ab" * 32, "wheel_file": f"{name}-{version}.whl"}


INVENTORY = {
    "idna": inv_entry("idna", "3.20", ["idna"], "BSD-3-Clause"),
    "cryptography": inv_entry("cryptography", "50.0.1", ["cryptography"], "Apache-2.0 OR BSD-3-Clause"),
    "agentic-os": inv_entry("agentic-os", "0.2.4", ["agentic_os"], "AGPL-3.0-or-later"),
    "certifi": inv_entry("certifi", "2026.7.22", ["certifi"], "MPL-2.0"),
    "pyinstaller": inv_entry("pyinstaller", "6.22.3", ["PyInstaller"], "GPL-2.0-or-later"),
}
LISTING = """Options in 'x' (PKG/CArchive):
 pyi-contents-directory _internal
Contents of 'x' (PKG/CArchive):
 struct
 pyimod01_archive
 pyi_rth_pkgutil
 sidecar_main
 Python.framework/Versions/3.12/Python
 libcrypto.3.dylib
 libssl.3.dylib
 cryptography/hazmat/bindings/_rust.abi3.so
 certifi/cacert.pem
 agentic_os/projects_ui/assets/index-x.js
 agentic_os/projects_ui/hero.jpg
 python3.12/lib-dynload/_ssl.cpython-312-darwin.so
 PYZ.pyz

Contents of 'PYZ.pyz' (PYZ):
 idna
 idna.core
 os
 json.decoder
 agentic_os.cli
 cryptography.x509
"""
FACTS = {"python_version": "3.12.10", "openssl": [{"entry": "libcrypto.3.dylib", "version": "3.0.16"},
                                                   {"entry": "cryptography/hazmat/bindings/_rust.abi3.so", "version": "4.0.2"}],
         "ui_js": [{"name": "react", "version": "18.3.1", "licence_text": "MIT"}]}


@pytest.fixture()
def inputs(tmp_path):
    (tmp_path / "listing.txt").write_text(LISTING)
    (tmp_path / "inv.json").write_text(json.dumps(INVENTORY))
    (tmp_path / "facts.json").write_text(json.dumps(FACTS))
    return tmp_path


def args(d, sbom=None):
    a = ["--pkg-listing", str(d / "listing.txt"), "--inventory-json", str(d / "inv.json"), "--facts", str(d / "facts.json")]
    return a


def gen(d):
    out = d / "out"
    rc = generate_sbom.main(args(d) + ["--out", str(out)])
    return rc, out / "sbom.cdx.json"


def test_classify_pkg_rules():
    c = analyze.classify_pkg
    assert c("pyi_rth_inspect")[0] == "pyinstaller-runtime" and c("sidecar_main")[0] == "sidecar"
    assert c("Python.framework/Versions/3.12/Python")[0] == "cpython" and c("python312.dll")[0] == "cpython"
    assert c("python3.12/lib-dynload/_ssl.cpython-312-darwin.so")[0] == "cpython" and c("_ssl.pyd")[0] == "cpython"
    assert c("libcrypto.3.dylib")[0] == "openssl-lib" and c("libssl-3.dll")[0] == "openssl-lib"
    assert c("VCRUNTIME140.dll")[0] == "ms-dll" and c("ucrtbase.dll")[0] == "ms-dll" and c("api-ms-win-core-file-l1-1-0.dll")[0] == "ms-apiset"
    assert c("libffi-8.dll")[0] == "libffi"
    assert c("pydantic-2.13.5.dist-info/METADATA") == ("dist-info", "pydantic")
    assert c("_duckdb.cp312-win_amd64.pyd") == ("top-level", "_duckdb")
    assert c("yaml/_yaml.cpython-312-darwin.so") == ("top-level", "yaml")
    # Linux (x86_64 and aarch64): the extension tag has several hyphens, the interpreter is a shared library, and PyInstaller
    # copies the GCC runtime from the build host. Before this was fixed these four entries were "unattributed" and the SBOM
    # step of build-all.sh exited 1 on an Ubuntu host.
    assert c("_cffi_backend.cpython-312-aarch64-linux-gnu.so") == ("top-level", "_cffi_backend")
    assert c("_cffi_backend.cpython-312-x86_64-linux-gnu.so") == ("top-level", "_cffi_backend")
    assert c("libpython3.12.so.1.0")[0] == "cpython"
    assert c("libgcc_s.so.1") == ("gcc-runtime", "libgcc_s.so.1") and c("libstdc++.so.6") == ("gcc-runtime", "libstdc++.so.6")


def test_openssl_banner_probe_finds_both_versions():
    data = b"\x00junk OpenSSL 3.0.16 11 Feb 2025 \x00\x00 OpenSSL 4.0.2 25 Aug 2026 more"
    assert analyze.probe_openssl(data) == [("3.0.16", "11 Feb 2025"), ("4.0.2", "25 Aug 2026")]


def test_js_scan_reads_react_banners_and_versions_only_when_shown():
    js = ('O.version="18.3.1";var _=ia.exports;/**\n * @license React\n * react.production.min.js\n *\n * Copyright (c) Facebook\n *\n'
          ' * This source code is licensed under the MIT license found in the\n * LICENSE file in the root directory of this source tree.\n */'
          '/**\n * @license React\n * scheduler.production.min.js\n *\n * licensed under the MIT license found in the\n */'
          'x={version:"18.3.1",rendererPackageName:"react-dom"};')
    found = {e["name"]: e for e in jsscan.scan(js)}
    assert found["react"]["version"] == "18.3.1" and found["react"]["licence_text"] == "MIT"
    assert found["scheduler"]["version"] is None            # banner only: never guessed


def test_pe_file_version_from_fixed_file_info():
    blob = b"MZ" + b"\x00" * 100 + b"\xbd\x04\xef\xfe" + struct.pack("<III", 0x10000, (14 << 16) | 42, (34438 << 16) | 0) + b"\x00" * 8
    assert pe.file_version(blob) == "14.42.34438.0"
    assert pe.file_version(b"not a pe") is None
    real = FROZEN / "r0-audit/licences/_work/win/VCRUNTIME140.dll"
    if real.exists():
        assert pe.file_version(real.read_bytes()) == "14.42.34438.0"


def test_generate_attributes_everything_and_marks_native_gaps(inputs):
    rc, sbom = gen(inputs)
    assert rc == 0
    bom = json.loads(sbom.read_text())
    names = {(c["name"], c.get("version")) for c in bom["components"]}
    assert ("OpenSSL", "3.0.16") in names and ("OpenSSL", "4.0.2") in names      # the second, statically linked OpenSSL
    assert ("react", "18.3.1") in names and ("CPython", "3.12.10") in names
    assert any(c["name"].startswith("Mozilla CA") for c in bom["components"])
    limits = {p["name"] for p in bom["metadata"]["properties"]}
    assert "redevops:limitation:L-LISTING-ONLY" in limits and "redevops:limitation:L-CPYTHON-EXT" in limits
    assert bom["compositions"][0]["aggregate"] == "incomplete" and bom["specVersion"] == "1.5"
    idna = next(c for c in bom["components"] if c["name"] == "idna")
    props = {p["name"]: p["value"] for p in idna["properties"]}
    assert idna["licenses"] == [{"expression": "BSD-3-Clause"}] and props["redevops:licence-status"] == "declared"
    assert "redevops:shipped_in" in props and "redevops:evidence" in props and idna["purl"] == "pkg:pypi/idna@3.20"


def test_verifier_passes_its_own_sbom_and_is_deterministic(inputs):
    rc, sbom = gen(inputs)
    assert verify_sbom.main(["--sbom", str(sbom)] + args(inputs)) == 0
    rc2, sbom2 = gen(inputs)
    assert sbom.read_bytes() == sbom2.read_bytes()


def _edit(sbom: Path, fn) -> Path:
    bom = json.loads(sbom.read_text())
    fn(bom)
    out = sbom.with_name("edited.json")
    out.write_text(json.dumps(bom))
    return out


def test_verifier_fails_on_unlisted_component(inputs, capsys):
    _, sbom = gen(inputs)
    edited = _edit(sbom, lambda b: b.update(components=[c for c in b["components"] if not (c["name"] == "OpenSSL" and c.get("version") == "4.0.2")]))
    assert verify_sbom.main(["--sbom", str(edited)] + args(inputs)) == 1
    assert "OpenSSL 4.0.2" in capsys.readouterr().out


def test_verifier_fails_on_stale_version(inputs, capsys):
    _, sbom = gen(inputs)
    def stale(b):
        for c in b["components"]:
            if c["name"] == "idna":
                c["version"] = "3.19"; c["purl"] = "pkg:pypi/idna@3.19"
    edited = _edit(sbom, stale)
    assert verify_sbom.main(["--sbom", str(edited)] + args(inputs)) == 1
    assert "SBOM says 3.19" in capsys.readouterr().out


def test_verifier_fails_on_module_no_distribution_explains(inputs, capsys):
    _, sbom = gen(inputs)
    (inputs / "listing.txt").write_text(LISTING + " mystery_pkg.core\n")
    assert verify_sbom.main(["--sbom", str(sbom)] + args(inputs)) == 1
    assert "mystery_pkg.core" in capsys.readouterr().out


def test_crate_path_strings_in_a_binary():
    data = b"\x00/rustc/x/registry/src/index.crates.io-1949cf8c6b5b557f/serde-1.0.229/src/de.rs\x00registry/src/index.crates.io-aaaa/toml_datetime-1.1.1+spec-1.1.0/src/lib.rs"
    assert rustsbom.crate_paths_in_binary(data) == {("serde", "1.0.229"), ("toml_datetime", "1.1.1+spec-1.1.0")}


@pytest.mark.skipif(not FROZEN.exists(), reason="frozen v0.3.2 evidence directory not available (set FROZEN_EVIDENCE_DIR)")
def test_verifier_reproduces_the_audits_known_gaps_in_the_frozen_macos_sbom(capsys):
    wh = ROOT / "packaging/wheelhouse"
    if not list(wh.glob("cryptography-*.whl")):
        pytest.skip("wheelhouse not built")
    rc = verify_sbom.main(["--sbom", str(FROZEN / "native-release-evidence/macos/sbom.cdx.json"),
                           "--pkg-listing", str(FROZEN / "macos-agentic-apps-v0.3.2/evidence-plan/archive_listing.txt"),
                           "--wheelhouse", str(wh), "--inventory-json", str(ROOT / "packaging/sbom/testdata/build-tools-inventory-macos.json"),
                           "--facts", str(ROOT / "packaging/sbom/testdata/frozen-v0.3.2-macos-facts.json")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "OpenSSL 4.0.2 is in the archive but not in the SBOM" in out          # SC-05 / VG-G02
    assert "react 18.3.1" in out and "Projects UI bundle" in out                   # SC-11
    assert "idna: SBOM says 3.19" in out and "uvicorn: SBOM says 0.52.4" in out    # SC-03 stale lock
    assert "Rust crates statically linked into cryptography" in out


def test_inventory_top_level_strips_every_platform_extension_tag():
    """LNX-01: the DuckDB extension is _duckdb.cpython-312-aarch64-linux-gnu.so on Linux (three hyphen groups); the old pattern
    allowed one, so the inventory named the top-level module '_duckdb.cpython-312-aarch64-linux-gnu' and the SBOM left the
    archive entry unattributed."""
    from packaging.sbom import inventory
    rec = ["_duckdb.cpython-312-aarch64-linux-gnu.so", "_duckdb.cpython-312-x86_64-linux-gnu.so", "_x.cpython-312-darwin.so",
           "_y.cp312-win_amd64.pyd", "duckdb/__init__.py", "duckdb-1.5.5.dist-info/RECORD"]
    assert inventory._top_level_from_record(rec) == ["_duckdb", "_x", "_y", "duckdb"]
