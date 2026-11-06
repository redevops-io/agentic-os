#!/usr/bin/env python3
"""Assemble THIRD_PARTY_NOTICES.txt for the Agentic Apps artifacts.

Input contract (the interface shared with the SBOM generator under packaging/):
  --sbom FILE   a CycloneDX JSON document (1.4 / 1.5 / 1.6). Only these fields are read:
                components[].{name, version, purl, licenses[], properties[], components[]}
                (nested components are read too). The purl type selects the lookup:
                  pkg:pypi/...   -> *.dist-info licence files under --site-packages
                  pkg:cargo/...  -> licence files of <name>-<version>/ (or <name>/) under --crates-dir
                  pkg:generic/.. or anything else -> the entry in components.json (texts/ directory)

Nothing is silently skipped: every component ends up either with at least one licence text, or in
NOTICES_MISSING.json / NOTICES_MISSING.md with the reason and the decision id that tracks it.
The output states what was collected. It does not say that any component is cleared or compliant.

Outputs in --out:
  THIRD_PARTY_NOTICES.txt   index + licence/notice texts (identical texts are printed once)
  NOTICES_MISSING.json      machine-readable list of components without a licence text
  NOTICES_MISSING.md        the same, for people
  notices-report.json       per component: status, files, sha256 of each text

Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LICENCE_FILE_RE = re.compile(r"^(licen[cs]e|licen[cs]es|copying|unlicense|copyright)([-_. ].*)?$", re.I)
NOTICE_FILE_RE = re.compile(r"^notice([-_. ].*)?$", re.I)
GENERATOR_VERSION = "1"
# a component that has no entry of its own and no licence text is still a decision item
DEFAULT_DECISION = "D-LIC-13"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------- SBOM

def purl_type(purl: str | None) -> str:
    if purl and purl.startswith("pkg:"):
        return purl[4:].split("/", 1)[0].split("@", 1)[0].lower()
    return "generic"


def declared_licence(comp: dict) -> str:
    out = []
    for item in comp.get("licenses") or []:
        if "expression" in item:
            out.append(item["expression"])
        else:
            lic = item.get("license") or {}
            out.append(lic.get("id") or lic.get("name") or "")
    return " AND ".join(x for x in out if x)


def iter_components(doc: dict):
    stack = list(doc.get("components") or [])
    while stack:
        c = stack.pop(0)
        yield c
        stack[0:0] = c.get("components") or []


def load_components(sbom: Path) -> list[dict]:
    doc = json.loads(sbom.read_text(encoding="utf-8"))
    if doc.get("bomFormat") not in (None, "CycloneDX"):
        raise SystemExit(f"{sbom}: not a CycloneDX document")
    comps = []
    seen = set()
    for c in iter_components(doc):
        name, version = c.get("name"), c.get("version", "")
        if not name:
            continue
        ptype = purl_type(c.get("purl"))
        key = (ptype, norm(name), version)
        if key in seen:
            continue
        seen.add(key)
        props = {p.get("name"): p.get("value") for p in c.get("properties") or []}
        comps.append({
            "type": ptype, "name": name, "version": version, "purl": c.get("purl", ""),
            "declared": declared_licence(c), "props": props,
        })
    comps.sort(key=lambda x: (x["type"], norm(x["name"]), x["version"]))
    return comps


# ------------------------------------------------------------------ file discovery

def licence_files_in(directory: Path, recurse_dirs=("licenses", "LICENSES", "license", "licences")) -> list[Path]:
    found: list[Path] = []
    if not directory.is_dir():
        return found
    for p in sorted(directory.iterdir()):
        if p.is_file() and (LICENCE_FILE_RE.match(p.name) or NOTICE_FILE_RE.match(p.name)):
            found.append(p)
        elif p.is_dir() and p.name in recurse_dirs:
            for q in sorted(p.rglob("*")):
                if q.is_file():
                    found.append(q)
    return found


def kind_of(path: Path) -> str:
    return "notice" if NOTICE_FILE_RE.match(path.name) else "licence"


def find_dist_info(site_dirs: list[Path], name: str, version: str) -> list[Path]:
    want = norm(name)
    hits = []
    for sp in site_dirs:
        if not sp.is_dir():
            continue
        for d in sp.glob("*.dist-info"):
            stem = d.name[: -len(".dist-info")]
            n, _, v = stem.rpartition("-")
            if norm(n) == want and (not version or v == version):
                hits.append(d)
    return hits


def metadata_licence_hint(dist_info: Path) -> str:
    meta = dist_info / "METADATA"
    if not meta.is_file():
        return ""
    hints = []
    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            break
        if line.startswith(("License-Expression:", "License:")) and len(line) < 200:
            hints.append(line.strip())
        elif line.startswith("Classifier: License ::"):
            hints.append(line.strip())
    return "; ".join(hints)


def find_crate_dir(crate_dirs: list[Path], name: str, version: str) -> Path | None:
    for base in crate_dirs:
        for cand in (base / f"{name}-{version}", base / name):
            if cand.is_dir():
                return cand
    return None


# ------------------------------------------------------------------------ assemble

def resolve(comp: dict, entries: dict, site_dirs, crate_dirs, texts_dir: Path) -> dict:
    """Return the notice record for one component."""
    key_exact = f"{comp['type']}:{norm(comp['name'])}@{comp['version']}"
    key = f"{comp['type']}:{norm(comp['name'])}"
    rkey = comp["props"].get("redevops:key", "")         # the SBOM generator's stable key, e.g. npm:react, certifi-bundle
    entry = entries.get(key_exact) or entries.get(key) or entries.get(f"key:{rkey}") or (entries.get(rkey) if ":" in rkey else None) or {}
    from_pypi_dist = comp["type"] == "pypi" or rkey.startswith("pypi:")   # first-party wheels carry a pkg:github purl
    files: list[tuple[str, Path, str]] = []   # (label, path, kind: licence|notice)
    hint = ""
    reasons = []

    for t in entry.get("texts", []):
        p = texts_dir / t
        if p.is_file():
            files.append((f"texts/{t}", p, "licence"))
        else:
            reasons.append(f"text file texts/{t} named in components.json is missing")

    if from_pypi_dist and (entry.get("texts_from_wheel", True)):
        dists = find_dist_info(site_dirs, comp["name"], comp["version"])
        if not dists:
            if not entry.get("texts"):
                reasons.append("no *.dist-info for this name/version in the build environment")
        for d in dists[:1]:
            hint = metadata_licence_hint(d)
            fs = licence_files_in(d)
            files += [(f"{d.name}/{f.relative_to(d).as_posix()}", f, kind_of(f)) for f in fs]
            if not fs and not entry.get("texts"):
                reasons.append("dist-info holds no LICENSE/COPYING/NOTICE file")
    elif comp["type"] == "cargo":
        d = find_crate_dir(crate_dirs, comp["name"], comp["version"])
        if d is None:
            reasons.append("crate source directory not found under --crates-dir")
        else:
            fs = licence_files_in(d)
            for extra in entry.get("package_files", []):     # licence statements in a non-standard file of the package
                ep = d / extra
                if ep.is_file() and ep not in fs:
                    fs.append(ep)
            files += [(f"{d.name}/{f.relative_to(d).as_posix()}", f, kind_of(f)) for f in fs]
            if not fs:
                reasons.append("crate package holds no LICENSE/COPYING/NOTICE file")
            up = d / "UPSTREAM-SOURCE.txt"
            if fs and up.is_file():                  # texts fetched from the upstream repository, not from the package
                entry = dict(entry, unverified=(entry.get("unverified", "") + " " + up.read_text(encoding="utf-8").strip().replace("\n", " ")).strip())
    elif not from_pypi_dist and comp["type"] != "cargo" and not entry:
        reasons.append("no entry with licence texts in components.json")

    if entry.get("not_third_party"):
        # first-party / system / not-redistributed item: listed for completeness, not a missing third-party text
        return {
            "type": comp["type"], "name": comp["name"], "version": comp["version"], "purl": comp["purl"],
            "declared": entry.get("declared") or comp["declared"] or "(none recorded in the SBOM)",
            "sbom_declared": comp["declared"], "metadata_hint": "", "note": entry.get("note", ""),
            "decision": entry.get("decision") or "", "unverified": entry.get("unverified", ""),
            "status": "not-third-party", "reasons": [entry["not_third_party"]], "files": [],
        }
    has_licence_text = any(k == "licence" for _, _, k in files)
    only_notice = bool(files) and not has_licence_text
    status = "text-found" if has_licence_text else "text-not-found"
    if entry.get("missing_reason"):
        reasons.append(entry["missing_reason"])
        if not has_licence_text:
            status = "text-not-found"
    if only_notice:
        reasons.append("only NOTICE-type files found, no licence text")
    return {
        "type": comp["type"], "name": comp["name"], "version": comp["version"], "purl": comp["purl"],
        "declared": entry.get("declared") or comp["declared"] or "(none recorded in the SBOM)",
        "sbom_declared": comp["declared"], "metadata_hint": hint,
        "note": entry.get("note", ""), "decision": entry.get("decision") or DEFAULT_DECISION,
        "unverified": entry.get("unverified", ""),
        "status": status, "reasons": reasons,
        "files": [{"label": lbl, "kind": k, "sha256": sha256(p.read_bytes()), "bytes": p.stat().st_size, "_path": p}
                  for lbl, p, k in files],
    }


def render(records: list[dict], title: str, sbom_digest: str, refs: list[dict] | None = None) -> str:
    out = []
    w = out.append
    w(f"THIRD-PARTY NOTICES - {title}")
    w("=" * 72)
    w("")
    w("This file collects the licence and notice texts of third-party components that")
    w("are shipped inside the Agentic Apps artifacts, as far as those texts could be")
    w("found. It lists what was collected. It does not state that any component or")
    w("any distribution is cleared, approved or compliant.")
    w("")
    w("The licence of Agentic Apps itself is stated in LICENSING.md (same folder).")
    w(f"Component list: CycloneDX SBOM, sha256 {sbom_digest}; generator version {GENERATOR_VERSION}.")
    w("Components for which no licence text could be found are marked TEXT NOT FOUND")
    w("below and are listed in NOTICES_MISSING.md.")
    w("")
    w("INDEX")
    w("-" * 72)
    for i, r in enumerate(records, 1):
        mark = {"text-found": "", "not-third-party": "   (not a third-party notice, see below)"}.get(r["status"], "   ** TEXT NOT FOUND **")
        w(f"[{i:03d}] {r['name']} {r['version']} - {r['declared']}{mark}")
    w("")
    printed: dict[str, str] = {}
    for i, r in enumerate(records, 1):
        w("=" * 72)
        w(f"[{i:03d}] {r['name']} {r['version']}")
        w(f"Declared licence: {r['declared']}")
        if r["metadata_hint"]:
            w(f"Package metadata says: {r['metadata_hint']}")
        if r["note"]:
            w(f"Note: {r['note']}")
        if r["unverified"]:
            w(f"UNVERIFIED: {r['unverified']}")
        if r["status"] == "not-third-party":
            w("Not a third-party notice: " + "; ".join(r["reasons"]))
        elif r["status"] != "text-found":
            w("TEXT NOT FOUND: " + ("; ".join(r["reasons"]) or "no reason recorded"))
        for f in r["files"]:
            w("-" * 72)
            if f["sha256"] in printed:
                w(f"File: {f['label']}")
                w(f"(identical to the text printed under {printed[f['sha256']]})")
                continue
            printed[f["sha256"]] = f"[{i:03d}] {r['name']} {r['version']}, file {f['label']}"
            w(f"File: {f['label']}")
            w("")
            w(read_text(f["_path"]).rstrip("\n"))
        w("")
    out.extend(render_reference(refs or [], printed))
    return "\n".join(out) + "\n"


def sbom_limitations(sbom: Path) -> set[str]:
    """Ids (e.g. 'L-LIBYAML') of the `redevops:limitation:<id>` properties of the SBOM's metadata."""
    doc = json.loads(sbom.read_text(encoding="utf-8"))
    out = set()
    for p in (doc.get("metadata") or {}).get("properties") or []:
        n = p.get("name", "")
        if n.startswith("redevops:limitation:"):
            out.add(n[len("redevops:limitation:"):])
    return out


