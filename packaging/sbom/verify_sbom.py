#!/usr/bin/env python3
"""Cross-check an SBOM against the archive it claims to describe. Exit 1 on any FAIL.

The archive (PyInstaller executable or listing) is analysed independently of the SBOM (same engine as
generate_sbom.py: PKG entries + PYZ modules -> components, native banners, UI bundle, wheel metadata,
launcher crate graph). Then, for every component the archive implies:

  FAIL missing   the SBOM has no such component (an UNLISTED module/distribution/native library/JS lib/crate)
  FAIL version   the SBOM lists it with a different version than the archive/oracle shows (e.g. a stale lock)
  FAIL unattributed  an archive entry that nothing explains (no distribution, no stdlib)
  FAIL crate     a crate source path found in the launcher binary that the SBOM does not list
  WARN phantom   the SBOM lists a PyPI/crate component the archive does not contain (FAIL with --strict)

Works on SBOMs produced by generate_sbom.py and on foreign ones (matching is by purl, redevops:key,
then normalised name). Licences are not checked: they are declared values, never verified here.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sbom import rustsbom  # noqa: E402
from sbom.common_cli import add_input_args, run_analysis  # noqa: E402
from sbom.inventory import norm  # noqa: E402


def _eco(c: dict) -> str:
    """ecosystem of a component: from its purl, else from a purl-shaped bom-ref, else ''"""
    for v in (c.get("purl") or "", c.get("bom-ref") or ""):
        m = re.match(r"^pkg:([a-z]+)/", v)
        if m:
            return m.group(1)
    return ""


def _row(c: dict, key: str | None) -> dict:
    purl = (c.get("purl") or "").split("?")[0]
    if not purl and (c.get("bom-ref") or "").startswith("pkg:"):
        purl = c["bom-ref"].split("?")[0]
    return {"name": c.get("name", ""), "version": c.get("version"), "purl": purl, "eco": _eco(c), "key": key, "raw": c,
            "scope": c.get("scope", "required")}


def index_sbom(bom: dict) -> list[dict]:
    out = []
    for c in bom.get("components", []):
        props = {p["name"]: p["value"] for p in c.get("properties", [])}
        out.append(_row(c, props.get("redevops:key")))
        for n in c.get("components", []):  # nested components (foreign SBOMs nest)
            out.append(_row(n, None))
    return out


ALIASES = {"cpython": ("cpython", "python"), "pyinstaller-runtime": ("pyinstaller",), "sidecar": ("redevops-sidecar",),
           "ui-bundle": ("projects ui bundle",), "certifi-bundle": ("certifi ca bundle", "mozilla ca certificate bundle")}


ECO = {"pypi": "pypi", "npm": "npm", "cargo": "cargo", "embedded": "cargo"}


def find(e, idx: list[dict]) -> list[dict]:
    """Components of the SBOM that denote the archive component e (same ecosystem, then purl/name)."""
    hits = [c for c in idx if c["key"] == e.key]
    if hits:
        return hits
    eco = ECO.get(e.key.split(":", 1)[0], "")
    allowed = {"pypi": ("pypi", "github", "generic", ""), "npm": ("npm", ""), "cargo": ("cargo", "")}.get(eco, ("", "generic", "github"))
    pool = [c for c in idx if c["eco"] in allowed]
    if e.key.startswith("openssl:"):
        pool = [c for c in idx if norm(c["name"]) == "openssl"]
        return pool
    names = {norm(e.name)} | {norm(a) for a in ALIASES.get(e.key, ())}
    return [c for c in pool if norm(c["name"]) in names or any(norm(c["name"]).startswith(a) for a in ALIASES.get(e.key, ()))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_input_args(ap)
    ap.add_argument("--sbom", required=True)
    ap.add_argument("--strict", action="store_true", help="phantom components fail too")
    ap.add_argument("--report", help="write the findings as JSON")
    ap.add_argument("--expect-fail", action="store_true", help="exit 0 only if the verifier FAILS (documents known gaps of a foreign SBOM)")
    a = ap.parse_args(argv)
    bom = json.loads(Path(a.sbom).read_text(encoding="utf-8"))
    idx = index_sbom(bom)
    arc, inv, res, facts, manifest = run_analysis(a)
    findings: list[dict] = []

    def add(level: str, kind: str, msg: str, **kw) -> None:
        findings.append({"level": level, "kind": kind, "message": msg, **kw})

    matched_idx: set[int] = set()
    for key, e in sorted(res.comps.items()):
        if e.scope != "required" or e.kind == "platform":
            continue
        if key == "openssl:unknown":
            add("FAIL", "unattributed", "OpenSSL library files present but their version could not be read (supply --sidecar or --facts)")
            continue
        hits = find(e, idx)
        if not hits:
            add("FAIL", "missing", f"{e.name} {e.version or ''} ({e.key}) is in the archive but not in the SBOM", component=e.key,
                evidence=e.evidence[:2])
            continue
        same = [c for c in hits if not e.version or not c["version"] or c["version"] == e.version]
        for c in hits:
            matched_idx.add(id(c))
        if not same:
            have = sorted({c["version"] or "?" for c in hits})
            other = {x.version for x in res.comps.values() if x.name == e.name and x.key != e.key}
            if set(have) <= other:   # the SBOM lists the OTHER versions of a multi-version component (e.g. two OpenSSLs)
                add("FAIL", "missing", f"{e.name} {e.version} is in the archive but not in the SBOM (SBOM lists only {', '.join(have)})",
                    component=e.key, evidence=e.evidence[:1])
            else:
                add("FAIL", "version", f"{e.name}: SBOM says {', '.join(have)}, the archive/oracle shows {e.version}", component=e.key)
    for u in res.unattributed:
        add("FAIL", "unattributed", f"archive entry not explained by any distribution or the stdlib: {u}")
    for p in res.problems:
        add("FAIL", "inconsistent", p)

    # launcher crate graph: every crate cargo resolves for the target must be listed
    exp_crates: set[tuple[str, str]] = set()
    if a.cargo_manifest or a.cargo_metadata_json:
        meta = json.loads(Path(a.cargo_metadata_json).read_text(encoding="utf-8")) if a.cargo_metadata_json else rustsbom.cargo_metadata(a.cargo_manifest, a.cargo_target)
        listed_c = {(c["name"], c["version"]) for c in idx if c["eco"] == "cargo"}
        for cr in rustsbom.crates(meta, {}):
            exp_crates.add((cr["name"], cr["version"]))
            if cr["scope"] == "required" and (cr["name"], cr["version"]) not in listed_c:
                add("FAIL", "missing", f"crate {cr['name']} {cr['version']} is linked into the launcher (cargo metadata) but not in the SBOM", component=f"cargo:{cr['name']}@{cr['version']}")

    # launcher crate cross-check
    if a.launcher_bin and Path(a.launcher_bin).is_file():
        listed = {(c["name"], c["version"]) for c in idx if c["purl"].startswith("pkg:cargo/")}
        seen = rustsbom.crate_paths_in_binary(Path(a.launcher_bin).read_bytes())
        miss = sorted(seen - listed)
        for n, v in miss:
            add("FAIL", "crate", f"launcher binary names crate {n} {v} in a source path; the SBOM does not list it")
        add("INFO", "crate-check", f"{len(seen)} crate paths in the launcher binary; {len(seen) - len(miss)} listed in the SBOM")
    elif a.cargo_manifest or a.cargo_metadata_json:
        add("INFO", "crate-check", "no --launcher-bin: crate cross-check against the binary skipped")

    for c in idx:
        if id(c) in matched_idx:
            continue
        if c["purl"].startswith(("pkg:pypi/", "pkg:cargo/")) and c["scope"] == "required":
            if c["purl"].startswith("pkg:cargo/") and not (a.cargo_manifest or a.cargo_metadata_json):
                continue
            if c["purl"].startswith("pkg:cargo/") and (c["name"], c["version"]) in exp_crates:
                continue
            expected_keys = {e.key for e in res.comps.values()}
            if c["key"] in expected_keys:
                continue
            nm = c["name"]
            if any(e.key.startswith(("cargo:", "embedded:")) and norm(e.name) == norm(nm) for e in res.comps.values()):
                continue
            add("FAIL" if a.strict else "WARN", "phantom", f"SBOM lists {nm} {c['version']} but the archive does not contain it")

    fails = [f for f in findings if f["level"] == "FAIL"]
    warns = [f for f in findings if f["level"] == "WARN"]
    grouped: dict[str, list[dict]] = {}
    for f in findings:
        k = f.get("component", "")
        g = k.split(":")[1] if k.startswith("embedded:") else None
        if g:
            grouped.setdefault(g, []).append(f)
            continue
        print(f"{f['level']:5s} {f['kind']:13s} {f['message']}")
    for g, fs in sorted(grouped.items()):
        eg = ", ".join(x["message"].split(" is in")[0] for x in fs[:3])
        print(f"FAIL  missing       {len(fs)} Rust crates statically linked into {g}'s native extension are not in the SBOM (declared by the wheel's own embedded SBOM), e.g. {eg}")
    print(f"-- archive: {arc.source} ({len(arc.pkg)} PKG entries, {len(arc.pyz)} PYZ modules); SBOM: {Path(a.sbom).name} ({len(idx)} components)")
    print(f"-- result: {len(fails)} FAIL, {len(warns)} WARN")
    if a.report:
        Path(a.report).write_text(json.dumps({"sbom": a.sbom, "archive": arc.source, "fails": len(fails), "warns": len(warns), "findings": findings}, indent=1) + "\n", encoding="utf-8")
    if a.expect_fail:
        return 0 if fails else 1
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
