"""Argument handling and input loading shared by generate_sbom.py and verify_sbom.py."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import inventory as INV
from .analyze import Analysis, analyze
from .archive import Archive, ExecutableArchive, ListingArchive


def add_input_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("archive (one of)")
    g.add_argument("--sidecar", help="the built PyInstaller executable (full fidelity: reads bytes)")
    g.add_argument("--pkg-listing", help="text listing of the PKG archive (listing-only run; needs --facts for native versions)")
    g.add_argument("--pyz-listing", help="plain module-name listing of the PYZ (when not inside --pkg-listing)")
    g = ap.add_argument_group("version / metadata oracles")
    g.add_argument("--wheelhouse", help="packaging/wheelhouse (the installed wheels: hashes, licences, top-levels, upstream SBOMs)")
    g.add_argument("--inventory-env", help="python of the freeze venv (adds PyInstaller, setuptools, packaging ...)")
    g.add_argument("--inventory-json", help="saved output of `python -I packaging/sbom/inventory.py --dump` in the freeze venv")
    g.add_argument("--lock", action="append", default=[], help="requirements lock(s): name==version oracle when there is no wheelhouse")
    g.add_argument("--facts", help="JSON of facts for listing-only runs (native versions, UI libraries) - every use is labelled")
    g.add_argument("--build-manifest", help="packaging/dist/build-manifest.json (python/pyinstaller versions, commit)")
    g.add_argument("--git-pins", help="packaging/git-pins.json (first-party tag/commit pins)")
    g = ap.add_argument_group("launcher (Rust)")
    g.add_argument("--cargo-manifest", help="deploy/launcher/src-tauri/Cargo.toml")
    g.add_argument("--cargo-lock", help="deploy/launcher/src-tauri/Cargo.lock")
    g.add_argument("--cargo-target", default="aarch64-apple-darwin", help="target triple the crate set is resolved for")
    g.add_argument("--cargo-metadata-json", help="saved `cargo metadata` output (instead of running cargo)")
    g.add_argument("--launcher-bin", help="the built launcher binary (hash + crate-path cross-check)")
    g.add_argument("--tauri-conf", help="deploy/launcher/src-tauri/tauri.conf.json")


def load_archive(a) -> Archive:
    if a.sidecar:
        return ExecutableArchive(a.sidecar)
    if a.pkg_listing:
        return ListingArchive(a.pkg_listing, a.pyz_listing)
    raise SystemExit("give --sidecar or --pkg-listing")


def load_inventory(a) -> dict[str, dict]:
    parts = []
    for l in a.lock:
        parts.append(INV.parse_lock(l))
    if a.inventory_env:
        parts.append(INV.from_environment(a.inventory_env))
    if a.inventory_json:
        parts.append(json.loads(Path(a.inventory_json).read_text(encoding="utf-8")))
    if a.wheelhouse:
        parts.append(INV.from_wheels(a.wheelhouse))
    if not parts:
        raise SystemExit("give --wheelhouse, --inventory-env, --inventory-json or --lock")
    return INV.merge(*parts)


def load_json(p: str | None) -> dict:
    if not p:
        return {}
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    d.setdefault("_source", Path(p).name)
    return d


def run_analysis(a) -> tuple[Archive, dict, Analysis, dict, dict]:
    arc = load_archive(a)
    inv = load_inventory(a)
    facts = load_json(a.facts)
    manifest = load_json(a.build_manifest)
    tc = manifest.get("toolchain", {})
    pyver = (tc.get("python_sidecar") or tc.get("python_wheelhouse") or {}).get("version")
    pyiver = (tc.get("python_sidecar") or {}).get("dists", {}).get("pyinstaller")
    res = analyze(arc, inv, facts, a.wheelhouse, python_version=pyver, pyinstaller_version=pyiver)
    return arc, inv, res, facts, manifest
