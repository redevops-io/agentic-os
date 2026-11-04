"""Shipped licence/notice files: the notices generator, the staging step, the artifact manifest check and the
licence-statement consistency check (packaging/notices/). Offline; no build venv required.

The tools live in `packaging/notices/` (not an importable package: the name `packaging` would shadow the PyPA
library), so they are loaded by path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "packaging" / "notices"
FIX = Path(__file__).resolve().parent / "fixtures" / "licences"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_notices_{name}", NOTICES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load("generate_notices")
interim = _load("interim_sbom")
stage = _load("stage_licenses")
manifest = _load("check_artifact_manifest")
statements = _load("check_licence_statements")


# ----------------------------------------------------------------------------- generator

def _fake_env(tmp_path: Path):
    site = tmp_path / "site-packages"
    for name, ver, text in (("alpha", "1.0", "MIT License\n\nCopyright (c) Alpha\n"),
                            ("beta", "2.0", "MIT License\n\nCopyright (c) Alpha\n"),   # identical bytes
                            ("gamma", "3.0", None)):                                    # no licence file
        d = site / f"{name}-{ver}.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: {ver}\nLicense-Expression: MIT\n")
        if text:
            (d / "licenses").mkdir()
            (d / "licenses" / "LICENSE").write_text(text)
    # a package with only a NOTICE file: that is not a licence text
    d = site / "delta-4.0.dist-info"
    d.mkdir()
    (d / "METADATA").write_text("Name: delta\nVersion: 4.0\n")
    (d / "NOTICE").write_text("Delta notice\n")
    crates = tmp_path / "crates"
    (crates / "serde-1.0.0").mkdir(parents=True)
    (crates / "serde-1.0.0" / "LICENSE-MIT").write_text("MIT crate text\n")
    (crates / "nolic-0.1.0").mkdir()
    texts = tmp_path / "texts"
    texts.mkdir()
    (texts / "CPython-LICENSE.txt").write_text("PSF text\n")
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
        {"type": "library", "name": "alpha", "version": "1.0", "purl": "pkg:pypi/alpha@1.0"},
        {"type": "library", "name": "beta", "version": "2.0", "purl": "pkg:pypi/beta@2.0"},
        {"type": "library", "name": "gamma", "version": "3.0", "purl": "pkg:pypi/gamma@3.0"},
        {"type": "library", "name": "delta", "version": "4.0", "purl": "pkg:pypi/delta@4.0"},
        {"type": "library", "name": "missing-from-venv", "version": "1", "purl": "pkg:pypi/missing-from-venv@1"},
        {"type": "library", "name": "serde", "version": "1.0.0", "purl": "pkg:cargo/serde@1.0.0",
         "licenses": [{"expression": "MIT OR Apache-2.0"}]},
        {"type": "library", "name": "nolic", "version": "0.1.0", "purl": "pkg:cargo/nolic@0.1.0"},
        {"type": "library", "name": "cpython", "version": "3.12.10", "purl": "pkg:generic/cpython@3.12.10"},
        {"type": "library", "name": "msvc", "version": "14", "purl": "pkg:generic/msvc@14"},
        {"type": "library", "name": "unknown-thing", "version": "1", "purl": "pkg:generic/unknown-thing@1"},
    ]}))
    comps = tmp_path / "components.json"
    comps.write_text(json.dumps({"entries": {}, "static_components": [
        {"name": "cpython", "version": "3.12.10", "declared": "PSF-2.0", "texts": ["CPython-LICENSE.txt"],
         "decision": "D-LIC-6"},
        {"name": "msvc", "version": "14", "declared": "Microsoft proprietary", "texts": [],
         "missing_reason": "terms unread", "unverified": "terms unread", "decision": "D-LIC-6"},
    ]}))
    return sbom, site, crates, texts, comps


def test_generator_collects_texts_and_lists_every_missing_item(tmp_path):
    sbom, site, crates, texts, comps = _fake_env(tmp_path)
    out = tmp_path / "out"
    records, missing = gen.run(sbom, [site], [crates], texts, comps, out, "T", [])
    by = {r["name"]: r for r in records}
    assert {n for n, r in by.items() if r["status"] == "text-found"} == {"alpha", "beta", "serde", "cpython"}
    missing_names = {m["name"] for m in missing}
    # nothing is silently skipped: each of these is a decision item with a reason
    assert missing_names == {"gamma", "delta", "missing-from-venv", "nolic", "msvc", "unknown-thing"}
    assert all(m["reasons"] for m in missing)
    assert by["delta"]["files"][0]["kind"] == "notice" and by["delta"]["status"] == "text-not-found"
    assert by["msvc"]["decision"] == "D-LIC-6" and by["gamma"]["decision"] == "D-LIC-13"
    text = (out / "THIRD_PARTY_NOTICES.txt").read_text()
    assert "TEXT NOT FOUND: dist-info holds no LICENSE/COPYING/NOTICE file" in text
    assert "UNVERIFIED: terms unread" in text
    assert "does not state that any component" in text and "cleared" in text   # the disclaimer is present
    # identical bytes are printed once, the second occurrence points back
    assert text.count("Copyright (c) Alpha") == 1 and "identical to the text printed under" in text
    md = (out / "NOTICES_MISSING.md").read_text()
    assert "| gamma | 3.0 |" in md and "| msvc | 14 |" in md
    rep = json.loads((out / "notices-report.json").read_text())
    assert rep["text_not_found"] == len(missing) and rep["components"] == len(records)


def test_generator_fail_on_missing_is_the_release_gate(tmp_path):
    sbom, site, crates, texts, comps = _fake_env(tmp_path)
    args = ["--sbom", str(sbom), "--site-packages", str(site), "--crates-dir", str(crates), "--texts-dir",
            str(texts), "--components", str(comps), "--out", str(tmp_path / "o")]
    assert gen.main(args) == 0
    assert gen.main(args + ["--fail-on-missing"]) == 1


def test_generator_exclude_leaves_a_component_out(tmp_path):
    sbom, site, crates, texts, comps = _fake_env(tmp_path)
    records, _ = gen.run(sbom, [site], [crates], texts, comps, tmp_path / "o", "T", ["gamma", "Delta"])
    assert "gamma" not in {r["name"] for r in records} and "delta" not in {r["name"] for r in records}


def test_interim_sbom_from_lock_and_frozen_listing(tmp_path):
    """The generator input for the frozen R0 archive: lock pins + modules observed in the frozen archive
    (the Windows PYZ listing of the audit) select the distributions; a dist that is in the archive but not
    in the lock (knowledge_frontier, packaging) is added; a lock pin with no module (pycparser) is kept."""
    site = tmp_path / "sp"
    for name, ver, tops in (("knowledge_frontier", "0.1.0", ["knowledge_frontier"]),
                            ("packaging", "26.3", ["packaging"]), ("pycparser", "3.0", ["pycparser"]),
                            ("PyYAML", "6.0.3", ["yaml"])):
        d = site / f"{name}-{ver}.dist-info"
        d.mkdir(parents=True)
        (d / "top_level.txt").write_text("\n".join(tops) + "\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("agentic-os==0.2.4\npycparser==3.0\nPyYAML==6.0.3\n")
    frozen = interim.frozen_distributions(FIX / "frozen-r0-windows-pyz-modules.txt", [site])
    assert ("knowledge-frontier", "0.1.0") in frozen and ("packaging", "26.3") in frozen
    assert ("pycparser", "3.0") not in frozen
    doc = interim.build(lock, [], NOTICES / "components.json", "windows", set(), frozen)
    names = {c["name"] for c in doc["components"]}
    assert {"pycparser", "PyYAML", "knowledge-frontier", "packaging", "cpython", "microsoft-vc-runtime"} <= names
    assert "agentic-os" not in names            # the product is covered by LICENSING.md
    mac = interim.build(lock, [], NOTICES / "components.json", "macos", set())
    assert "microsoft-vc-runtime" not in {c["name"] for c in mac["components"]}
    ex = interim.build(lock, [], NOTICES / "components.json", "macos", {"pyyaml"})
    assert "PyYAML" not in {c["name"] for c in ex["components"]}


def test_components_json_every_text_exists_and_matches_its_manifest():
    comps = json.loads((NOTICES / "components.json").read_text())
    man = json.loads((NOTICES / "texts" / "MANIFEST.json").read_text())["files"]
    for sc in comps["static_components"]:
        for t in sc["texts"]:
            assert (NOTICES / "texts" / t).is_file(), t
    import hashlib
    for name, info in man.items():
        assert hashlib.sha256((NOTICES / "texts" / name).read_bytes()).hexdigest() == info["sha256"], name
    # items without a text carry the reason and a decision id, never a silent gap
    for sc in comps["static_components"]:
        if not sc["texts"]:
            assert sc.get("missing_reason") and sc.get("decision", "").startswith("D-LIC-"), sc["name"]


# ------------------------------------------------------------------------------- staging

def _generated(tmp_path):
    gdir = tmp_path / "gen"
    gdir.mkdir()
    for n in ("THIRD_PARTY_NOTICES.txt", "NOTICES_MISSING.md", "NOTICES_MISSING.json"):
        (gdir / n).write_text(n)
    return gdir


def test_stage_fails_without_generated_notices_and_succeeds_with_them(tmp_path):
    dest = tmp_path / "licenses"
    errs = stage.stage(tmp_path / "empty", dest)
    assert errs and any("THIRD_PARTY_NOTICES.txt" in e for e in errs) and not dest.exists()
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    assert stage.stage(_generated(tmp_path), dest) == []
    names = {p.name for p in dest.iterdir()}
    assert {"LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "THIRD_PARTY_NOTICES.txt",
            "NOTICES_MISSING.md", "LICENSES-MANIFEST.json"} <= names
    assert "stale.txt" not in names                  # the folder is generated: no stale file survives
    man = json.loads((dest / "LICENSES-MANIFEST.json").read_text())["files"]
    assert set(man) == names - {"LICENSES-MANIFEST.json"}


# ------------------------------------------------------------------ artifact manifest check

def test_frozen_r0_macos_app_lacks_the_licence_files_and_r0prime_layout_has_them():
    r0 = (FIX / "frozen-r0-macos-app.listing").read_text().splitlines()
    errs = manifest.check_paths(r0)
    assert len(errs) == len(manifest.REQUIRED), errs      # R0: none of the files is there
    new = (FIX / "r0prime-macos-app.listing").read_text().splitlines()
    assert manifest.check_paths(new) == []


def test_windows_install_tree_listing(tmp_path):
    good = [f"C:\\Program Files\\Agentic Apps R0-prime\\Licenses\\{n}" for n in manifest.REQUIRED]
    assert manifest.check_paths(good) == []
    assert manifest.check_paths(good[:-1]) != []


def test_directory_check_detects_missing_and_altered_files(tmp_path):
    app = tmp_path / "Agentic Apps.app" / "Contents" / "Resources"
    assert stage.stage(_generated(tmp_path), app / "Licenses") == []
    assert manifest.check_dir(tmp_path / "Agentic Apps.app") == []
    (app / "Licenses" / "THIRD_PARTY_NOTICES.txt").write_text("tampered")
    assert any("sha256 differs" in e for e in manifest.check_dir(tmp_path / "Agentic Apps.app"))
    (app / "Licenses" / "LICENSE").unlink()
    assert any("no Licenses/LICENSE" in e for e in manifest.check_dir(tmp_path / "Agentic Apps.app"))


# ------------------------------------------------------------- licence-statement consistency

def test_repo_licence_fields_agree_with_licensing_md():
    assert statements.check(ROOT) == []


def _copy_tree(tmp_path: Path) -> Path:
    dst = tmp_path / "repo"
    for rel in ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "README.md", "modules.yaml",
                "pyproject.toml", "deploy/launcher/src-tauri/Cargo.toml",
                "deploy/launcher/src-tauri/tauri.conf.json", "agentic_os/stack_knowledge.py",
                "packaging/DECISIONS-REQUIRED.md"):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst / rel)
    return dst


@pytest.mark.parametrize("rel,old,new,needle", [
    ("pyproject.toml", 'license = "LicenseRef-AGPL-3.0-or-later-with-Commons-Clause"',
     'license = { text = "AGPL-3.0-or-later WITH Commons-Clause" }', "pyproject [project] license"),
    ("deploy/launcher/src-tauri/Cargo.toml", 'license-file = "../../../LICENSING.md"',
     'license = "AGPL-3.0-or-later"', "launcher Cargo.toml has license"),
    ("deploy/launcher/src-tauri/tauri.conf.json", '"resources": {"licenses/": "Licenses/"},', "",
     "does not deliver a Licenses/"),
    ("agentic_os/stack_knowledge.py", "source-available (family licence: AGPL-3.0-or-later with the \"\n"
     "            \"Commons Clause)", "open source (AGPL-3.0)", "licensing answer"),
    ("README.md", "source-available", "freely available", "README.md licence"),
    ("packaging/DECISIONS-REQUIRED.md", "D-LIC-12", "D-LIC-99", "D-LIC-12 appears in LICENSING.md"),
])
def test_statement_check_fails_when_a_field_drifts(tmp_path, rel, old, new, needle):
    repo = _copy_tree(tmp_path)
    assert statements.check(repo) == []
    p = repo / rel
    text = p.read_text()
    assert old in text, f"fixture drifted: {old!r} not in {rel}"
    p.write_text(text.replace(old, new))
    errs = statements.check(repo)
    assert any(needle in e for e in errs), errs


def test_committed_artifact_configuration_delivers_licences():
    conf = json.loads((ROOT / "deploy/launcher/src-tauri/tauri.conf.json").read_text())["bundle"]
    assert conf["resources"] == {"licenses/": "Licenses/"} and "RedevOps" in conf["copyright"]
    assert "licenses/" in (ROOT / ".gitignore").read_text()   # generated, never committed


# ------------------------------------------------- against a real build venv (opt-in, needs the venv)

@pytest.mark.skipif(not os.environ.get("NOTICES_BUILD_SITE_PACKAGES"),
                    reason="set NOTICES_BUILD_SITE_PACKAGES to the site-packages of a build venv")
def test_generator_against_frozen_listing_and_build_venv(tmp_path):
    """Frozen R0 archive listing (Windows PYZ module list of the audit) + the lock + a build venv: the
    Python part of the component list must get its licence texts, and the known gaps must be reported."""
    site = Path(os.environ["NOTICES_BUILD_SITE_PACKAGES"])
    lock = tmp_path / "lock.txt"
    lock.write_text((ROOT / "packaging" / "requirements.lock").read_text())
    frozen = interim.frozen_distributions(FIX / "frozen-r0-windows-pyz-modules.txt", [site])
    doc = interim.build(lock, [], NOTICES / "components.json", "windows", set(), frozen)
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps(doc))
    records, missing = gen.run(sbom, [site], [], NOTICES / "texts", NOTICES / "components.json", tmp_path / "o",
                               "frozen R0", [])
    by = {r["name"]: r for r in records}
    assert by["certifi"]["status"] == "text-found"
    text = (tmp_path / "o" / "THIRD_PARTY_NOTICES.txt").read_text()
    assert "Mozilla Public License Version 2.0" in text                      # certifi: full text from texts/
    assert by["cpython"]["status"] == "text-found" and by["openssl"]["status"] == "text-found"
    gaps = {m["name"]: m["decision"] for m in missing}
    assert gaps.get("microsoft-vc-runtime") == "D-LIC-6"
    assert gaps.get("redevops-connectors") == "D-LIC-2"