def resolve_reference_notices(refs: list[dict], limitations: set[str], texts_dir: Path) -> list[dict]:
    """Texts for libraries that the SBOM can only name as a LIMITATION (linked in, not enumerable from the binary)."""
    out = []
    for r in refs:
        if r.get("limitation") not in limitations:
            continue
        files, reasons = [], []
        for t in r.get("texts", []):
            p = texts_dir / t
            if p.is_file():
                files.append({"label": f"texts/{t}", "kind": "licence", "sha256": sha256(p.read_bytes()),
                              "bytes": p.stat().st_size, "_path": p})
            else:
                raise SystemExit(f"reference notice {r['id']}: text file texts/{t} named in components.json is missing")
        out.append({"id": r["id"], "limitation": r["limitation"], "title": r["title"], "declared": r.get("declared", ""),
                    "decision": r.get("decision", ""), "note": r.get("note", ""), "unverified": r.get("unverified", ""),
                    "status": "text-found" if files and not reasons else "text-not-found", "reasons": reasons,
                    "files": files})
    return out


def render_reference(refs: list[dict], printed: dict[str, str]) -> list[str]:
    if not refs:
        return []
    out = ["=" * 72, "REFERENCE NOTICES", "-" * 72,
           "The texts below belong to libraries that the SBOM can only name as a LIMITATION: they are linked into a",
           "shipped binary (or are part of the interpreter build) but their presence and version cannot be read from",
           "the binary. Each text is the upstream text of the version named in its note; whether that exact version is",
           "the one linked is UNVERIFIED unless stated. A text listed here is not a statement that the library is",
           "cleared. Items without a text are listed in NOTICES_MISSING.md.", ""]
    for i, r in enumerate(refs, 1):
        out.append("=" * 72)
        out.append(f"[R{i:02d}] {r['title']}")
        out.append(f"Declared licence: {r['declared'] or '(not recorded)'}   Register item: {r['decision'] or '-'}   SBOM limitation: {r['limitation']}")
        if r["note"]:
            out.append(f"Note: {r['note']}")
        if r["unverified"]:
            out.append(f"UNVERIFIED: {r['unverified']}")
        for reason in r["reasons"]:
            out.append(f"TEXT NOT FOUND: {reason}")
        for f in r["files"]:
            out.append("-" * 72)
            out.append(f"File: {f['label']}")
            if f["sha256"] in printed:
                out.append(f"(identical to the text printed under {printed[f['sha256']]})")
                continue
            printed[f["sha256"]] = f"[R{i:02d}] {r['title']}, file {f['label']}"
            out.append("")
            out.append(read_text(f["_path"]).rstrip("\n"))
        out.append("")
    return out


