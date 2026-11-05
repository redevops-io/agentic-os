#!/usr/bin/env python3
"""Check an About record (`about.json`) against the Licenses folder and the repository.

  python3 -I packaging/notices/check_about.py --licenses-dir deploy/launcher/src-tauri/licenses \
        [--notices-report packaging/notices/out/notices-report.json] [--gate FILE] [--distribution] [--copy PATH]...

build-all.sh runs this beside check_notices_vs_sbom.py. It fails (exit 1) when
  * the record is not valid `redevops.about/1` (exact field allowlist, character classes, sizes, offsets, slot states),
  * LICENSES-MANIFEST.json does not list about.json with its sha256, or a licence file / THIRD_PARTY_NOTICES.txt differs from
    the record or the manifest,
  * product name, versions or bundle identifier disagree with packaging/identity.json, tauri.conf.json, Cargo.toml,
    pyproject.toml or agentic_os.__version__,
  * a component section offset does not point at the `[NNN] name version` heading of THIRD_PARTY_NOTICES.txt, or the component
    list differs from notices-report.json (when given),
  * a legal slot differs from what the gate file says now (a slot is filled only for DECIDED decisions with a matching approved
    text; see check-distribution-gate.py), or, with --distribution (or REDEVOPS_DISTRIBUTION_BUILD set to anything but empty/0),
    the record is not a distribution record or carries a placeholder,
  * a product string contains a forbidden phrase (open source, cleared, compliant, approved by counsel, legally),
  * a --copy (the copy inside the .app, the copy extracted from the sidecar archive) is not byte-identical to the staged record.
It does not judge any licence. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check(licenses_dir: Path, *, root: Path = ROOT, notices_report: Path | None = None, gate_path: Path | None = None,
          distribution: bool = False, copies: tuple = (), code_root: Path | None = None, allowed_signers: Path | None = None,
          expect_commit: str | None = None) -> list[str]:
    """`root` holds the DATA (identity, versions, gate file, product licence files); `code_root` (default: root) holds the CODE that is
    executed (agentic_os/about_record.py and packaging/check-distribution-gate.py; generate_about.py is loaded from this script's own
    directory). The signing scripts run a hash-verified private copy as code_root."""
    code_root = code_root or root
    rec = _load(code_root / "agentic_os" / "about_record.py", "_about_record")
    gen = _load(HERE / "generate_about.py", "_generate_about")
    gate = gen.load_gate(code_root)
    allowed = allowed_signers or (root / "packaging" / "legal-allowed-signers")
    errs: list[str] = []
    ap = licenses_dir / "about.json"
    if not ap.is_file():
        return [f"{ap} missing"]
    raw = ap.read_bytes()
    doc, problems = rec.parse_record(raw)
    if doc is None:
        return [f"about.json: {p}" for p in problems]
    if raw != rec.canonical_bytes(doc):
        errs.append("about.json is not in canonical form (edited by hand?)")

    # --- the folder: manifest, files
    man = licenses_dir / "LICENSES-MANIFEST.json"
    manifest = {}
    if man.is_file():
        manifest = json.loads(man.read_text(encoding="utf-8")).get("files", {})
    else:
        errs.append("LICENSES-MANIFEST.json missing")
    if manifest.get("about.json", {}).get("sha256") != hashlib.sha256(raw).hexdigest():
        errs.append("LICENSES-MANIFEST.json does not list about.json with its sha256")
    listed = [(f["file"], f["bytes"], f["sha256"]) for f in doc["licence_files"]] + \
             [(doc["notices"]["file"], doc["notices"]["bytes"], doc["notices"]["sha256"])]
    for name, size, sha in listed:
        p = licenses_dir / name
        if not p.is_file():
            errs.append(f"{name} named in about.json is absent from the Licenses folder")
            continue
        b = p.read_bytes()
        if len(b) != size or hashlib.sha256(b).hexdigest() != sha:
            errs.append(f"{name} differs from about.json")
        if manifest.get(name, {}).get("sha256") != sha:
            errs.append(f"{name}: about.json and LICENSES-MANIFEST.json disagree")

    # --- identity and versions
    ident = json.loads((root / "packaging" / "identity.json").read_text(encoding="utf-8"))
    line = ident["r0prime"]
    p = doc["product"]
    expect = {"name": line["product_name"], "version": line["version"], "release_line": ident["release_line"],
              "bundle_identifier": line["bundle_identifier"]}
    for k, v in expect.items():
        if p[k] != v:
            errs.append(f"product.{k} {p[k]!r} differs from packaging/identity.json {v!r}")
    if (p["launcher"]["binary"], p["launcher"]["version"]) != (line["main_binary_name"], line["bundle_version"]):
        errs.append("product.launcher differs from packaging/identity.json")
    if p["sidecar"]["binary"] != line["sidecar_binary_name"]:
        errs.append("product.sidecar.binary differs from packaging/identity.json")
    tauri = json.loads((root / "deploy/launcher/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = re.search(r'^version\s*=\s*"([^"]+)"', (root / "deploy/launcher/src-tauri/Cargo.toml").read_text(encoding="utf-8"), re.M)
    if (tauri.get("productName"), tauri.get("version"), tauri.get("identifier")) != (p["name"], p["version"], p["bundle_identifier"]):
        errs.append("product name/version/identifier differ from tauri.conf.json")
    if not cargo or cargo.group(1) != p["launcher"]["version"]:
        errs.append("product.launcher.version differs from the launcher Cargo.toml")
    pyver = gen.pyproject_version(root)
    init = re.search(r'^__version__\s*=\s*"([^"]+)"', (root / "agentic_os/__init__.py").read_text(encoding="utf-8"), re.M)
    if p["sidecar"]["python_package_version"] != pyver or not init or init.group(1) != pyver:
        errs.append("product.sidecar.python_package_version differs from pyproject.toml / agentic_os.__version__")

    # --- notices offsets and component list
    nraw = (licenses_dir / doc["notices"]["file"]).read_bytes() if (licenses_dir / doc["notices"]["file"]).is_file() else b""
    sep = gen.SEP
    for c in doc["notices"]["components"]:
        head = f"[{c['n']:03d}] {c['name']} {c['version']}\n".encode("ascii", "backslashreplace")
        if not nraw[c["offset"]:].startswith(sep + head):
            # names are written with non-ASCII escaped in the record: compare on the ASCII form of the file text
            seg = nraw[c["offset"]:c["offset"] + len(sep) + len(head) + 64].decode("utf-8", "replace")
            want = (sep + head).decode("ascii")
            if seg.encode("ascii", "backslashreplace").decode("ascii")[:len(want)] != want:
                errs.append(f"component {c['n']} ({c['name']}): offset {c['offset']} is not its section heading")
    if notices_report is not None:
        report = json.loads(notices_report.read_text(encoding="utf-8"))
        want = [(r["type"].lower(), r["name"], r["version"], r["status"]) for r in report["records"]]
        have = [(c["type"], c["name"], c["version"], c["status"]) for c in doc["notices"]["components"]]
        norm = lambda t: [(a, gen.ascii_safe(b, 120), gen.ascii_safe(c, 80), d) for a, b, c, d in t]  # noqa: E731
        if norm(want) != have:
            errs.append("about.json component list differs from notices-report.json")
        if report["sbom_sha256"] != doc["build"]["inputs"]["sbom_sha256"]:
            errs.append("about.json was generated from another SBOM than notices-report.json names")
        if f"sha256 {report['sbom_sha256']}" not in nraw.decode("utf-8", "replace")[:2000]:
            errs.append("THIRD_PARTY_NOTICES.txt was not generated from the SBOM named in about.json")

    # --- legal slots against the gate file as it is now
    gate_path = gate_path or (root / "packaging" / "distribution-gate.json")
    try:
        gdoc = gate.load_doc(gate_path)
    except SystemExit:
        return errs + ["gate file unreadable or malformed"]
    decisions, signers = gdoc["decisions"], gdoc.get("legal_signer_fingerprints", [])
    if expect_commit is not None and doc["build"]["source"]["commit"] != expect_commit:
        errs.append(f"the record's source.commit {doc['build']['source']['commit']} is not the commit of the build that was authorized "
                    f"({expect_commit})")
    if doc["build"]["inputs"]["gate_file_sha256"] != hashlib.sha256(gate_path.read_bytes()).hexdigest():
        errs.append("the gate file changed after about.json was generated")
    slots, slot_errs, refusals = gate.resolve_about_slots(decisions, root, distribution, allowed_signers=allowed, signers=signers)
    errs += [f"slot: {e}" for e in slot_errs]
    if slots != doc["legal"]["slots"]:
        errs.append("legal.slots differ from what the gate file and the approved texts give now")
    if distribution:
        if doc["build"]["mode"] != "distribution":
            errs.append("distribution build but the record is a private-candidate record")
        errs += [f"distribution: {r}" for r in refusals]
        pg = doc["build"]["platform"].split("-")[0]
        errs += [f"distribution: {r}" for r in gate.signature_problems(
            decisions, root, allowed, pg if pg in ("macos", "windows", "linux") else "all", signers)]

    # --- wording
    for s in rec.record_strings(doc):
        bad = rec.forbidden_in(s)
        if bad:
            errs.append(f"forbidden phrase {bad} in an About string: {s[:80]!r}")

    # --- other copies
    for c in copies:
        if not Path(c).is_file():
            errs.append(f"copy {c} not found")
        elif Path(c).read_bytes() != raw:
            errs.append(f"copy {c} is not byte-identical to the staged about.json")
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--licenses-dir", required=True, type=Path)
    ap.add_argument("--notices-report", type=Path)
    ap.add_argument("--gate", type=Path)
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (tests only)")
    ap.add_argument("--code-root", type=Path, help="directory holding the code to execute (agentic_os/about_record.py, packaging/"
                    "check-distribution-gate.py); default --root. The signing scripts pass a hash-verified private copy")
    ap.add_argument("--allowed-signers", type=Path, help="allowed-signers file (default: <root>/packaging/legal-allowed-signers)")
    ap.add_argument("--expect-commit", help="the record's build.source.commit must equal this 40-hex commit (the authorized build)")
    ap.add_argument("--distribution", action="store_true")
    ap.add_argument("--copy", action="append", default=[], type=Path, help="another copy that must be byte-identical (repeatable)")
    a = ap.parse_args(argv)
    gen = _load(HERE / "generate_about.py", "_generate_about")
    dist = gen.load_gate(a.code_root or a.root).is_distribution(a.distribution)
    errs = check(a.licenses_dir, root=a.root, notices_report=a.notices_report, gate_path=a.gate, distribution=dist,
                 copies=tuple(a.copy), code_root=a.code_root, allowed_signers=a.allowed_signers, expect_commit=a.expect_commit)
    for e in errs:
        print("FAIL:", e, file=sys.stderr)
    print("about.json is consistent" if not errs else f"{len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
