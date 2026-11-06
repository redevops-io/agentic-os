#!/usr/bin/env python3
"""Generate the About record `about.json` (schema `redevops.about/1`) that ships in the Licenses folder.

  python3 -I packaging/notices/generate_about.py --notices-dir packaging/notices/out --platform macos-arm64

build-licenses.sh runs this after generate_notices.py and before stage_licenses.py (which copies about.json into the
Licenses folder and lists its sha256 in LICENSES-MANIFEST.json). Inputs, all existing build products: packaging/identity.json
(product identity), the notices directory (THIRD_PARTY_NOTICES.txt, NOTICES_MISSING.md, notices-report.json), the four product
licence files at the repository root, the vendored Projects UI bundle, git (commit facts) and packaging/distribution-gate.json
(legal slots). Nothing is typed in by hand and no field is copied as a block: every field is named here (allowlist), so no
path, host name, user name, wall-clock time or hash of the artifact that contains the record can get in.

LEGAL SLOTS. The record carries five slots (LEGAL.COPYRIGHT_LINE, LEGAL.LICENCE_STATEMENT, LEGAL.SOURCE_OFFER,
LEGAL.NO_WARRANTY, LEGAL.SECTION13_NOTICE). A slot is "approved-verified" only when every decision it lists is DECIDED in the gate file, the
primary decision's `slot_texts` entry names an approved text file whose sha256 matches, AND the signatures and the signed record
headers verify against the signer list (rules in check-distribution-gate.py). While a decision is OPEN the slot is the neutral
placeholder ("pending") and its text file is never opened; a DECIDED slot whose signatures do not verify is an "unverified-draft"
that carries its own label inside the text and is never shown as ordinary legal text. This script writes no legal text.

A hash match between a DECIDED entry and its files proves consistency only, not who decided (see check-distribution-gate.py). A
DISTRIBUTION build therefore also needs a valid detached signature (packaging/legal-allowed-signers) on every decision record and
approved text it relies on; a private build prints a warning when one does not verify.

Exit codes: 0 = record written; 2 = inputs or gate file malformed, or a DECIDED slot whose approved text is missing or does not
match (an error in EVERY build mode); 3 = DISTRIBUTION build (REDEVOPS_DISTRIBUTION_BUILD anything other than empty or exactly 0,
or --distribution) with a slot that is still a placeholder or a record without a valid signature: nothing is written and any older about.json in the output directory is
removed. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

PRODUCT_FILES = (  # (id, file, label, role): labels name the file; they say nothing about its content
    ("licence", "LICENSE", "Licence file", "licence-file"),
    ("commons-clause", "LICENSE-COMMONS-CLAUSE", "Commons Clause file", "addendum-file"),
    ("licence-statement", "LICENSE.md", "Licence statement file", "statement-file"),
    ("licensing", "LICENSING.md", "Licensing overview file", "overview-file"),
)
GAPS_FILE = ("notices-missing", "NOTICES_MISSING.md", "Components without a licence text in this build", "gaps-file")
NOTICES_FILE = "THIRD_PARTY_NOTICES.txt"
SEP = b"=" * 72 + b"\n"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_record_module(root: Path = ROOT):
    return _load(root / "agentic_os" / "about_record.py", "_about_record")


def load_gate(root: Path = ROOT):
    return _load(root / "packaging" / "check-distribution-gate.py", "_about_gate")


def ascii_safe(s: object, limit: int = 300) -> str:
    """Printable ASCII only (anything else is backslash-escaped, control characters become spaces), bounded."""
    t = str(s).encode("ascii", "backslashreplace").decode("ascii")
    t = "".join(c if 32 <= ord(c) < 127 else " " for c in t)
    return t[:limit]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Refused(Exception):
    def __init__(self, code: int, msgs: list[str]):
        super().__init__("; ".join(msgs))
        self.code, self.msgs = code, msgs


# ----------------------------------------------------------------------------------------------------- notices offsets
def section_offsets(raw: bytes, records: list[dict], refs: list[dict]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Byte (offset, length) of every component section and every reference-notice section in THIRD_PARTY_NOTICES.txt.

    A section starts at the line of 72 '=' that precedes its heading `[NNN] name version` (components) or `[Rnn] title`
    (reference notices) and ends where the next section starts (the last component ends where the REFERENCE NOTICES block starts,
    the last reference notice at the end of the file). Raises ValueError when the file does not have the expected shape."""
    heads: list[bytes] = [f"[{i:03d}] {r['name']} {r['version']}\n".encode("utf-8") for i, r in enumerate(records, 1)]
    starts: list[int] = []
    pos = 0
    for i, h in enumerate(heads, 1):
        at = raw.find(SEP + h, pos)
        if at < 0:
            raise ValueError(f"section heading for component {i} ({records[i - 1]['name']}) not found in {NOTICES_FILE}")
        starts.append(at)
        pos = at + len(SEP) + len(h)
    ref_marker = raw.find(SEP + b"REFERENCE NOTICES\n", pos)
    if refs and ref_marker < 0:
        raise ValueError("REFERENCE NOTICES block not found")
    comp_end = ref_marker if ref_marker >= 0 else len(raw)
    ref_starts: list[int] = []
    pos = ref_marker if ref_marker >= 0 else len(raw)
    for i, r in enumerate(refs, 1):
        h = f"[R{i:02d}] {r['title']}\n".encode("utf-8")
        at = raw.find(SEP + h, pos)
        if at < 0:
            raise ValueError(f"section heading for reference notice {i} ({r['id']}) not found in {NOTICES_FILE}")
        ref_starts.append(at)
        pos = at + len(SEP) + len(h)
    comps = [(s, (starts[k + 1] if k + 1 < len(starts) else comp_end) - s) for k, s in enumerate(starts)]
    rf = [(s, (ref_starts[k + 1] if k + 1 < len(ref_starts) else len(raw)) - s) for k, s in enumerate(ref_starts)]
    return comps, rf