def missing_md(missing: list[dict]) -> str:
    out = ["# Components without a licence text in this build", "",
           "Each row is a decision item, not a skipped item: see packaging/DECISIONS-REQUIRED.md.", ""]
    if not missing:
        out.append("None.")
    else:
        out += ["| Component | Version | Declared | Reason | Decision |", "|---|---|---|---|---|"]
        for m in missing:
            out.append(f"| {m['name']} | {m['version']} | {m['declared']} | {'; '.join(m['reasons']) or 'unspecified'} | {m['decision'] or 'UNASSIGNED'} |")
    return "\n".join(out) + "\n"


def run(sbom: Path, site_dirs, crate_dirs, texts_dir: Path, components: Path | None, out: Path, title: str,
        exclude: list[str]) -> tuple[list[dict], list[dict]]:
    entries = {}
    if components and components.is_file():
        doc = json.loads(components.read_text(encoding="utf-8"))
        entries = dict(doc.get("entries", {}))
        for sc in doc.get("static_components", []):   # generic components: same file, same keys
            entries[f"generic:{norm(sc['name'])}"] = {
                k: sc[k] for k in ("texts", "declared", "decision", "note", "unverified", "missing_reason")
                if k in sc}
    ref_defs = []
    if components and components.is_file():
        ref_defs = list(json.loads(components.read_text(encoding="utf-8")).get("reference_notices", []))
    refs = resolve_reference_notices(ref_defs, sbom_limitations(sbom), texts_dir)
    comps = [c for c in load_components(sbom) if norm(c["name"]) not in {norm(e) for e in exclude}]
    records = [resolve(c, entries, site_dirs, crate_dirs, texts_dir) for c in comps]
    missing = [r for r in records if r["status"] == "text-not-found"]
    not_third_party = [r for r in records if r["status"] == "not-third-party"]
    out.mkdir(parents=True, exist_ok=True)
    digest = sha256(sbom.read_bytes())
    (out / "THIRD_PARTY_NOTICES.txt").write_text(render(records, title, digest, refs), encoding="utf-8", newline="\n")
    clean = lambda r: {k: v for k, v in r.items() if k != "files"} | {
        "files": [{k: v for k, v in f.items() if k != "_path"} for f in r["files"]]}
    (out / "NOTICES_MISSING.json").write_text(
        json.dumps([clean(m) for m in missing], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "NOTICES_MISSING.md").write_text(missing_md(missing), encoding="utf-8", newline="\n")
    (out / "notices-report.json").write_text(json.dumps(
        {"sbom_sha256": digest, "generator_version": GENERATOR_VERSION, "components": len(records),
         "text_found": len(records) - len(missing) - len(not_third_party), "text_not_found": len(missing),
         "not_third_party": len(not_third_party),
         "reference_notices": [clean(r) for r in refs],
         "records": [clean(r) for r in records]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return records, missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sbom", required=True, type=Path)
    ap.add_argument("--site-packages", action="append", default=[], type=Path,
                    help="site-packages of the BUILD venv (repeatable)")
    ap.add_argument("--crates-dir", action="append", default=[], type=Path,
                    help="cargo registry/src/<index> or `cargo vendor` directory (repeatable)")
    ap.add_argument("--texts-dir", default=HERE / "texts", type=Path)
    ap.add_argument("--components", default=HERE / "components.json", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--title", default="Agentic Apps")
    ap.add_argument("--exclude", action="append", default=[], help="component name to leave out (repeatable)")
    ap.add_argument("--fail-on-missing", action="store_true",
                    help="exit 1 if any component has no licence text (the release gate)")
    a = ap.parse_args(argv)
    records, missing = run(a.sbom, a.site_packages, a.crates_dir, a.texts_dir, a.components, a.out, a.title,
                           a.exclude)
    ntp = sum(1 for r in records if r["status"] == "not-third-party")
    print(f"components: {len(records)}  text found: {len(records) - len(missing) - ntp}  "
          f"not third-party (listed): {ntp}  text NOT found: {len(missing)}  -> {a.out}")
    for m in missing:
        print(f"  MISSING {m['type']}:{m['name']} {m['version']}  [{m['decision'] or 'UNASSIGNED'}]  "
              f"{'; '.join(m['reasons'])}", file=sys.stderr)
    return 1 if (a.fail_on_missing and missing) else 0


if __name__ == "__main__":
    sys.exit(main())
