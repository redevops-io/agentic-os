#!/usr/bin/env python3
"""Fetch the source packages of Rust crates the SBOM lists but no local crates directory holds.

The SBOM of the built sidecar lists, besides the launcher's crates (present in the cargo registry after
`cargo fetch --locked`), the Rust crates that the cryptography and pydantic_core wheels declare as statically
linked (their own embedded SBOMs). Their sources are not in any local registry; the licence texts live in the
published crate packages. This tool downloads each missing `<name>-<version>.crate` from static.crates.io and
VERIFIES it before extracting anything:
  * against the SHA-256 the SBOM records for the component, if there is one, else
  * against the `cksum` of the crates.io sparse index entry for that exact version.
A crate that fails verification is not extracted (it is reported, and the notices list it as missing).
Only licence/notice files, Cargo.toml and .cargo_vcs_info.json are extracted (no code), into
OUT/<name>-<version>/. For a package that holds no licence file, the licence files of the upstream repository AT
THE PACKAGED COMMIT (from .cargo_vcs_info.json) are fetched when the repository is on GitHub, saved as
LICENSE-upstream-<file> together with UPSTREAM-SOURCE.txt (URL, commit, sha256): generate_notices.py then shows
the text with an UNVERIFIED note, because it is not the file the crate package shipped.

  fetch_crate_sources.py --sbom FILE --out DIR [--crates-dir DIR]... [--offline]

The generator must be given OUT before the registry directories (--crates-dir order) so that these files are found first.

Standard library only. Network: static.crates.io, index.crates.io, raw.githubusercontent.com.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "redevops-notices/1 (licence texts for a source offer; contact: redevops.io)"}
KEEP = re.compile(r"^(licen[cs]e|licen[cs]es|copying|unlicense|copyright|notice)([-_. ].*)?$", re.I)
UPSTREAM_NAMES = ("LICENSE", "LICENSE-MIT", "LICENSE-APACHE", "LICENSE-ZLIB", "LICENSE.md", "LICENSE.txt", "LICENSE-MIT.md",
                  "LICENSE-APACHE.md", "COPYING", "LICENCE", "LICENSE-BSD", "LICENSE-2.0.txt")


def _gen():
    spec = importlib.util.spec_from_file_location("generate_notices", HERE / "generate_notices.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def get(url: str, timeout: int = 60) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def index_path(name: str) -> str:
    n = name.lower()
    if len(n) == 1:
        return f"1/{n}"
    if len(n) == 2:
        return f"2/{n}"
    if len(n) == 3:
        return f"3/{n[0]}/{n}"
    return f"{n[:2]}/{n[2:4]}/{n}"


def index_cksum(name: str, version: str) -> str | None:
    raw = get(f"https://index.crates.io/{index_path(name)}")
    if raw is None:
        return None
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("name", "").lower() == name.lower() and row.get("vers") == version:
            return row.get("cksum")
    return None


def sbom_hashes(sbom: Path) -> dict[tuple[str, str], str]:
    doc = json.loads(sbom.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], str] = {}
    stack = list(doc.get("components") or [])
    while stack:
        c = stack.pop()
        stack.extend(c.get("components") or [])
        for h in c.get("hashes") or []:
            if str(h.get("alg", "")).upper().replace("-", "") == "SHA256" and (c.get("purl") or "").startswith("pkg:cargo/"):
                out[(c.get("name", ""), c.get("version", ""))] = h["content"].lower()
    return out


def safe_member(m: tarfile.TarInfo) -> str | None:
    """Return the path below the top directory if m is a plain file worth keeping, else None."""
    if not m.isfile():
        return None
    parts = Path(m.name).parts
    if len(parts) < 2 or ".." in parts or m.name.startswith("/"):
        return None
    rel = Path(*parts[1:])
    keep = rel.name in ("Cargo.toml", ".cargo_vcs_info.json") or (
        len(rel.parts) == 1 and KEEP.match(rel.name)) or (
        len(rel.parts) > 1 and rel.parts[0] in ("licenses", "LICENSES", "license", "licences") )
    return rel.as_posix() if keep and m.size < 2_000_000 else None


def upstream_licences(dest: Path) -> str:
    """Fetch the licence files of the upstream GitHub repository at the packaged commit; return a status line."""
    vcs, toml = dest / ".cargo_vcs_info.json", dest / "Cargo.toml"
    if not vcs.is_file() or not toml.is_file():
        return "no .cargo_vcs_info.json: no upstream commit to fetch from"
    sha1 = json.loads(vcs.read_text()).get("git", {}).get("sha1", "")
    path_in_vcs = json.loads(vcs.read_text()).get("path_in_vcs", "")
    m = re.search(r'^repository\s*=\s*"https?://github\.com/([^/"]+/[^/".]+)', toml.read_text(encoding="utf-8", errors="replace"), re.M)
    if not m or not re.fullmatch(r"[0-9a-f]{40}", sha1):
        return "repository is not on GitHub or the commit is unknown"
    repo, got, lines = m.group(1), 0, []
    dirs = [path_in_vcs, ""] if path_in_vcs else [""]
    seen = set()
    for d in dirs:
        for n in UPSTREAM_NAMES:
            rel = f"{d}/{n}".lstrip("/")
            url = f"https://raw.githubusercontent.com/{repo}/{sha1}/{rel}"
            data = get(url, 30)
            if data and len(data) > 200:
                h = hashlib.sha256(data).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                (dest / f"LICENSE-upstream-{n}").write_bytes(data)
                lines.append(f"{url} sha256 {h}")
                got += 1
    if got:
        (dest / "UPSTREAM-SOURCE.txt").write_text(
            f"The crate package holds no licence file. These files are from the upstream repository {repo} at the commit "
            f"the package was built from ({sha1}); they are not part of the package.\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return f"{got} upstream file(s) from {repo}@{sha1[:10]}"
    return f"no licence file found in {repo}@{sha1[:10]}"


def fetch_one(name: str, version: str, want: str | None, out: Path, offline: bool) -> dict:
    dest = out / f"{name}-{version}"
    rec = {"name": name, "version": version, "dir": dest.name}
    if dest.is_dir() and (dest / "Cargo.toml").is_file():
        return rec | {"status": "cached"}
    if offline:
        return rec | {"status": "missing (offline)"}
    data = get(f"https://static.crates.io/crates/{name}/{name}-{version}.crate")
    if data is None:
        return rec | {"status": "download failed"}
    got = hashlib.sha256(data).hexdigest()
    basis = "sbom"
    if want is None:
        want, basis = index_cksum(name, version), "crates.io index"
    if want is None:
        return rec | {"status": "no checksum to verify against: not extracted"}
    if got != want.lower():
        return rec | {"status": f"CHECKSUM MISMATCH ({basis}): not extracted"}
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            rel = safe_member(m)
            if rel:
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tf.extractfile(m).read())
    rec |= {"status": "verified", "sha256": got, "verified_against": basis}
    if not any(KEEP.match(p.name) for p in dest.iterdir() if p.is_file() and p.name not in ("Cargo.toml",)) \
            and not any((dest / d).is_dir() for d in ("licenses", "LICENSES", "license", "licences")):
        rec["upstream"] = upstream_licences(dest)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sbom", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--crates-dir", action="append", default=[], type=Path)
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    gen = _gen()
    excl = {gen.norm(e) for e in a.exclude}
    hashes = sbom_hashes(a.sbom)
    todo, local_no_licence = [], []
    for c in gen.load_components(a.sbom):
        if c["type"] != "cargo" or gen.norm(c["name"]) in excl:
            continue
        d = gen.find_crate_dir(a.crates_dir, c["name"], c["version"])
        if d is None:
            todo.append((c["name"], c["version"]))
        elif not gen.licence_files_in(d):
            local_no_licence.append((c["name"], c["version"], d))
    a.out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        recs = list(ex.map(lambda t: fetch_one(t[0], t[1], hashes.get(t), a.out, a.offline), todo))
    # crates in the local registry (checksum-verified by cargo against Cargo.lock) whose package holds no licence file:
    # look for the upstream repository's files at the packaged commit; they go to OUT/<name>-<version>/ next to a copy of
    # the package's Cargo.toml / .cargo_vcs_info.json, and OUT must be listed BEFORE the registry in --crates-dir for the
    # generator to see them.
    for name, version, d in local_no_licence:
        dest = a.out / f"{name}-{version}"
        rec = {"name": name, "version": version, "dir": dest.name, "status": "local registry package has no licence file"}
        if a.offline:
            rec["upstream"] = "offline"
        else:
            dest.mkdir(parents=True, exist_ok=True)
            for f in ("Cargo.toml", ".cargo_vcs_info.json"):
                if (d / f).is_file():
                    (dest / f).write_bytes((d / f).read_bytes())
            rec["upstream"] = upstream_licences(dest)
        recs.append(rec)
    (a.out / "fetch-report.json").write_text(json.dumps(recs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bad = [r for r in recs if r["status"] not in ("verified", "cached", "local registry package has no licence file")]
    print(f"crates not in a local registry: {len(todo)}; verified/cached: {len(recs) - len(bad)}; problems: {len(bad)} -> {a.out}")
    for r in bad:
        print(f"  {r['name']} {r['version']}: {r['status']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
