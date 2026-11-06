#!/usr/bin/env python3
"""Collect the corresponding-source references of the THIRD-PARTY components of an SBOM (online).

  collect_source_refs.py --sbom packaging/dist/sbom/sbom.cdx.json --out packaging/source-offer/third-party-sources.json

For every required component that is not first-party: PyPI sdist URL + sha256 (PyPI JSON API), crates.io
download URL + the Cargo.lock/embedded-SBOM checksum, npm tarball URL + registry integrity, CPython and
OpenSSL release tarballs (the sha256 is computed by downloading, or taken from the wheel's embedded SBOM),
and an explicit `no_source: reason` where none is available (Microsoft redistributable DLLs). Entries that
cannot be resolved are written with `unresolved: reason`, never guessed. The output is the generator input
of packaging/sbom/generate_source_offer.py; it is committed so that the offer can be re-rendered offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

UA = {"User-Agent": "redevops-pins-source-offer (redevops@redevops.io)"}
FIRST_PARTY = {"agentic-os", "runtime-contracts", "knowledge-frontier", "redevops-connectors"}


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r)


def sha256_url(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def props(c: dict) -> dict[str, str]:
    return {p["name"]: p["value"] for p in c.get("properties", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sbom", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--existing", help="previous output: entries are reused, only missing ones are fetched")
    ap.add_argument("--no-download", action="store_true", help="do not download release tarballs to hash them")
    a = ap.parse_args()
    bom = json.loads(Path(a.sbom).read_text(encoding="utf-8"))
    prev = json.loads(Path(a.existing).read_text(encoding="utf-8"))["components"] if a.existing and Path(a.existing).exists() else {}
    out: dict[str, dict] = dict(prev)
    for c in bom["components"]:
        pr = props(c)
        key, name, ver, purl = pr.get("redevops:key", ""), c.get("name"), c.get("version"), c.get("purl", "")
        if c.get("scope") == "excluded" or c["type"] in ("platform", "application", "data") and not key.startswith(("certifi", "npm:")):
            continue
        if key.startswith("pypi:") and key.split(":", 1)[1] in FIRST_PARTY:
            continue
        ident = key or purl
        if ident in out and not out[ident].get("unresolved"):
            continue
        ent: dict = {"name": name, "version": ver, "purl": purl}
        try:
            if key.startswith("pypi:") or key == "certifi-bundle" or key == "pyinstaller-runtime":
                dist = (purl.split("pkg:pypi/")[1].split("@")[0]) if "pkg:pypi/" in purl else None
                if dist:
                    d = get_json(f"https://pypi.org/pypi/{dist}/{ver}/json")
                    sd = next((u for u in d["urls"] if u["packagetype"] == "sdist"), None)
                    ent["source"] = ({"type": "sdist", "url": sd["url"], "sha256": sd["digests"]["sha256"]} if sd else
                                     {"type": "none-on-pypi", "note": "no sdist published for this version", "project_url": f"https://pypi.org/project/{dist}/{ver}/"})
                    ent["upstream"] = (d["info"].get("project_urls") or {}).get("Source") or (d["info"].get("project_urls") or {}).get("Homepage") or d["info"].get("home_page")
            elif key.startswith(("cargo:", "embedded:")):
                n = name
                h = next((x["content"] for x in c.get("hashes", []) if x["alg"] == "SHA-256"), None)
                ent["source"] = {"type": "crate", "url": f"https://crates.io/api/v1/crates/{n}/{ver}/download", "sha256": h,
                                 "note": "" if h else "no checksum in the SBOM source (embedded upstream SBOM without hashes): record from crates.io"}
            elif key.startswith("npm:"):
                if not ver:
                    ent["unresolved"] = "version unknown (banner only): the source package cannot be named"
                else:
                    d = get_json(f"https://registry.npmjs.org/{name}/{ver}")
                    ent["source"] = {"type": "npm-tarball", "url": d["dist"]["tarball"], "integrity": d["dist"].get("integrity"), "sha1": d["dist"].get("shasum")}
            elif key == "cpython":
                url = f"https://www.python.org/ftp/python/{ver}/Python-{ver}.tgz"
                ent["source"] = {"type": "release-tarball", "url": url, "sha256": None if a.no_download else sha256_url(url)}
            elif key.startswith("openssl:"):
                url = f"https://github.com/openssl/openssl/releases/download/openssl-{ver}/openssl-{ver}.tar.gz"
                h = next((x["content"] for x in c.get("hashes", []) if x["alg"] == "SHA-256"), None)
                ent["source"] = {"type": "release-tarball", "url": url, "sha256": h or (None if a.no_download else sha256_url(url))}
            elif key.startswith("ms-") or "ms-dll" in key or key == "ms-apiset":
                ent["source"] = {"type": "none", "note": "Microsoft redistributable runtime: binary-only; no source is published by this project (see decision register)"}
            elif key.startswith("libffi"):
                ent["unresolved"] = "libffi version not discoverable from the shipped library; source cannot be named"
            else:
                continue
        except (urllib.error.URLError, KeyError, ValueError, StopIteration) as e:
            ent["unresolved"] = f"lookup failed: {e}"
        if "source" not in ent and "unresolved" not in ent:
            ent["unresolved"] = "no rule produced a source for this component"
        out[ident] = ent
        print(f"  {ident}: {'unresolved: ' + ent['unresolved'] if 'unresolved' in ent else ent['source']['type']}")
    Path(a.out).write_text(json.dumps({"_comment": "Generator input for packaging/sbom/generate_source_offer.py; collected by packaging/tools/collect_source_refs.py. Entries marked unresolved are NOT sources.",
                                       "components": dict(sorted(out.items()))}, indent=1) + "\n", encoding="utf-8")
    print(f"{len(out)} entries -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