# ----------------------------------------------------------------------------------------------------- source facts
def git_facts(root: Path) -> dict:
    def g(*a: str) -> str:
        r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if r.returncode != 0:
            raise ValueError(f"git {' '.join(a)} failed: {r.stderr.strip()[:200]}")
        return r.stdout.strip()
    from datetime import datetime, timezone
    status = g("status", "--porcelain")
    t = int(g("show", "-s", "--format=%ct", "HEAD"))
    return {"commit": g("rev-parse", "HEAD"), "tree": g("rev-parse", "HEAD^{tree}"),
            "dirty_files": len(status.splitlines()) if status else 0,
            "commit_time_utc": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "describe": g("describe", "--tags", "--always")}


def ui_bundle(ui_dir: Path | None) -> dict:
    files = []
    ident = "none"
    if ui_dir is not None and ui_dir.is_dir():
        for f in sorted(p for p in ui_dir.rglob("*") if p.is_file()):
            rel = f.relative_to(ui_dir).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            files.append({"path": rel, "bytes": f.stat().st_size, "sha256": sha256_file(f)})
            if ident == "none" and re.fullmatch(r"assets/index-[A-Za-z0-9_-]+\.js", rel):
                ident = Path(rel).name
    return {"id": ident, "files": files}


def pyproject_version(root: Path) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    if not m:
        raise ValueError("pyproject.toml has no version")
    return m.group(1)


