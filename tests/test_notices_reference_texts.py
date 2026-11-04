"""Texts found after the first notices round (register items D-LIC-6, -13, -18): the generator must print them.

* package_files: r-efi keeps its licence statement in AUTHORS, not in a LICENSE file.
* reference_notices: texts for libraries the SBOM names only as a limitation (libyaml, libffi, DuckDB's tree).
* texts/MANIFEST.json: every reference text is on disk with the recorded sha256 (offline check of fetch_reference_texts.py).
* the objc2 family carries the standard MIT / Zlib / Apache texts next to the upstream licence-selection statement.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "packaging" / "notices"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_notices2_{name}", NOTICES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load("generate_notices")
fetch = _load("fetch_reference_texts")
COMPONENTS = json.loads((NOTICES / "components.json").read_text(encoding="utf-8"))


def test_manifest_matches_every_text_on_disk():
    man = json.loads((NOTICES / "texts" / "MANIFEST.json").read_text(encoding="utf-8"))["files"]
    on_disk = {p.name for p in (NOTICES / "texts").iterdir() if p.is_file() and p.name != "MANIFEST.json"}
    assert on_disk == set(man), sorted(on_disk ^ set(man))
    for name, meta in man.items():
        assert hashlib.sha256((NOTICES / "texts" / name).read_bytes()).hexdigest() == meta["sha256"], name
        assert meta["source"].startswith("https://"), name
    assert fetch.verify() == 0
    assert set(fetch.ENTRIES) <= set(man)


def test_every_text_named_in_components_json_exists():
    names = set()
    for e in COMPONENTS["entries"].values():
        names |= set(e.get("texts", []))
    for s in COMPONENTS["static_components"]:
        names |= set(s.get("texts", []))
    for r in COMPONENTS["reference_notices"]:
        names |= set(r.get("texts", []))
    missing = sorted(n for n in names if not (NOTICES / "texts" / n).is_file())
    assert missing == []


def test_objc2_family_has_the_standard_texts_and_keeps_the_apple_sdk_note():
    fam = {k: v for k, v in COMPONENTS["entries"].items() if v.get("decision") == "D-LIC-18"}
    assert len(fam) >= 11
    for k, v in fam.items():
        assert "objc2-LICENSE-MIT.txt" in v["texts"], k
        assert "Apple SDKs" in v["note"], k                       # the unresolved Xcode question stays on record
        assert v["unverified"], k
    three = [k for k, v in fam.items() if "objc2-LICENSE-APACHE.txt" in v["texts"]]
    assert "cargo:objc2" not in three and "cargo:objc2-app-kit" in three


def _sbom(tmp: Path, limitations=()):
    p = tmp / "sbom.json"
    p.write_text(json.dumps({
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "metadata": {"properties": [{"name": f"redevops:limitation:{x}", "value": "v"} for x in limitations]},
        "components": [{"type": "library", "name": "r-efi", "version": "5.2.0", "purl": "pkg:cargo/r-efi@5.2.0"},
                       {"type": "library", "name": "plain", "version": "1.0.0", "purl": "pkg:cargo/plain@1.0.0"}]}))
    return p


def test_package_files_make_r_efi_authors_the_licence_text(tmp_path):
    crates = tmp_path / "crates"
    (crates / "r-efi-5.2.0").mkdir(parents=True)
    (crates / "r-efi-5.2.0" / "AUTHORS").write_text("LICENSE:\n  triple-licensed MIT, Apache-2.0, LGPL-2.1+\n")
    (crates / "plain-1.0.0").mkdir()
    (crates / "plain-1.0.0" / "AUTHORS").write_text("not a licence\n")          # AUTHORS only counts where components.json says so
    out = tmp_path / "out"
    records, missing = gen.run(_sbom(tmp_path), [], [crates], NOTICES / "texts", NOTICES / "components.json", out, "t", [])
    by = {r["name"]: r for r in records}
    assert by["r-efi"]["status"] == "text-found"
    assert [f["label"] for f in by["r-efi"]["files"]] == ["r-efi-5.2.0/AUTHORS"]
    assert by["plain"]["status"] == "text-not-found"
    assert [m["name"] for m in missing] == ["plain"]
    assert "triple-licensed MIT, Apache-2.0, LGPL-2.1+" in (out / "THIRD_PARTY_NOTICES.txt").read_text()


def test_reference_notices_follow_the_sbom_limitations(tmp_path):
    crates = tmp_path / "crates"
    crates.mkdir()
    out = tmp_path / "out"
    gen.run(_sbom(tmp_path, ["L-LIBYAML", "L-DUCKDB"]), [], [crates], NOTICES / "texts", NOTICES / "components.json", out, "t", [])
    text = (out / "THIRD_PARTY_NOTICES.txt").read_text()
    rep = json.loads((out / "notices-report.json").read_text())
    ids = [r["id"] for r in rep["reference_notices"]]
    assert ids == ["libyaml", "duckdb-third-party"]                 # libffi is not listed: its limitation is absent
    assert "REFERENCE NOTICES" in text and "Ingy" in text and "Stichting DuckDB Foundation" in text
    assert "UNVERIFIED" in text and "does not state that any component" in text
    # no limitation, no reference section
    out2 = tmp_path / "out2"
    gen.run(_sbom(tmp_path), [], [crates], NOTICES / "texts", NOTICES / "components.json", out2, "t", [])
    assert "REFERENCE NOTICES" not in (out2 / "THIRD_PARTY_NOTICES.txt").read_text()
    assert json.loads((out2 / "notices-report.json").read_text())["reference_notices"] == []
