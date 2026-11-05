"""Shared fixtures for the About / legal-slot tests (not a test module).

Everything here is SYNTHETIC: a throw-away repository root with copies of the few real files the tools read, a tiny notices
directory made by the real generator from a made-up SBOM, and synthetic DECIDED gate entries with synthetic placeholder
'texts'. No legal text is written anywhere and no real decision is touched.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "packaging" / "notices"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gen_notices = load(NOTICES / "generate_notices.py", "_t_generate_notices")
stage = load(NOTICES / "stage_licenses.py", "_t_stage_licenses")
gen_about = load(NOTICES / "generate_about.py", "_t_generate_about")
chk_about = load(NOTICES / "check_about.py", "_t_check_about")
about_record = load(ROOT / "agentic_os" / "about_record.py", "_t_about_record")
gate = load(ROOT / "packaging" / "check-distribution-gate.py", "_t_gate")

SLOTS = {
    "LEGAL.COPYRIGHT_LINE": ["D-LIC-1"],
    "LEGAL.LICENCE_STATEMENT": ["D-LIC-1", "D-LIC-15"],
    "LEGAL.SOURCE_OFFER": ["D-LIC-12", "pins-03", "pins-04"],
    "LEGAL.NO_WARRANTY": ["D-LIC-10"],
    "LEGAL.SECTION13_NOTICE": ["D-LIC-5"],
}
ALL_SLOT_DECISIONS = sorted({d for ids in SLOTS.values() for d in ids})
SYNTH = "SYNTHETIC TEST TEXT for {slot}. Not a legal statement; written by the test suite only.\n"


def make_root(tmp: Path) -> Path:
    """A throw-away repository root (a git repo with one commit) holding copies of what the About tools read."""
    r = tmp / "repo"
    for rel in ("agentic_os/about_record.py", "agentic_os/__init__.py", "pyproject.toml", "packaging/check-distribution-gate.py",
                "packaging/identity.json", "packaging/distribution-gate.json", "deploy/launcher/src-tauri/tauri.conf.json",
                "deploy/launcher/src-tauri/Cargo.toml", "LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md"):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, r / rel)
    ui = r / "agentic_os" / "projects_ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>x</title>\n")
    (ui / "assets" / "index-TESTBUNDLE1.js").write_text("// bundle\n")
    (r / ".gitignore").write_text("__pycache__/\n")
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(r), *args], check=True, env=env, capture_output=True)
    return r


def make_notices(tmp: Path, with_reference: bool = True) -> Path:
    """Run the REAL notices generator on a made-up SBOM (alpha with a text, gamma without, a reference notice)."""
    site = tmp / "site"
    d = site / "alpha-1.0.dist-info"
    (d / "licenses").mkdir(parents=True)
    (d / "METADATA").write_text("Name: alpha\nVersion: 1.0\nLicense-Expression: MIT\n")
    (d / "licenses" / "LICENSE").write_text("SYNTHETIC licence text of alpha\n")
    g = site / "gamma-3.0.dist-info"
    g.mkdir()
    (g / "METADATA").write_text("Name: gamma\nVersion: 3.0\n")
    texts = tmp / "texts"
    texts.mkdir()
    (texts / "REF.txt").write_text("SYNTHETIC reference text\n")
    sbom = tmp / "sbom.cdx.json"
    sbom.write_text(json.dumps({
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "metadata": {"properties": [{"name": "redevops:limitation:L-X", "value": "yes"}]},
        "components": [
            {"type": "library", "name": "alpha", "version": "1.0", "purl": "pkg:pypi/alpha@1.0"},
            {"type": "library", "name": "gamma", "version": "3.0", "purl": "pkg:pypi/gamma@3.0"}]}))
    comps = tmp / "components.json"
    comps.write_text(json.dumps({"entries": {}, "static_components": [],
                                 "reference_notices": ([{"id": "libx", "limitation": "L-X", "title": "Lib X (synthetic)",
                                                         "declared": "MIT", "texts": ["REF.txt"], "decision": "D-LIC-6"}]
                                                       if with_reference else [])}))
    out = tmp / "notices-out"
    gen_notices.run(sbom, [site], [], texts, comps, out, "Synthetic", [])
    return out


def read_gate(root: Path) -> dict:
    return json.loads((root / "packaging" / "distribution-gate.json").read_text())


def write_gate(root: Path, doc: dict) -> None:
    (root / "packaging" / "distribution-gate.json").write_text(json.dumps(doc, indent=1))


def decide(root: Path, ids, *, texts: str = "ok") -> None:
    """Mark the gate entries `ids` DECIDED with a synthetic written record. texts: ok | none | missing | mismatch | na (section 13)."""
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] not in ids:
            continue
        rec = root / "docs" / "legal" / "decisions" / f"{d['id']}.signed.txt"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(f"synthetic signed record for {d['id']}\n")
        d.update(status="DECIDED", chosen_option=d["options"][0]["key"], decision_reference=rec.relative_to(root).as_posix(),
                 decision_sha256=hashlib.sha256(rec.read_bytes()).hexdigest(), decided_by="test-owner", decided_on="2026-12-01")
        if "counsel" in d["decides"]:
            d["counsel_reference"] = "synthetic counsel reference"
        mine = {s: ids_ for s, ids_ in SLOTS.items() if ids_[0] == d["id"]}
        if texts != "none" and mine:
            st = {}
            for slot in mine:
                f = root / "docs" / "legal" / "approved" / f"{slot.lower().replace('.', '-').replace('_', '-')}.en.txt"
                f.parent.mkdir(parents=True, exist_ok=True)
                body = SYNTH.format(slot=slot)
                if texts == "na" and slot == "LEGAL.SECTION13_NOTICE":
                    st[slot] = {"not_applicable": True}
                    continue
                f.write_text(body)
                sha = hashlib.sha256(body.encode()).hexdigest()
                if texts == "mismatch":
                    f.write_text(body + "edited after approval\n")
                if texts == "missing":
                    f.unlink()
                st[slot] = {"file": f.relative_to(root).as_posix(), "sha256": sha}
            d["slot_texts"] = st
    write_gate(root, doc)


def run_generate(root: Path, notices: Path, *, distribution: bool = False, out: Path | None = None, monkeypatch=None, extra=()):
    """Call generate_about.main on the fixture root. Returns the exit code."""
    if monkeypatch is not None:
        monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)
    args = ["--notices-dir", str(notices), "--platform", "macos-arm64", "--root", str(root), *extra]
    if out:
        args += ["--out", str(out)]
    if distribution:
        args.append("--distribution")
    return gen_about.main(args)


def stage_folder(root: Path, notices: Path, dest: Path) -> Path:
    """Stage like build-licenses.sh does: product files from `root`, generated files from `notices`."""
    assert stage.stage(notices, dest, root) == []
    return dest