# ----------------------------------------------------------------------------------------------------- the record
def build_record(*, notices_dir: Path, root: Path = ROOT, gate_path: Path | None = None, identity_path: Path | None = None,
                 platform: str, distribution: bool, ui_dir: Path | None = None, source: dict | None = None) -> dict:
    """Return the record, or raise Refused(code, messages). `source` overrides the git facts (tests)."""
    gate = load_gate(root)
    gate_path = gate_path or (root / "packaging" / "distribution-gate.json")
    try:
        gdoc = gate.load_doc(gate_path)
    except SystemExit:
        raise Refused(2, ["gate file unreadable or malformed (see the message above)"])
    decisions, signers = gdoc["decisions"], gdoc.get("legal_signer_fingerprints", [])
    allowed = root / "packaging" / "legal-allowed-signers"
    errs = gate.validate(decisions, root) + gate.separation_problems(allowed)
    if errs:
        raise Refused(2, [f"gate file problem: {e}" for e in errs])
    slots, slot_errs, refusals = gate.resolve_about_slots(decisions, root, distribution, allowed_signers=allowed, signers=signers)
    if slot_errs:
        raise Refused(2, [f"about slot problem: {e}" for e in slot_errs])
    if distribution and refusals:
        raise Refused(3, ["DISTRIBUTION BUILD REFUSED: the About page has legal slots without an approved text"] + refusals)
    platform_gate = platform.split("-")[0] if platform.split("-")[0] in ("macos", "windows", "linux") else "all"
    sig = gate.signature_problems(decisions, root, allowed, platform_gate, signers)
    if distribution and sig:
        raise Refused(3, ["DISTRIBUTION BUILD REFUSED: a legal record or approved text has no valid detached signature "
                          "(a hash match alone proves consistency, not who decided)"] + sig)
    for w in sig:
        print("ABOUT: WARNING:", w, "(a distribution build would be refused)", file=sys.stderr)

    try:
        ident = json.loads((identity_path or root / "packaging" / "identity.json").read_text(encoding="utf-8"))
        line = ident["r0prime"]
        notices_raw = (notices_dir / NOTICES_FILE).read_bytes()
        report = json.loads((notices_dir / "notices-report.json").read_text(encoding="utf-8"))
        records, refs = report["records"], report.get("reference_notices", [])
        offsets, ref_offsets = section_offsets(notices_raw, records, refs)
        src = source or git_facts(root)
        pyver = pyproject_version(root)
        licence_files = []
        for sid, name, label, role in PRODUCT_FILES:
            p = root / name
            licence_files.append({"id": sid, "file": name, "label": label, "bytes": p.stat().st_size,
                                  "sha256": sha256_file(p), "role": role})
        gid, gname, glabel, grole = GAPS_FILE
        gp = notices_dir / gname
        licence_files.append({"id": gid, "file": gname, "label": glabel, "bytes": gp.stat().st_size,
                              "sha256": sha256_file(gp), "role": grole})
        gate_hash = sha256_file(gate_path)
    except (OSError, KeyError, ValueError, TypeError) as e:
        raise Refused(2, [f"input problem: {e.__class__.__name__}: {str(e)[:200]}"])

    components = []
    for i, (r, (off, ln)) in enumerate(zip(records, offsets), 1):
        components.append({
            "n": i, "name": ascii_safe(r["name"], 120), "version": ascii_safe(r["version"], 80),
            "declared": ascii_safe(r["declared"]), "type": ascii_safe(r["type"], 32).lower(),
            "status": r["status"], "offset": off, "length": ln,
            "files": [{"label": ascii_safe(f["label"], 200), "bytes": f["bytes"], "sha256": f["sha256"]} for f in r["files"]]})
    reference = [{"id": ascii_safe(r["id"], 40).lower(), "title": ascii_safe(r["title"], 200), "declared": ascii_safe(r.get("declared", "")),
                  "status": r["status"], "offset": off, "length": ln} for r, (off, ln) in zip(refs, ref_offsets)]
    missing = [{"name": ascii_safe(r["name"], 120), "version": ascii_safe(r["version"], 80)}
               for r in records if r["status"] == "text-not-found"]
    doc = {
        "schema": "redevops.about/1",
        "product": {
            "name": ascii_safe(line["product_name"], 80), "version": line["version"], "release_line": ident["release_line"],
            "bundle_identifier": line["bundle_identifier"],
            "launcher": {"binary": line["main_binary_name"], "version": line["bundle_version"]},
            "sidecar": {"binary": line["sidecar_binary_name"], "python_package": "agentic-os", "python_package_version": pyver}},
        "build": {
            "mode": "distribution" if distribution else "private-candidate", "platform": platform,
            "source": {k: src[k] for k in ("commit", "tree", "dirty_files", "commit_time_utc", "describe")},
            "ui_bundle": ui_bundle(ui_dir if ui_dir is not None else root / "agentic_os" / "projects_ui"),
            "inputs": {"sbom_sha256": report["sbom_sha256"], "gate_file_sha256": gate_hash}},
        "licence_files": licence_files,
        "notices": {"file": NOTICES_FILE, "bytes": len(notices_raw), "sha256": hashlib.sha256(notices_raw).hexdigest(),
                    "component_count": len(components), "components": components, "reference_notices": reference, "missing": missing},
        "legal": {"slots": slots},
        "source_access": {"state": "offer-recorded" if slots["LEGAL.SOURCE_OFFER"]["state"] == "approved-verified" else "not-published"},
        "ui": {"entry": True, "locales": ["en"]},
    }
    rec = load_record_module(root)
    problems = rec.validate_record(doc)
    if problems:
        raise Refused(2, [f"generated record is invalid: {p}" for p in problems])
    if len(rec.canonical_bytes(doc)) > rec.MAX_BYTES_WRITE:
        raise Refused(2, [f"record larger than {rec.MAX_BYTES_WRITE} bytes"])
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--notices-dir", required=True, type=Path, help="output directory of generate_notices.py")
    ap.add_argument("--out", type=Path, help="directory to write about.json into (default: --notices-dir)")
    ap.add_argument("--platform", required=True, help="e.g. macos-arm64, windows-x64")
    ap.add_argument("--distribution", action="store_true", help="treat this as a distribution build")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (tests only)")
    ap.add_argument("--gate", type=Path, help="gate file (tests only; default packaging/distribution-gate.json)")
    ap.add_argument("--ui-dir", type=Path, help="Projects UI bundle directory (default agentic_os/projects_ui)")
    a = ap.parse_args(argv)
    out = (a.out or a.notices_dir) / "about.json"
    gate = load_gate(a.root)
    dist = gate.is_distribution(a.distribution)
    try:
        doc = build_record(notices_dir=a.notices_dir, root=a.root, gate_path=a.gate, platform=a.platform,
                           distribution=dist, ui_dir=a.ui_dir)
    except Refused as r:
        try:
            out.unlink()
        except FileNotFoundError:
            pass
        for m in r.msgs:
            print("ABOUT:", m, file=sys.stderr)
        return r.code
    rec = load_record_module(a.root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(rec.canonical_bytes(doc))
    n = sum(1 for s in doc["legal"]["slots"].values() if s["state"] == "pending")
    print(f"about.json: {len(doc['notices']['components'])} components, {n} of {len(doc['legal']['slots'])} legal slots are "
          f"placeholders, mode {doc['build']['mode']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
