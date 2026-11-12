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

import pytest

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


NAMESPACE = "redevops-legal-decision@v1"


class Signer:
    """A THROWAWAY test key (generated in a temp dir, never a real key) and its allowed-signers file. The key files are deleted by name."""

    def __init__(self, tmp: Path, principal: str = "test-owner"):
        self.dir = tmp / "signer"
        self.dir.mkdir()
        self.key = self.dir / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "throwaway-test-key", "-f", str(self.key)], check=True,
                       env={"PATH": "/usr/bin:/bin"}, capture_output=True)
        pub = (self.dir / "key.pub").read_text().strip()
        self.allowed = self.dir / "allowed-signers"
        self.allowed.write_text(f'{principal} namespaces="{NAMESPACE}" {pub}\n')
        out = subprocess.run(["ssh-keygen", "-l", "-f", str(self.dir / "key.pub")], check=True, env={"PATH": "/usr/bin:/bin"},
                             capture_output=True, text=True).stdout
        self.fingerprint = out.split()[1]                       # SHA256:...

    def sign(self, path: Path, namespace: str = NAMESPACE, key: Path | None = None) -> Path:
        sig = Path(str(path) + ".sig")
        if sig.exists():
            sig.unlink()
        subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key or self.key), "-n", namespace, str(path)], check=True,
                       env={"PATH": "/usr/bin:/bin"}, capture_output=True)
        return sig

    def cleanup(self) -> None:
        for n in ("key", "key.pub", "allowed-signers"):
            try:
                (self.dir / n).unlink()
            except FileNotFoundError:
                pass


def sign_all(root: Path, signer: Signer) -> None:
    """Sign the record of every DECIDED entry and every approved slot text of the gate file under `root`."""
    for d in read_gate(root)["decisions"]:
        if d["status"] == "DECIDED":
            signer.sign(root / d["decision_reference"])
            for v in (d.get("slot_texts") or {}).values():
                if "file" in v and (root / v["file"]).is_file():
                    signer.sign(root / v["file"])



# ------------------------------------------------------------------------------------------------ the shipped policy BEFORE any owner signature
def pristine_gate_text(src: Path | None = None) -> str:
    """The distribution gate file as it ships BEFORE the owner signs anything: every decision OPEN, no record, no signer. The tests build their
    synthetic repositories from this, so they do not depend on whether the real repository already carries the owner's signed approvals."""
    doc = json.loads((src or ROOT / "packaging" / "distribution-gate.json").read_text(encoding="utf-8"))
    for d in doc["decisions"]:
        if d.get("status") == "DECIDED":
            d.update(status="OPEN", chosen_option=None, decision_reference=None, decision_sha256=None, decided_by=None, decided_on=None, counsel_reference=None)
        d.pop("slot_texts", None)
    doc["legal_signer_fingerprints"] = []
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def pristine_allowed_text(src: Path | None = None) -> str:
    """packaging/legal-allowed-signers as it ships before the owner's key is added: the comment lines only."""
    lines = (src or ROOT / "packaging" / "legal-allowed-signers").read_text(encoding="utf-8").splitlines()
    return "\n".join(ln for ln in lines if ln.lstrip().startswith("#") or not ln.strip()) + "\n"


def copy_shipped(rel: str, dest: Path) -> None:
    """Copy a repository file into a synthetic root; the gate file and the signer list are copied in their pre-signature form."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if rel == "packaging/distribution-gate.json":
        dest.write_text(pristine_gate_text(), encoding="utf-8")
    elif rel == "packaging/legal-allowed-signers":
        dest.write_text(pristine_allowed_text(), encoding="utf-8")
    else:
        shutil.copyfile(ROOT / rel, dest)


def make_root(tmp: Path) -> Path:
    """A throw-away repository root (a git repo with one commit) holding copies of what the About tools read."""
    r = tmp / "repo"
    for rel in ("agentic_os/about_record.py", "agentic_os/__init__.py", "pyproject.toml", "packaging/check-distribution-gate.py",
                "packaging/identity.json", "packaging/distribution-gate.json", "packaging/legal-allowed-signers", "deploy/launcher/src-tauri/tauri.conf.json",
                "deploy/launcher/src-tauri/Cargo.toml", "LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md"):
        copy_shipped(rel, r / rel)
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


def record_text(d: dict, slots: dict | None = None, extra: str = "synthetic signed record") -> str:
    """The machine-readable header of a decision record (see check-distribution-gate.py) plus a synthetic free-text body."""
    lines = ["redevops-legal-decision: 1", f"decision: {d['id']}", f"option: {d['chosen_option']}"]
    for slot, v in (slots if slots is not None else d.get("slot_texts") or {}).items():
        lines.append(f"slot: {slot} " + ("not-applicable" if v.get("not_applicable") else f"sha256 {v['sha256']}"))
    return "\n".join(lines) + f"\n\n{extra} for {d['id']}\n"


def decide(root: Path, ids, *, texts: str = "ok") -> None:
    """Mark the gate entries `ids` DECIDED with a synthetic written record (machine-readable header included). texts: ok | none | missing |
    mismatch | na (section 13). Nothing is signed here (see trust())."""
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] not in ids:
            continue
        rec = root / "docs" / "legal" / "decisions" / f"{d['id']}.signed.txt"
        rec.parent.mkdir(parents=True, exist_ok=True)
        d.update(status="DECIDED", chosen_option=d["options"][0]["key"], decision_reference=rec.relative_to(root).as_posix(),
                 decided_by="test-owner", decided_on="2026-12-01")
        if "counsel" in d["decides"]:
            d["counsel_reference"] = "synthetic counsel reference"
        mine = {s_: ids_ for s_, ids_ in SLOTS.items() if ids_[0] == d["id"]}
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
        rec.write_text(record_text(d))
        d["decision_sha256"] = hashlib.sha256(rec.read_bytes()).hexdigest()
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


@pytest.fixture()
def signer(tmp_path):
    s = Signer(tmp_path)
    yield s
    s.cleanup()


def trust(root: Path, signer: Signer) -> None:
    """Make `signer` the allowed signer of the fixture repository `root` (allowed-signers file AND the gate file's fingerprint list) and
    sign every DECIDED record and approved text in it."""
    (root / "packaging" / "legal-allowed-signers").write_text(signer.allowed.read_text())
    doc = read_gate(root)
    doc["legal_signer_fingerprints"] = [signer.fingerprint]
    write_gate(root, doc)
    sign_all(root, signer)


def licenses_for(root: Path, work: Path, *, distribution: bool = False, platform: str = "macos-arm64") -> Path:
    """A staged Licenses folder (real notices generator on a made-up SBOM, real about generator, real staging) consistent with the
    repository `root` (identity, versions, gate). `root` becomes a git repository on first use."""
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(work), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    work.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        (root / ".gitignore").write_text("__pycache__/\n")
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)
    notices = make_notices(work)
    args = ["--notices-dir", str(notices), "--platform", platform, "--root", str(root)] + (["--distribution"] if distribution else [])
    rc = gen_about.main(args)
    assert rc == 0, rc
    return stage_folder(root, notices, work / "Licenses")
