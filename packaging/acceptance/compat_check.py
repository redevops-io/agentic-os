#!/usr/bin/env python3
"""Check deploy/compat/releases.json, the machine-readable statement of which versions belong together.

  compat_check.py                      offline: shape, uniqueness, and agreement with this checkout's own pins
  compat_check.py --release-dir DIR --tag TAG
                                       also: the entry for TAG agrees with the PUBLISHED release in DIR (its signed
                                       release-manifest.json)

The desktop app's version and its release tag are different numbers (app 0.1.4 is published as r0prime-v0.1.5; app 0.1.1 as
v0.6.1). An installer or a person must not derive one from the other: this file is where the pairing is written down, and
the published-release check fails a release whose pairing is missing or wrong. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "deploy" / "compat" / "releases.json"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SHA = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DESKTOP_TAG = re.compile(r"^(r0prime-)?v\d+\.\d+\.\d+$")
STATUS = {"current", "superseded", "withdrawn"}


def offline(doc: dict, root: Path = ROOT) -> list[str]:
    bad: list[str] = []

    def need(c: bool, msg: str) -> None:
        if not c:
            bad.append(msg)

    need(doc.get("schema") == "redevops.release-compat/1", "schema must be redevops.release-compat/1")
    desk, bund = doc.get("desktop") or [], doc.get("bundle") or []
    need(bool(desk) and bool(bund), "both a desktop and a bundle list are required")
    for kind, rows, key in (("desktop", desk, "app_version"), ("desktop", desk, "release_tag"), ("bundle", bund, "bundle_tag")):
        vals = [r.get(key) for r in rows]
        need(len(vals) == len(set(vals)), f"{kind}: {key} values repeat: {vals}")
    for kind, rows in (("desktop", desk), ("bundle", bund)):
        need(sum(r.get("status") == "current" for r in rows) == 1, f"{kind}: exactly one entry must be current")
    for r in desk:
        t = f"desktop {r.get('app_version')}"
        need(bool(SEMVER.match(str(r.get("app_version", "")))), f"{t}: app_version is not X.Y.Z")
        need(bool(DESKTOP_TAG.match(str(r.get("release_tag", "")))), f"{t}: release_tag is not r0prime-vX.Y.Z or vX.Y.Z")
        need(r.get("status") in STATUS, f"{t}: status must be one of {sorted(STATUS)}")
        need(str(r.get("release_url", "")).endswith("/releases/tag/" + str(r.get("release_tag"))), f"{t}: release_url does not point at its release_tag")
        if r.get("status") == "current":
            for k in ("build_commit", "manifest_sha256", "manifest_url", "kernel", "projects_ui", "platforms"):
                need(bool(r.get(k)), f"{t}: the current entry needs {k}")
            need(bool(COMMIT.match(str(r.get("build_commit", "")))), f"{t}: build_commit is not a full commit")
            need(bool(SHA.match(str(r.get("manifest_sha256", "")))), f"{t}: manifest_sha256 is not a sha256")
            need(f"/download/{r.get('release_tag')}/" in str(r.get("manifest_url", "")), f"{t}: manifest_url is not under its release_tag")
            need(bool(SHA.match(str((r.get("projects_ui") or {}).get("artifact_sha256", "")))), f"{t}: projects_ui.artifact_sha256 is not a sha256")
    for r in bund:
        t = f"bundle {r.get('bundle_tag')}"
        need(bool(SEMVER.match(str(r.get("bundle_tag", "")))), f"{t}: bundle_tag is not X.Y.Z")
        need(r.get("status") in STATUS, f"{t}: status must be one of {sorted(STATUS)}")
        k = r.get("kernel") or {}
        need(k.get("tag") == "v" + str(k.get("package_version")), f"{t}: kernel.tag and kernel.package_version disagree")
        need((r.get("projects_image") or {}).get("tag") == f"bundle-{r.get('bundle_tag')}", f"{t}: projects_image.tag is not bundle-<bundle_tag>")
    # the current bundle must be the one this checkout installs
    env = root / "deploy" / "combined" / ".env.bundle.example"
    cur = next((r for r in bund if r.get("status") == "current"), None)
    if env.is_file() and cur:
        pins = dict(line.split("=", 1) for line in env.read_text().splitlines() if "=" in line and not line.startswith("#"))
        for key, want in (("BUNDLE_TAG", cur.get("bundle_tag")), ("PROJECTS_IMAGE_TAG", (cur.get("projects_image") or {}).get("tag")),
                          ("QA_IMAGE_TAG", cur.get("redevops_qa")), ("EDGE_SENTINEL_IMAGE_TAG", cur.get("edge_sentinel"))):
            need(pins.get(key) == want, f"bundle {cur.get('bundle_tag')}: {key} is {pins.get(key)} in .env.bundle.example, the compatibility file says {want}")
    return bad


def published(doc: dict, tag: str, d: Path) -> list[str]:
    bad: list[str] = []
    row = next((r for r in doc.get("desktop") or [] if r.get("release_tag") == tag), None)
    if row is None:
        return [f"no desktop entry for the release tag {tag}: add it to deploy/compat/releases.json"]
    man = d / "release-manifest.json"
    if not man.is_file():
        return [f"{tag}: the compatibility file names a manifest and the release carries none"] if row.get("manifest_sha256") else []
    got = hashlib.sha256(man.read_bytes()).hexdigest()
    if row.get("manifest_sha256") and got != row["manifest_sha256"]:
        bad.append(f"{tag}: the published manifest is {got}, the compatibility file says {row['manifest_sha256']}")
    m = json.loads(man.read_text())
    if row.get("build_commit") and m.get("release_commit") != row["build_commit"]:
        bad.append(f"{tag}: the manifest names commit {m.get('release_commit')}, the compatibility file says {row['build_commit']}")
    names = [f["name"] for fs in (m.get("platforms") or {}).values() for f in fs]
    off = [n for n in names if f"_{row['app_version']}_" not in n]
    if off or not names:
        bad.append(f"{tag}: installers that do not carry app version {row['app_version']} in their name: {off or 'none listed'}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", type=Path, default=FILE)
    ap.add_argument("--release-dir", type=Path)
    ap.add_argument("--tag")
    a = ap.parse_args(argv)
    doc = json.loads(a.file.read_text())
    bad = offline(doc)
    if a.tag:
        if not a.release_dir:
            ap.error("--tag needs --release-dir")
        bad += published(doc, a.tag, a.release_dir)
    for b in bad:
        print("FAIL ", b)
    cur = next((r for r in doc.get("desktop") or [] if r.get("status") == "current"), {})
    curb = next((r for r in doc.get("bundle") or [] if r.get("status") == "current"), {})
    print(f"{'REFUSED' if bad else 'OK'}: desktop app {cur.get('app_version')} is published as {cur.get('release_tag')}; current bundle {curb.get('bundle_tag')}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
