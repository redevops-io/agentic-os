#!/usr/bin/env python3
"""Render packaging/SOURCE-OFFER.md for one build from its SBOM + the pinned source references.

  generate_source_offer.py --sbom packaging/dist/sbom/sbom.cdx.json --out packaging/dist/SOURCE-OFFER.md \
      [--template packaging/SOURCE-OFFER.md] [--git-pins packaging/git-pins.json] \
      [--sources packaging/source-offer/third-party-sources.json] [--build-manifest packaging/dist/build-manifest.json]

Exit 1 when a shipped component has no source reference entry (so the offer cannot silently shrink).
Writes facts only; it never says a licence is cleared.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIRST_PARTY = {"agentic-os", "runtime-contracts", "knowledge-frontier", "redevops-connectors"}
HERE = Path(__file__).resolve().parent.parent


def props(c: dict) -> dict[str, str]:
    return {p["name"]: p["value"] for p in c.get("properties", [])}


def lic(c: dict) -> str:
    for l in c.get("licenses", []):
        if "expression" in l:
            return l["expression"]
        if "license" in l:
            return l["license"].get("id") or l["license"].get("name", "")
    return "none read"


def md_row(cells: list[str]) -> str:
    return "| " + " | ".join(x.replace("|", "\\|").replace("\n", " ") for x in cells) + " |"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sbom", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", default=str(HERE / "SOURCE-OFFER.md"))
    ap.add_argument("--git-pins", default=str(HERE / "git-pins.json"))
    ap.add_argument("--sources", default=str(HERE / "source-offer" / "third-party-sources.json"))
    ap.add_argument("--build-manifest")
    a = ap.parse_args()
    bom = json.loads(Path(a.sbom).read_text(encoding="utf-8"))
    pins = {p["name"]: p for p in json.loads(Path(a.git_pins).read_text(encoding="utf-8"))["dependencies"]}
    srcs = json.loads(Path(a.sources).read_text(encoding="utf-8"))["components"]
    manifest = json.loads(Path(a.build_manifest).read_text(encoding="utf-8")) if a.build_manifest and Path(a.build_manifest).exists() else {}
    comps = bom["components"]
    missing: list[str] = []

    # --- build identity ---------------------------------------------------------------------
    meta_props = {p["name"]: p["value"] for p in bom["metadata"].get("component", {}).get("properties", [])}
    src = manifest.get("source", {})
    ident = [f"- SBOM serial: `{bom['serialNumber']}` ({len(comps)} components, CycloneDX {bom['specVersion']})",
             f"- agentic-os source commit: `{meta_props.get('redevops:source-commit', 'unknown')}` (tree `{meta_props.get('redevops:source-tree', 'unknown')}`)"]
    ident_rel = manifest.get("identity") or {}
    if manifest.get("release_line"):
        ident.append(f"- product: {ident_rel.get('product_name', '?')} {ident_rel.get('version', '?')}, release line `{manifest['release_line']}`, "
                     f"bundle identifier `{ident_rel.get('bundle_identifier', '?')}` (not the frozen v0.3.2 identity; see docs/R0_PRIME_IDENTITY.md)")
    if src.get("dirty_files"):
        ident.append(f"- WARNING: the build tree had {src['dirty_files']} uncommitted/untracked files: the commit above does not fully describe the build")
    ident.append(f"- archive analysed: {meta_props.get('redevops:archive-source', '?')} sha256 `{meta_props.get('redevops:archive-sha256', '')}`")
    tc = manifest.get("toolchain", {})
    for k in ("rustc", "cargo", "tauri_cli", "uv"):
        if tc.get(k):
            ident.append(f"- {k}: {str(tc[k]).splitlines()[0]}")
    py = tc.get("python_sidecar") or {}
    if py:
        ident.append(f"- build interpreter: CPython {py.get('version')} ({py.get('source')}); executable sha256 `{py.get('executable_sha256')}`")
    rn = manifest.get("runner", {}).get("env", {})
    if rn.get("ImageVersion"):
        ident.append(f"- runner image: {rn.get('ImageOS')} version {rn['ImageVersion']}")

    # --- first-party --------------------------------------------------------------------------
    fp = ["| component | declared licence | repository | tag | tag object | commit | obtain |", "|---|---|---|---|---|---|---|"]
    for c in comps:
        p = props(c)
        key = p.get("redevops:key", "")
        if not key.startswith("pypi:") or key.split(":", 1)[1] not in FIRST_PARTY:
            continue
        n = key.split(":", 1)[1]
        if n == "agentic-os":
            commit = p.get("redevops:git-commit", meta_props.get("redevops:source-commit", "unknown"))
            fp.append(md_row([f"agentic-os {c.get('version')} (control plane, sidecar entry script, Tauri launcher, packaging)", lic(c),
                              "https://github.com/redevops-io/agentic-os", "(none: R0-prime commit)", "-", f"`{commit}`",
                              f"`git clone https://github.com/redevops-io/agentic-os && git checkout {commit}`; PUBLICATION OF THIS COMMIT IS AN OPEN OWNER ACTION (decision pins-SO-1)"]))
        else:
            pin = pins.get(n, {})
            fp.append(md_row([f"{n} {c.get('version')}", lic(c), pin.get("url", "?"), pin.get("tag", "?"), f"`{pin.get('tag_object', '?')}`", f"`{pin.get('commit', '?')}`",
                              f"`git clone {pin.get('url', '?')} && git checkout {pin.get('commit', '?')}`"]))
    if any(props(c).get("redevops:key") == "ui-bundle" for c in comps):
        ui_rec_path = HERE / "ui-bundle-source.json"
        if ui_rec_path.exists():
            u = json.loads(ui_rec_path.read_text())
            published = "published" if u.get("source_commit_published") else "NOT PUBLISHED"
            fp.append(md_row(["Projects UI bundle (agentic_os/projects_ui, minified JavaScript + CSS)", "see agentic-os", u["repository"],
                              "(none: local, unpublished commit)" if not u.get("source_commit_published") else "see record", "-", f"`{u['source_commit']}`",
                              f"{published}: this commit exists only in a local clone, the public repository holds the base `{u['base_commit'][:8]}` only; "
                              f"npm lock git blob `{u['package_lock_git_blob'][:8]}`; rebuild: `{'` then `'.join(u['build']['commands'])}` "
                              f"(record packaging/ui-bundle-source.json; decisions pins-10, pins-04 and D-LIC-12 stay OPEN)"]))
        else:
            fp.append(md_row(["Projects UI bundle (agentic_os/projects_ui, minified JavaScript + CSS)", "see agentic-os", "https://github.com/redevops-io/redevops-projects",
                              "NOT RECORDED", "-", "NOT RECORDED", "the bundle is vendored into agentic_os; the redevops-projects tag/commit and npm lock that produced it are not recorded in this repository (decision pins-10)"]))
    if any(props(c).get("redevops:key") == "licenses-folder" for c in comps):
        fp.append(md_row(["Licenses folder (product licence files, THIRD_PARTY_NOTICES.txt)", "n/a (licence and notice texts)", "https://github.com/redevops-io/agentic-os", "-", "-", "same commit as agentic-os",
                          "generated by packaging/notices from the SBOM; texts are in the repository and `packaging/notices/texts`"]))
    launcher = next((c for c in comps if props(c).get("redevops:key") == "launcher"), None)
    if launcher:
        fp.append("")
        fp.append(f"The launcher (`redevops-launcher`, declared licence per its Cargo.toml: {lic(launcher)}) lives in the agentic-os repository under `deploy/launcher/`.")

    # --- third party --------------------------------------------------------------------------
    groups: dict[str, list[list[str]]] = {"Python (PyPI) distributions": [], "JavaScript (UI bundle)": [], "Native libraries and runtimes": [], "Rust crates (launcher)": [],
                                          "Rust crates statically linked into Python wheels (upstream-declared)": []}
    nosrc: list[str] = []
    for c in sorted(comps, key=lambda c: (c["name"].lower(), c.get("version", ""))):
        p = props(c)
        key = p.get("redevops:key", "")
        if c.get("scope") == "excluded" or c["type"] in ("platform", "application"):
            continue
        if key.startswith("pypi:") and key.split(":", 1)[1] in FIRST_PARTY:
            continue
        if key in ("ui-bundle", "licenses-folder") or key.startswith("asset:"):
            continue
        ent = srcs.get(key) or srcs.get(c.get("purl", ""))
        if ent is None:
            missing.append(key or c["name"])
            continue
        sh = "shipped in: " + p.get("redevops:shipped_in", "?")
        if "unresolved" in ent or ent.get("source", {}).get("type") in ("none", None):
            nosrc.append(f"- {c['name']} {c.get('version', '(version unknown)')} ({lic(c)}): " + (ent.get("unresolved") or ent["source"].get("note", "")))
            continue
        s = ent["source"]
        ref = s.get("url") or s.get("project_url", "")
        h = s.get("sha256") or s.get("integrity") or s.get("sha1") or ""
        row = [c["name"], c.get("version", ""), lic(c), ref, h or ("not recorded" if not s.get("note") else s["note"])]
        if key.startswith("pypi:") or key in ("certifi-bundle", "pyinstaller-runtime"):
            g = "Python (PyPI) distributions"
        elif key.startswith("npm:"):
            g = "JavaScript (UI bundle)"
        elif key.startswith("embedded:"):
            g = "Rust crates statically linked into Python wheels (upstream-declared)"
        elif key.startswith("cargo:"):
            g = "Rust crates (launcher)"
        else:
            g = "Native libraries and runtimes"
        groups[g].append(row)
    tp: list[str] = []
    for g, rows in groups.items():
        if not rows:
            continue
        tp += [f"### {g} ({len(rows)})", "", md_row(["component", "version", "declared licence", "source", "sha256 / integrity"]), "|---|---|---|---|---|"]
        tp += [md_row(r) for r in rows]
        tp.append("")
    ns = nosrc or ["(none)"]
    for c in comps:  # Microsoft DLLs etc. flagged in the SBOM itself
        pass
    text = Path(a.template).read_text(encoding="utf-8")
    for marker, body in (("build-inputs", ident), ("first-party", fp), ("third-party", tp), ("no-source", ns)):
        text = text.replace(f"<!-- GENERATED:{marker} -->", "\n".join(body))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text, encoding="utf-8")
    print(f"wrote {a.out}: {sum(len(r) for r in groups.values())} third-party source references, {len(nosrc)} without a nameable source")
    if missing:
        print("NO source-reference entry for: " + ", ".join(sorted(set(missing))[:20]) + (" ..." if len(set(missing)) > 20 else ""))
        print("run packaging/tools/collect_source_refs.py to refresh packaging/source-offer/third-party-sources.json")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
