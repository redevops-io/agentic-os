#!/usr/bin/env python3
"""DEVELOPMENT-ONLY end-to-end test of the About / Legal decision-signature mechanism, with a DISPOSABLE signer.

  python3 packaging/notices/tools/dev_signer_e2e.py [--repo-root DIR] [--tmp-base DIR] [--json FILE]

WHAT THIS IS. A harness that proves, with the REAL gate, generator, checker and sidecar code (no mocks), that the legal-decision
signature mechanism works: an all-signed fixture is "approved-verified" and passes a distribution gate; a tampered byte, a signature by a
key that is not allow-listed, a record reused for another decision, a wrong namespace, a missing signature and a signer-list mismatch
are each refused (or shown only as a labelled "Unverified draft"). See docs/ABOUT_LEGAL_DEV_SIGNER.md.

WHAT THIS IS NOT. It is not a signer, it selects no production signer, it decides nothing and it clears nothing. Everything it signs is
a SYNTHETIC record or placeholder text inside a temporary FIXTURE repository root ("DEV FIXTURE TEXT - NOT LEGAL TEXT"). The throwaway
key is generated inside the temporary directory (never in ~/.ssh, never in an agent, mode 0600), is allow-listed only in the fixture's own
allowed-signers file and is deleted, file by file, in a finally block. The production files (packaging/legal-allowed-signers,
`legal_signer_fingerprints` and the decisions of packaging/distribution-gate.json) are never written; the harness asserts at the start and
at the end that they are unchanged, that the allowed-signers file lists no key, that the fingerprint list is empty, that every production
decision is OPEN, and that no private key file exists in the repository or in the temporary directory.

Exit codes: 0 = every scenario gave the expected outcome and the production state was untouched; 1 = an outcome differed (or the
production state changed); 4 = the harness refused to start (production state inconsistent, key location inside the repository or in an
SSH/GPG directory, ssh-keygen missing). Standard library for the harness itself; the sidecar part needs the project's own dependencies.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NAMESPACE = "redevops-legal-decision@v1"
OTHER_NAMESPACE = "redevops-legal-decision-OTHER@v1"
SAFE_PATH = "/usr/bin:/bin:/opt/homebrew/bin"
KEY_COMMENT = "DEV-ONLY disposable signer"
KEY2_COMMENT = "DEV-ONLY second disposable signer (not allow-listed)"
FIXTURE_TEXT = "DEV FIXTURE TEXT - NOT LEGAL TEXT. Placeholder for {slot}; written by dev_signer_e2e.py only.\n"
FIXTURE_RECORD_BODY = "DEV FIXTURE RECORD - NOT A DECISION. Synthetic file for {id}; written by dev_signer_e2e.py only.\n"
EXIT_REFUSED = 4

# files copied (byte for byte) from the repository into the fixture root: what the real tools read
FIXTURE_COPIES = ("agentic_os/about_record.py", "agentic_os/__init__.py", "pyproject.toml", "packaging/check-distribution-gate.py",
                  "packaging/identity.json", "packaging/distribution-gate.json", "deploy/launcher/src-tauri/tauri.conf.json",
                  "deploy/launcher/src-tauri/Cargo.toml", "LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md")
CODE_COPIES = ("agentic_os/about_record.py", "packaging/check-distribution-gate.py")      # must stay byte-identical to the repository

_LOG: list = []                    # every subprocess argv that was run (the tests check it: no agent, no ~/.ssh)
_LOG_LOCK = threading.Lock()


class Refused(Exception):
    """The harness refuses to run (exit 4)."""


# ------------------------------------------------------------------------------------------------------------ small helpers
def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_REC = None


def rec_module():
    """agentic_os/about_record.py of the repository (the shared validator the launcher view contract is built on), loaded once."""
    global _REC
    with _LOG_LOCK:
        if _REC is None:
            _REC = _load(REPO / "agentic_os" / "about_record.py", "_dev_about_record")
    return _REC


_STAGE = None


def stage_module():
    global _STAGE
    with _LOG_LOCK:
        if _STAGE is None:
            _STAGE = _load(REPO / "packaging" / "notices" / "stage_licenses.py", "_dev_stage")
    return _STAGE


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def run(cmd: list, *, env: dict, input: Optional[bytes] = None, timeout: int = 180) -> subprocess.CompletedProcess:
    """The ONLY way this harness starts a process: explicit minimal environment (never SSH_AUTH_SOCK / SSH_AGENT_PID), no stdin unless
    given, a time limit, the argv logged."""
    assert "SSH_AUTH_SOCK" not in env and "SSH_AGENT_PID" not in env, "an agent socket must never reach a child process"
    with _LOG_LOCK:
        _LOG.append([str(c) for c in cmd])
    return subprocess.run([str(c) for c in cmd], env=env, input=input, stdin=None if input is not None else subprocess.DEVNULL,
                          capture_output=True, timeout=timeout)


def clean_env(home: Path) -> dict:
    return {"PATH": SAFE_PATH, "HOME": str(home), "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def remove_tree_by_name(top: Path) -> int:
    """Delete `top` WITHOUT rm -rf: every file (and symlink, never followed) individually, then the directories bottom-up. Returns the
    number of files removed."""
    n = 0
    top = Path(top)
    if not os.path.lexists(top):
        return 0
    for dirpath, dirnames, filenames in os.walk(top, topdown=False, followlinks=False):
        for fn in filenames:
            os.unlink(os.path.join(dirpath, fn))
            n += 1
        for dn in dirnames:
            p = os.path.join(dirpath, dn)
            if os.path.islink(p):
                os.unlink(p)
                n += 1
            else:
                os.rmdir(p)
    os.rmdir(top)
    return n


# ------------------------------------------------------------------------------------------------------------ production state
def _git_porcelain(root: Path) -> str:
    if not (root / ".git").exists():
        return ""
    r = run(["git", "-C", root, "status", "--porcelain", "--untracked-files=all"], env=clean_env(Path(tempfile.gettempdir())))
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _candidate_files(root: Path) -> list:
    if (root / ".git").exists():
        r = run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"], env=clean_env(Path(tempfile.gettempdir())))
        if r.returncode == 0:
            return [root / p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        out += [Path(dirpath) / f for f in filenames]
    return out


def find_private_keys(root: Path) -> list:
    """Files below `root` that look like private key material: a name like an OpenSSH/PEM key file, or content that STARTS with a
    `-----BEGIN ... PRIVATE KEY-----` line (the marker inside a test or a document is not at the start of a file)."""
    found = []
    for p in _candidate_files(Path(root)):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            name = p.name
            if re.fullmatch(r"id_(ed25519|rsa|ecdsa|dsa)", name):
                found.append(str(p))
                continue
            with open(p, "rb") as fh:
                head = fh.read(80)
            if head.startswith(b"-----BEGIN") and b"PRIVATE KEY-----" in head.split(b"\n")[0]:
                found.append(str(p))
        except OSError:
            continue
    return sorted(found)


def production_state(root: Path) -> dict:
    root = Path(root)
    allowed = root / "packaging" / "legal-allowed-signers"
    gate = root / "packaging" / "distribution-gate.json"
    doc = json.loads(gate.read_text(encoding="utf-8"))
    text = allowed.read_text(encoding="utf-8")
    active = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    decisions = doc.get("decisions", [])
    carrying = [d.get("id") for d in decisions if any(d.get(k) for k in ("chosen_option", "decision_reference", "decision_sha256",
                                                                          "decided_by", "decided_on", "slot_texts"))]
    legal_files = []
    for sub in ("docs/legal/approved", "docs/legal/decisions"):
        d = root / sub
        if d.is_dir():
            legal_files += [str(p.relative_to(root)) for p in d.rglob("*") if p.is_file()]
    return {"allowed_signers_sha256": sha256_file(allowed), "allowed_signers_active_lines": len(active),
            "gate_file_sha256": sha256_file(gate), "fingerprints": list(doc.get("legal_signer_fingerprints", [])),
            "decisions_total": len(decisions), "decisions_open": sum(1 for d in decisions if d.get("status") == "OPEN"),
            "decisions_decided": sum(1 for d in decisions if d.get("status") == "DECIDED"), "decisions_with_decision_fields": carrying,
            "legal_text_or_record_files": sorted(legal_files), "private_keys_in_repo": find_private_keys(root),
            "git_porcelain_sha256": hashlib.sha256(_git_porcelain(root).encode()).hexdigest()}


def production_violations(st: dict) -> list:
    """The harness never writes the production files, so it may run in a repository that already carries the owner's signed approvals (it builds
    its own fixture from the shipped policy). It refuses only when the production signer list and the pinned fingerprints disagree in number
    (a half-configured policy) or when private key material lies in the repository."""
    v = []
    if st["allowed_signers_active_lines"] != len(st["fingerprints"]):
        v.append(f"packaging/legal-allowed-signers lists {st['allowed_signers_active_lines']} signer line(s) but legal_signer_fingerprints pins {len(st['fingerprints'])}")
    if st["decisions_total"] == 0:
        v.append("the production gate file lists no decision")
    if st["private_keys_in_repo"]:
        v.append(f"private key material found in the repository: {st['private_keys_in_repo']}")
    return v


def check_tmp_base(base: Path, repo: Path) -> Path:
    """The directory in which the temporary workspace (and with it the throwaway key) is created. Refused: anywhere inside the
    repository, inside an SSH or GPG directory, or a path that is not an existing directory."""
    base = Path(base)
    if not base.is_dir():
        raise Refused(f"--tmp-base {base} is not an existing directory")
    real = base.resolve()
    if real == repo.resolve() or repo.resolve() in real.parents:
        raise Refused(f"the temporary directory {real} is inside the repository {repo.resolve()}: a key must never be created there")
    home = Path(os.path.expanduser("~")).resolve()
    for forbidden in (home / ".ssh", home / ".gnupg"):
        if real == forbidden or forbidden in real.parents:
            raise Refused(f"the temporary directory {real} is inside {forbidden}")
    if any(part in (".ssh", ".gnupg") for part in real.parts):
        raise Refused(f"the temporary directory {real} is inside an SSH or GPG directory")
    return real


# ------------------------------------------------------------------------------------------------------------ the throwaway signer
class Signer:
    """A THROWAWAY ed25519 key inside `directory`: no passphrase, mode 0600, never in an agent, deleted by name."""

    def __init__(self, directory: Path, stem: str, comment: str):
        self.dir = Path(directory)
        self.dir.mkdir(mode=0o700, exist_ok=True)
        self.key, self.pub = self.dir / stem, self.dir / (stem + ".pub")
        self.env = clean_env(self.dir.parent / "home")
        (self.dir.parent / "home").mkdir(mode=0o700, exist_ok=True)
        r = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", self.key], env=self.env)
        if r.returncode != 0:
            raise Refused("ssh-keygen could not create the throwaway key: " + r.stderr.decode("utf-8", "replace")[:200])
        os.chmod(self.key, 0o600)
        mode = stat.S_IMODE(os.stat(self.key).st_mode)
        if mode != 0o600:
            raise Refused(f"the throwaway key has mode {oct(mode)}, expected 0o600")
        fp = run(["ssh-keygen", "-l", "-f", self.pub], env=self.env)
        self.fingerprint = fp.stdout.decode().split()[1]
        self.allowed_line = f'dev-fixture-signer namespaces="{NAMESPACE}" ' + self.pub.read_text(encoding="utf-8").strip() + "\n"
        self.deleted = False

    def sign(self, path: Path, namespace: str = NAMESPACE) -> Path:
        sig = Path(str(path) + ".sig")
        if sig.exists():
            sig.unlink()
        r = run(["ssh-keygen", "-Y", "sign", "-q", "-f", self.key, "-n", namespace, path], env=self.env)
        if r.returncode != 0:
            raise RuntimeError("signing failed: " + r.stderr.decode("utf-8", "replace")[:200])
        return sig

    def delete(self) -> list:
        gone = []
        for p in (self.key, self.pub):
            try:
                p.unlink()
                gone.append(p.name)
            except FileNotFoundError:
                pass
        try:
            self.dir.rmdir()
        except OSError:
            pass
        self.deleted = not self.key.exists() and not self.pub.exists()
        return gone


# ------------------------------------------------------------------------------------------------------------ the fixture
def slot_file(slot: str) -> str:
    return "docs/legal/approved/" + slot.lower().replace(".", "-").replace("_", "-") + ".en.txt"


class Fixture:
    """A fixture repository root: copies of the files the tools read, a gate file with DEV FIXTURE decisions, synthetic records and texts."""

    def __init__(self, root: Path, key: Optional[Signer] = None):
        self.root, self.key = Path(root), key

    @property
    def gate_path(self) -> Path:
        return self.root / "packaging" / "distribution-gate.json"

    @property
    def allowed_path(self) -> Path:
        return self.root / "packaging" / "legal-allowed-signers"

    def gate(self) -> dict:
        return json.loads(self.gate_path.read_text(encoding="utf-8"))

    def write_gate(self, doc: dict) -> None:
        self.gate_path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")

    def entry(self, doc: dict, did: str) -> dict:
        return next(d for d in doc["decisions"] if d["id"] == did)

    def record_rel(self, did: str) -> str:
        return f"docs/legal/decisions/{did}.signed.txt"

    def record_text(self, d: dict) -> str:
        lines = ["redevops-legal-decision: 1", f"decision: {d['id']}", f"option: {d['chosen_option']}"]
        for slot, v in (d.get("slot_texts") or {}).items():
            lines.append(f"slot: {slot} " + ("not-applicable" if v.get("not_applicable") else f"sha256 {v['sha256']}"))
        return "\n".join(lines) + "\n\n" + FIXTURE_RECORD_BODY.format(id=d["id"])

    def rewrite_record(self, doc: dict, did: str) -> None:
        d = self.entry(doc, did)
        p = self.root / d["decision_reference"]
        p.write_text(self.record_text(d), encoding="utf-8")
        d["decision_sha256"] = sha256_file(p)

    def sign(self, rel: str, signer: Optional[Signer] = None, namespace: str = NAMESPACE) -> None:
        (signer or self.key).sign(self.root / rel, namespace)

    def flip_last_byte(self, rel: str) -> None:
        p = self.root / rel
        b = bytearray(p.read_bytes())
        b[-2] ^= 0x01                                   # the byte before the final newline; stays printable ASCII
        assert 32 <= b[-2] < 127
        p.write_bytes(bytes(b))


def build_baseline(slots_spec: dict, tmp: Path, root: Path, signer: Signer, decided_on: str = "2000-01-01") -> Fixture:
    """All decisions DECIDED (in the fixture only) with synthetic signed records and synthetic approved texts, all signed by `signer`."""
    for rel in FIXTURE_COPIES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    for rel in CODE_COPIES:
        assert sha256_file(root / rel) == sha256_file(REPO / rel), f"{rel} in the fixture differs from the repository code"
    ui = root / "agentic_os" / "projects_ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html><title>dev fixture</title>\n", encoding="utf-8")
    (ui / "assets" / "index-DEVFIXTURE1.js").write_text("// dev fixture bundle\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    fx = Fixture(root, signer)
    doc = fx.gate()
    for d in doc["decisions"]:                               # the production file may already carry the owner's signed decisions: the fixture starts clean
        for k in ("chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on", "counsel_reference", "slot_texts"):
            d.pop(k, None)
    doc["purpose"] = "DEV FIXTURE gate file written by dev_signer_e2e.py: every decision below is a SYNTHETIC fixture entry, not a decision."
    doc["legal_signer_fingerprints"] = [signer.fingerprint]
    fx.allowed_path.write_text(
        "# DEV FIXTURE allowed-signers: the public half of a DISPOSABLE key that exists only inside one harness run.\n" + signer.allowed_line,
        encoding="utf-8")
    for d in doc["decisions"]:
        did = d["id"]
        d["title"] = "[DEV FIXTURE] " + d["title"]
        d.update(status="DECIDED", chosen_option=d["options"][0]["key"], decision_reference=fx.record_rel(did),
                 decided_by="DEV FIXTURE (disposable signer, not a decision)", decided_on=decided_on)
        if "counsel" in d["decides"]:
            d["counsel_reference"] = "DEV FIXTURE - no counsel involved"
        mine = [s for s, spec in slots_spec.items() if spec["decisions"][0] == did]
        if mine:
            st = {}
            for slot in mine:
                p = root / slot_file(slot)
                p.parent.mkdir(parents=True, exist_ok=True)
                body = FIXTURE_TEXT.format(slot=slot)
                p.write_text(body, encoding="utf-8")
                st[slot] = {"file": slot_file(slot), "sha256": hashlib.sha256(body.encode()).hexdigest()}
            d["slot_texts"] = st
        (root / fx.record_rel(did)).parent.mkdir(parents=True, exist_ok=True)
        (root / fx.record_rel(did)).write_text(fx.record_text(d), encoding="utf-8")
        d["decision_sha256"] = sha256_file(root / fx.record_rel(did))
    fx.write_gate(doc)
    for d in doc["decisions"]:
        fx.sign(d["decision_reference"])
        for v in (d.get("slot_texts") or {}).values():
            fx.sign(v["file"])
    env = clean_env(tmp / "home")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=dev-fixture", "-c", "user.email=dev-fixture@example.invalid",
                                                  "commit", "-qm", "dev fixture"]):
        r = run(["git", "-C", root, *args], env=env)
        assert r.returncode == 0, r.stderr
    return fx


def make_notices(tmp: Path, gen_notices) -> Path:
    """Run the REAL notices generator on a made-up SBOM (one component with a text, one without, one reference notice)."""
    site = tmp / "site"
    d = site / "alpha-1.0.dist-info"
    (d / "licenses").mkdir(parents=True)
    (d / "METADATA").write_text("Name: alpha\nVersion: 1.0\nLicense-Expression: MIT\n", encoding="utf-8")
    (d / "licenses" / "LICENSE").write_text("DEV FIXTURE licence text of alpha\n", encoding="utf-8")
    g = site / "gamma-3.0.dist-info"
    g.mkdir()
    (g / "METADATA").write_text("Name: gamma\nVersion: 3.0\n", encoding="utf-8")
    texts = tmp / "texts"
    texts.mkdir()
    (texts / "REF.txt").write_text("DEV FIXTURE reference text\n", encoding="utf-8")
    sbom = tmp / "sbom.cdx.json"
    sbom.write_text(json.dumps({
        "bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {"properties": [{"name": "redevops:limitation:L-X", "value": "yes"}]},
        "components": [{"type": "library", "name": "alpha", "version": "1.0", "purl": "pkg:pypi/alpha@1.0"},
                       {"type": "library", "name": "gamma", "version": "3.0", "purl": "pkg:pypi/gamma@3.0"}]}), encoding="utf-8")
    comps = tmp / "components.json"
    comps.write_text(json.dumps({"entries": {}, "static_components": [],
                                 "reference_notices": [{"id": "libx", "limitation": "L-X", "title": "Lib X (dev fixture)",
                                                        "declared": "MIT", "texts": ["REF.txt"], "decision": "D-LIC-6"}]}), encoding="utf-8")
    out = tmp / "notices-baseline"
    gen_notices.run(sbom, [site], [], texts, comps, out, "DEV FIXTURE", [])
    return out


# ------------------------------------------------------------------------------------------------------------ scenarios
AV, UD, PD, NA = "approved-verified", "unverified-draft", "pending", "not-applicable"
ABBR = {AV: "AV", UD: "UD", PD: "PD", NA: "NA"}


def scenarios(slots_spec: dict, key2: Signer) -> list:
    """(key, title, mutate(fx), expected) -- expected: gp/gd = gate private/distribution exit, np/nd = generator private/distribution exit,
    states = {slot: state} of the private About record (None = no record is written), http = also check the sidecar."""
    def states(**over):
        s = {k: AV for k in slots_spec}
        s.update({k: v for k, v in over.items()})
        return s
    NW, S13 = "LEGAL.NO_WARRANTY", "LEGAL.SECTION13_NOTICE"
    rec10 = "docs/legal/decisions/D-LIC-10.signed.txt"
    txt10 = slot_file(NW)

    def baseline(fx): pass

    def tamper_record_only(fx): fx.flip_last_byte(rec10)

    def tamper_text_only(fx): fx.flip_last_byte(txt10)

    def tamper_record_rehash(fx):
        fx.flip_last_byte(rec10)
        doc = fx.gate()
        fx.entry(doc, "D-LIC-10")["decision_sha256"] = sha256_file(fx.root / rec10)
        fx.write_gate(doc)

    def tamper_text_rehash(fx):
        fx.flip_last_byte(txt10)
        doc = fx.gate()
        fx.entry(doc, "D-LIC-10")["slot_texts"][NW]["sha256"] = sha256_file(fx.root / txt10)
        fx.write_gate(doc)

    def second_key(fx):
        fx.sign(rec10, key2)
        fx.sign(txt10, key2)

    def record_reused(fx):
        doc = fx.gate()
        d5 = fx.entry(doc, "D-LIC-5")
        target = d5["decision_reference"]
        shutil.copyfile(fx.root / rec10, fx.root / target)                       # the signed record of D-LIC-10 ...
        shutil.copyfile(fx.root / (rec10 + ".sig"), fx.root / (target + ".sig"))  # ... with its signature, used for D-LIC-5
        d5["decision_sha256"] = sha256_file(fx.root / target)
        fx.write_gate(doc)

    def option_changed(fx):
        doc = fx.gate()
        fx.entry(doc, "D-LIC-10")["chosen_option"] = "b"                         # the signed record names option a
        fx.write_gate(doc)

    def wrong_namespace(fx):
        fx.sign(rec10, namespace=OTHER_NAMESPACE)
        fx.sign(txt10, namespace=OTHER_NAMESPACE)

    def missing_record_sig(fx): os.unlink(fx.root / (rec10 + ".sig"))

    def missing_text_sig(fx): os.unlink(fx.root / (txt10 + ".sig"))

    def signer_list_mismatch(fx):
        doc = fx.gate()
        doc["legal_signer_fingerprints"] = [key2.fingerprint]                    # the pinned list names another key than the file lists
        fx.write_gate(doc)

    def section13_not_applicable(fx):
        doc = fx.gate()
        d5 = fx.entry(doc, "D-LIC-5")
        d5["slot_texts"] = {S13: {"not_applicable": True}}
        fx.rewrite_record(doc, "D-LIC-5")
        fx.write_gate(doc)
        os.unlink(fx.root / slot_file(S13))
        os.unlink(fx.root / (slot_file(S13) + ".sig"))
        fx.sign(d5["decision_reference"])

    def one_open(fx):
        doc = fx.gate()
        d = fx.entry(doc, "D-LIC-10")
        d["status"] = "OPEN"
        for k in ("chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on", "counsel_reference", "slot_texts"):
            d.pop(k, None)
        fx.write_gate(doc)

    draft = dict(gp=0, gd=1, np=0, nd=3)
    return [
        ("all-signed", "all five slots signed by the allow-listed key", baseline,
         dict(gp=0, gd=0, np=0, nd=0, states=states(), http=True)),
        ("tamper-record", "one byte of a signed record changed (entry hash not updated)", tamper_record_only,
         dict(gp=2, gd=2, np=2, nd=2, states=None)),
        ("tamper-text", "one byte of an approved text changed (entry hash not updated)", tamper_text_only,
         dict(gp=2, gd=2, np=2, nd=2, states=None)),
        ("tamper-record+hash", "one record byte changed AND its entry hash rewritten", tamper_record_rehash,
         dict(draft, states=states(**{NW: UD}))),
        ("tamper-text+hash", "one text byte changed AND its entry hash rewritten", tamper_text_rehash,
         dict(draft, states=states(**{NW: UD}))),
        ("second-key", "signed by a second throwaway key that is not allow-listed", second_key,
         dict(draft, states=states(**{NW: UD}), http=True)),
        ("record-reused", "the signed record of D-LIC-10 reused for D-LIC-5", record_reused,
         dict(draft, states=states(**{S13: UD}))),
        ("option-changed", "chosen option in the entry differs from the signed record", option_changed,
         dict(draft, states=states(**{NW: UD}))),
        ("wrong-namespace", "signed in another namespace", wrong_namespace, dict(draft, states=states(**{NW: UD}))),
        ("missing-record-sig", "record .sig file missing", missing_record_sig,
         dict(draft, states=states(**{NW: UD}), http=True)),
        ("missing-text-sig", "approved text .sig file missing", missing_text_sig, dict(draft, states=states(**{NW: UD}))),
        ("fingerprint-mismatch", "legal_signer_fingerprints names another key than the allowed-signers file", signer_list_mismatch,
         dict(draft, states=states(**{k: UD for k in slots_spec}))),
        ("section13-na", "Section 13 slot recorded as 'no statement', signed", section13_not_applicable,
         dict(gp=0, gd=0, np=0, nd=0, states=states(**{S13: NA}))),
        ("decision-open", "one slot decision (D-LIC-10) left OPEN", one_open,
         dict(draft, states=states(**{NW: PD}), http=True)),
    ]


# ------------------------------------------------------------------------------------------------------------ running one row
class Row:
    def __init__(self, key: str, title: str, expected: dict):
        self.key, self.title, self.expected = key, title, expected
        self.got: dict = {}
        self.states: Optional[dict] = None
        self.detail: list = []
        self.problems: list = []
        self.checkabout = "-"
        self.http: Optional[dict] = None
        self.root: Optional[Path] = None
        self.licenses: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return not self.problems


def _first(stderr: bytes, pat: str) -> str:
    for ln in stderr.decode("utf-8", "replace").splitlines():
        if re.search(pat, ln):
            return re.sub(r"\s+", " ", ln.strip())[:170]
    return ""


def evaluate(row: Row, fx: Fixture, tmp: Path, notices_base: Path, py: str) -> None:
    """Run the REAL gate, generator and checker (subprocesses, python -I) on the fixture root, in private and distribution mode."""
    env = clean_env(tmp / "home")
    root = fx.root
    scen = root.parent
    gate_cmd = [py, "-I", REPO / "packaging" / "check-distribution-gate.py", "--gate", fx.gate_path, "--root", root,
                "--allowed-signers", fx.allowed_path, "--platform", "all", "--about-slots"]
    r = run(gate_cmd, env=env)
    row.got["gp"] = r.returncode
    w = _first(r.stderr, r"WARNING|PROBLEM")
    if w:
        row.detail.append("gate (private): " + w)
    r = run(gate_cmd + ["--distribution"], env=env)
    row.got["gd"] = r.returncode
    d = _first(r.stderr, r"^\s+- |PROBLEM|REFUSED")
    if r.returncode and d:
        row.detail.append("gate (distribution): " + d)
    if r.returncode == 1:
        d2 = _first(b"\n".join(r.stderr.splitlines()[2:3]), r".")
        if d2:
            row.detail.append("  first refusal: " + d2)
    notices = scen / "notices"
    shutil.copytree(notices_base, notices)
    gen = [py, "-I", REPO / "packaging" / "notices" / "generate_about.py", "--notices-dir", notices, "--platform", "macos-arm64",
           "--root", root, "--gate", fx.gate_path]
    r = run(gen, env=env)
    row.got["np"] = r.returncode
    m = _first(r.stderr, r"WARNING|ABOUT")
    if m and r.returncode:
        row.detail.append("generator (private): " + m)
    if r.returncode == 0:
        doc = json.loads((notices / "about.json").read_text(encoding="utf-8"))
        row.states = {k: v["state"] for k, v in doc["legal"]["slots"].items()}
        rec = rec_module()
        bad = rec.validate_record(doc)                                      # the launcher view contract (shared validator)
        if bad:
            row.problems.append(f"validate_record rejected the private record: {bad[:2]}")
    outd = scen / "dist-out"
    outd.mkdir()
    r = run(gen + ["--out", outd, "--distribution"], env=env)
    row.got["nd"] = r.returncode
    m = _first(r.stderr, r"ABOUT")
    if r.returncode:
        row.detail.append("generator (distribution): " + m)
    if r.returncode == 0:
        if not (outd / "about.json").is_file():
            row.problems.append("distribution generator exit 0 but no about.json")
        else:
            rec = rec_module()
            doc = json.loads((outd / "about.json").read_text(encoding="utf-8"))
            if rec.validate_record(doc) or doc["build"]["mode"] != "distribution":
                row.problems.append("the distribution record is not valid / not a distribution record")
            else:
                stage = stage_module()
                dn = scen / "notices-dist"
                shutil.copytree(notices_base, dn)
                shutil.copyfile(outd / "about.json", dn / "about.json")
                lic = scen / "Licenses-dist"
                assert stage.stage(dn, lic, root) == []
                c = run([py, "-I", REPO / "packaging" / "notices" / "check_about.py", "--licenses-dir", lic, "--root", root,
                         "--gate", fx.gate_path, "--allowed-signers", fx.allowed_path, "--distribution"], env=env)
                row.checkabout = "pass" if c.returncode == 0 else f"exit {c.returncode}"
                if c.returncode:
                    row.problems.append("check_about.py --distribution failed on the distribution record: "
                                        + c.stderr.decode()[:200])
    elif (outd / "about.json").exists():
        row.problems.append("a refused distribution build left an about.json behind")
    # the staged Licenses folder of the PRIVATE record (for the sidecar check)
    if row.states is not None:
        stage = stage_module()
        lic = scen / "Licenses"
        assert stage.stage(notices, lic, root) == []
        row.licenses = lic
        c = run([py, "-I", REPO / "packaging" / "notices" / "check_about.py", "--licenses-dir", lic, "--root", root, "--gate", fx.gate_path,
                 "--allowed-signers", fx.allowed_path], env=env)
        if c.returncode:
            row.problems.append("check_about.py (private) failed on the private record: " + c.stderr.decode()[:200])
    row.root = root
    for k in ("gp", "gd", "np", "nd"):
        if row.got.get(k) != row.expected[k]:
            row.problems.append(f"{k}: exit {row.got.get(k)} but {row.expected[k]} expected")
    if row.expected.get("states") != row.states:
        row.problems.append(f"slot states {row.states} but {row.expected.get('states')} expected")


# ------------------------------------------------------------------------------------------------------------ the sidecar
class InProcessSidecar:
    """The sidecar app from SOURCE (agentic_os.projects_api.mounted_app with the public /about/ routes), served by uvicorn in this process
    on an ephemeral 127.0.0.1 port, reading the Licenses folder given."""

    def __init__(self, licenses: Path):
        import uvicorn
        from agentic_os import about_ui
        from agentic_os.local_capability import LocalCapability
        from agentic_os.projects_api import SampleProjectionProvider, mounted_app
        os.environ.pop("AGENT_GATEWAY_TOKEN", None)
        self._about_ui, self._orig = about_ui, about_ui.find_licenses_dir
        about_ui.find_licenses_dir = lambda: licenses
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        self.port = sock.getsockname()[1]
        app = mounted_app("", SampleProjectionProvider(), capability=LocalCapability(port=self.port))
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[sock]), daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("the in-process sidecar did not start")

    def get(self, path: str):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        hdr = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdr, body

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)
        self._about_ui.find_licenses_dir = self._orig


def check_sidecar(row: Row, gate_mod) -> dict:
    """Fetch /about/, /about/about.json and the viewer endpoint /about/files/licence and check what is DISPLAYED per slot."""
    rec = rec_module()
    srv = InProcessSidecar(row.licenses)
    out = {"port": srv.port, "checks": []}
    try:
        st, hdr, body = srv.get("/about/")
        out["checks"].append(f"GET /about/ -> {st} {hdr.get('content-type', '')}")
        if st != 200 or b"<html" not in body.lower():
            row.problems.append(f"/about/ answered {st}")
        st, hdr, body = srv.get("/about/about.json")
        out["checks"].append(f"GET /about/about.json -> {st} {len(body)} bytes")
        if st != 200:
            row.problems.append(f"/about/about.json answered {st}")
            return out
        if body != (row.licenses / "about.json").read_bytes():
            row.problems.append("/about/about.json is not byte-identical to the staged record")
        doc = json.loads(body)
        bad = rec.validate_record(doc)
        out["checks"].append("validate_record (launcher view contract): " + ("accepted" if not bad else f"REJECTED {bad[:2]}"))
        if bad:
            row.problems.append(f"validate_record rejected the served record: {bad[:2]}")
        for slot, s in doc["legal"]["slots"].items():
            want = row.expected["states"][slot]
            tag, text = s["tag"], s["text"]
            shown = f"{slot}: {s['state']} / tag '{tag}'"
            if s["state"] != want or tag != gate_mod.TAGS[want]:
                row.problems.append(f"{slot}: served state {s['state']!r}/tag {tag!r}, expected {want!r}")
            if want == AV:
                exp = (row.root / slot_file(slot)).read_text(encoding="utf-8")
                if text != exp or "DEV FIXTURE TEXT - NOT LEGAL TEXT" not in text:
                    row.problems.append(f"{slot}: an approved-verified slot does not display exactly the approved fixture text")
                shown += " / text = the fixture text, no draft label"
            elif want == UD:
                if not text.startswith(gate_mod.DRAFT_PREFIX) or "DEV FIXTURE TEXT" not in text:
                    row.problems.append(f"{slot}: an unverified slot is displayed WITHOUT the draft label")
                shown += " / text only after the label 'Unverified draft, not an approved text.'"
            elif want == PD:
                if not text.startswith("Legal text pending decision") or "DEV FIXTURE TEXT" in text:
                    row.problems.append(f"{slot}: a pending slot does not show the neutral placeholder")
                shown += " / neutral placeholder, no text file opened"
            out["checks"].append(shown)
        st, hdr, body = srv.get("/about/files/licence")
        out["checks"].append(f"viewer endpoint GET /about/files/licence -> {st} {hdr.get('content-type', '')}")
        if st != 200 or body != (row.root / "LICENSE").read_bytes():
            row.problems.append("the viewer endpoint did not return the licence file byte-exact")
    finally:
        srv.stop()
    return out


# ------------------------------------------------------------------------------------------------------------ main
def render_table(rows: list) -> str:
    def states(r: Row) -> str:
        if r.states is None:
            return "(no record)"
        c = {}
        for v in r.states.values():
            c[ABBR[v]] = c.get(ABBR[v], 0) + 1
        return " ".join(f"{k}:{c[k]}" for k in ("AV", "UD", "PD", "NA") if k in c)
    head = ("scenario", "gate private", "gate distribution", "About private (slots)", "About distribution", "check_about --dist", "verdict")
    data = [head]
    for r in rows:
        g = r.got
        data.append((r.key, f"exit {g['gp']}", f"exit {g['gd']}", f"exit {g['np']}  {states(r)}", f"exit {g['nd']}", r.checkabout,
                     "as expected" if r.ok else "UNEXPECTED"))
    w = [max(len(str(x[i])) for x in data) for i in range(len(head))]
    line = "+-" + "-+-".join("-" * n for n in w) + "-+"
    out = [line]
    for i, x in enumerate(data):
        out.append("| " + " | ".join(str(c).ljust(w[j]) for j, c in enumerate(x)) + " |")
        if i == 0:
            out.append(line)
    out.append(line)
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo-root", type=Path, default=REPO, help="the repository whose PRODUCTION state is asserted untouched (tests only)")
    ap.add_argument("--tmp-base", type=Path, help="directory for the temporary workspace and key (default: the system temp dir); "
                    "refused inside the repository or an SSH/GPG directory")
    ap.add_argument("--json", type=Path, help="also write the results as JSON to this file")
    ap.add_argument("--no-sidecar", action="store_true", help="skip the in-process sidecar checks")
    a = ap.parse_args(argv)
    repo = a.repo_root.resolve()
    for var in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        if os.environ.pop(var, None) is not None:
            print(f"note: {var} was set in the caller's environment; it is removed and never passed to any child process")
    tmp: Optional[Path] = None
    start: Optional[dict] = None
    signers: list = []
    key_names: list = []
    results: dict = {}
    rc = 1
    try:
        # ------------------------------------------------------------------ preconditions
        if not (shutil.which("ssh-keygen", path=SAFE_PATH)):
            raise Refused("ssh-keygen not found")
        base = check_tmp_base(a.tmp_base or Path(tempfile.gettempdir()), repo)
        try:
            start = production_state(repo)
        except (OSError, ValueError) as e:
            raise Refused(f"the production files cannot be read under {repo}: {e.__class__.__name__}")
        viol = production_violations(start)
        print("PRODUCTION STATE at start (the harness never writes these):")
        print(f"  packaging/legal-allowed-signers sha256 {start['allowed_signers_sha256']} ({start['allowed_signers_active_lines']} signer lines)")
        print(f"  legal_signer_fingerprints = {start['fingerprints']}; decisions: {start['decisions_total']} total, "
              f"{start['decisions_open']} OPEN, {start['decisions_decided']} DECIDED")
        print(f"  private key files in the repository: {len(start['private_keys_in_repo'])}")
        if viol:
            raise Refused("production state is not consistent: " + "; ".join(viol))
        # ------------------------------------------------------------------ workspace and key
        tmp = Path(tempfile.mkdtemp(prefix="dev-signer-e2e-", dir=base))
        os.chmod(tmp, 0o700)
        tmp_real = tmp.resolve()
        if repo in tmp_real.parents or tmp_real == repo:
            raise Refused("the temporary workspace is inside the repository")
        (tmp / "home").mkdir(mode=0o700)
        print(f"\nTEMPORARY WORKSPACE (outside the repository, mode 0700): {tmp_real}")
        key1 = Signer(tmp / "signer", "dev-signer-key", KEY_COMMENT)
        key2 = Signer(tmp / "signer-other", "dev-other-key", KEY2_COMMENT)
        signers = [key1, key2]
        key_names = [str(key1.key), str(key2.key)]
        print(f"throwaway key 1 (allow-listed in the fixture only): {key1.fingerprint}, mode {oct(stat.S_IMODE(os.stat(key1.key).st_mode))}, "
              f"not in ~/.ssh, no agent, no passphrase")
        print(f"throwaway key 2 (NOT allow-listed anywhere):         {key2.fingerprint}, mode {oct(stat.S_IMODE(os.stat(key2.key).st_mode))}")
        # ------------------------------------------------------------------ real code
        gate_mod = _load(REPO / "packaging" / "check-distribution-gate.py", "_dev_gate")
        gen_notices = _load(REPO / "packaging" / "notices" / "generate_notices.py", "_dev_gen_notices")
        baseline_root = tmp / "baseline" / "repo"
        baseline_root.mkdir(parents=True)
        build_baseline(gate_mod.ABOUT_SLOTS, tmp, baseline_root, key1)
        (tmp / "notices-work").mkdir()
        notices_base = make_notices(tmp / "notices-work", gen_notices)
        print(f"fixture repository root with {len(json.loads((baseline_root / 'packaging/distribution-gate.json').read_text(encoding="utf-8"))['decisions'])} "
              "DEV FIXTURE decisions, synthetic signed records and synthetic texts built")
        rows: list = []
        todo = []
        for key, title, mutate, expected in scenarios(gate_mod.ABOUT_SLOTS, key2):
            sroot = tmp / "scen" / key
            sroot.parent.mkdir(exist_ok=True)
            shutil.copytree(baseline_root, sroot / "repo", symlinks=True)
            fx = Fixture(sroot / "repo", key1)
            mutate(fx)
            row = Row(key, title, expected)
            rows.append(row)
            todo.append((row, fx))
        py = sys.executable
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda t: evaluate(t[0], t[1], tmp, notices_base, py), todo))
        # ------------------------------------------------------------------ sidecar
        sidecar_out = {}
        if not a.no_sidecar:
            sys.path.insert(0, str(REPO))
            for row in rows:
                if row.expected.get("http") and row.licenses is not None:
                    try:
                        sidecar_out[row.key] = check_sidecar(row, gate_mod)
                    except ImportError as e:
                        row.problems.append(f"the sidecar could not be imported ({e.__class__.__name__}: {e})")
        # ------------------------------------------------------------------ report
        print("\nOUTCOMES (real gate / generator / checker code on the fixture root; AV approved-verified, UD unverified-draft, "
              "PD pending, NA not-applicable)")
        print(render_table(rows))
        print("\nDETAILS")
        for r in rows:
            print(f"- {r.key}: {r.title}")
            for d in r.detail:
                print("    " + d)
            for p in r.problems:
                print("    PROBLEM: " + p)
        if sidecar_out:
            print("\nSIDECAR (agentic_os from source, in-process, ephemeral 127.0.0.1 port; /about/ is public)")
            for k, v in sidecar_out.items():
                print(f"- scenario {k} (port {v['port']})")
                for c in v["checks"]:
                    print("    " + c)
        results = {"rows": [{"scenario": r.key, "title": r.title, "got": r.got, "states": r.states, "check_about_distribution": r.checkabout,
                             "detail": r.detail, "problems": r.problems, "ok": r.ok} for r in rows], "sidecar": sidecar_out}
        rc = 0 if all(r.ok for r in rows) else 1
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        rc = EXIT_REFUSED
    finally:
        # delete the throwaway keys FIRST, by name, then the whole workspace file by file (no rm -rf)
        deleted = []
        for s in signers:
            deleted += s.delete()
        n = remove_tree_by_name(tmp) if tmp is not None else 0
        if signers:
            gone = all(not os.path.exists(k) for k in key_names) and (tmp is None or not os.path.exists(tmp))
            print(f"\nCLEANUP: the throwaway keys ({', '.join(deleted)}) were deleted by name; {n} further file(s) in the workspace removed "
                  f"individually, workspace directory removed: {'yes' if gone else 'NO'}")
            if not gone:
                rc = 1
        elif tmp is not None:
            print(f"\nCLEANUP: workspace removed ({n} file(s)); no key was created")
    # ---------------------------------------------------------------------- end-of-run production assertions
    if start is not None:
        try:
            end = production_state(repo)
        except (OSError, ValueError) as e:
            print(f"PRODUCTION STATE at end: unreadable ({e.__class__.__name__})", file=sys.stderr)
            return 1
        changed = [k for k in end if end[k] != start[k]]
        end_viol = production_violations(end) if rc != EXIT_REFUSED else []
        if changed or end_viol:
            print("PRODUCTION STATE CHANGED OR NOT EMPTY AT END: " + ", ".join(changed + end_viol), file=sys.stderr)
            rc = 1 if rc != EXIT_REFUSED else rc
        elif rc != EXIT_REFUSED:
            print("PRODUCTION STATE at end: UNCHANGED (allowed-signers sha256 "
                  f"{end['allowed_signers_sha256'][:16]}..., fingerprints [], {end['decisions_open']} of {end['decisions_total']} decisions OPEN, "
                  "gate file and working tree status identical, no private key file in the repository)")
    if a.json and results:
        a.json.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("RESULT:", {0: "PASS", 1: "FAIL", EXIT_REFUSED: "REFUSED"}[rc])
    return rc


if __name__ == "__main__":
    sys.exit(main())
