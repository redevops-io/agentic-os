#!/usr/bin/env python3
"""Fetch the reference licence texts that `texts/` holds beyond the first set, and record each in `texts/MANIFEST.json`.

  fetch_reference_texts.py            download every entry below, write texts/<name>, update MANIFEST.json
  fetch_reference_texts.py --verify   offline: check every file named in MANIFEST.json against its recorded sha256

Each entry names the exact upstream tag or commit, so that the text is the one of the packaged (or declared) version
wherever such a version exists; where the text comes from a LATER commit than the packaged one (objc2) the description
says so. Presence of a text here is not a clearance. Standard library only. Network: raw.githubusercontent.com.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXTS = HERE / "texts"
UA = {"User-Agent": "redevops-notices/1 (licence texts for a source offer; contact: redevops.io)"}
RAW = "https://raw.githubusercontent.com"
OBJC2 = "8a5abacd90d5403fc0463459a35826f892b9142e"      # madsmtm/objc2 HEAD fetched 2026-11-04; first has the three texts
DUCK = "v1.5.5"                                          # duckdb/duckdb tag (commit d8cdaa33fda8df955cc76ef58a280f68f4cd43fa)
DUCK_THIRD_PARTY = ("brotli", "concurrentqueue", "fast_float", "fastpforlib", "fmt", "fsst", "httplib", "hyperloglog",
                    "jaro_winkler", "jemalloc", "libpg_query", "lz4", "mbedtls", "miniz", "parquet", "pcg", "pdqsort", "re2",
                    "ska_sort", "skiplist", "snappy", "snowball", "tdigest", "utf8proc", "vergesort", "yyjson", "zstd",
                    "thrift/thrift")

ENTRIES: dict[str, tuple[str, str]] = {
    "objc2-LICENSE-MIT.txt": (
        f"{RAW}/madsmtm/objc2/{OBJC2}/LICENSE-MIT.txt",
        "MIT text of the madsmtm/objc2 repository (Copyright 2026 Mads Marquart) from upstream commit 8a5abacd, which is LATER "
        "than the commits the packaged crates were built from (those hold only the licence-selection statement LICENSE.md); "
        "for objc2, block2, objc2-foundation, objc2-encode. UNVERIFIED that the copyright line applies to the packaged versions"),
    "objc2-LICENSE-ZLIB.txt": (
        f"{RAW}/madsmtm/objc2/{OBJC2}/LICENSE-ZLIB.txt",
        "Zlib text of the madsmtm/objc2 repository, upstream commit 8a5abacd (later than the packaged crates); for the "
        "'Zlib OR Apache-2.0 OR MIT' crates"),
    "objc2-LICENSE-APACHE.txt": (
        f"{RAW}/madsmtm/objc2/{OBJC2}/LICENSE-APACHE.txt",
        "Apache-2.0 text of the madsmtm/objc2 repository, upstream commit 8a5abacd (later than the packaged crates); for the "
        "'Zlib OR Apache-2.0 OR MIT' crates"),
    "libyaml-0.2.5-License.txt": (
        f"{RAW}/yaml/libyaml/0.2.5/License",
        "libyaml 0.2.5 License (MIT). PyYAML 6.0.3's CI builds its wheels against libyaml ref 0.2.5 "
        "(.github/workflows/ci.yaml LIBYAML_REF default); the version linked in the shipped _yaml extension was not read from "
        "the binary"),
    "libffi-3.4.4-LICENSE.txt": (
        f"{RAW}/libffi/libffi/v3.4.4/LICENSE",
        "libffi v3.4.4 LICENSE (MIT); byte-identical to python/cpython-source-deps tag libffi-3.4.4. CPython v3.12.10 "
        "PCbuild/get_externals.bat names libffi-3.4.4 as the Windows libffi; the version of libffi-8.dll actually shipped was "
        "not read from the file"),
    "NSIS-trunk-COPYING.txt": (
        "https://sourceforge.net/p/nsis/code/HEAD/tree/NSIS/trunk/COPYING?format=raw",
        "NSIS COPYING (HEAD of the NSIS trunk on 2026-11-04: zlib/libpng for the stub and plug-ins, bzip2 licence, Common Public "
        "License 1.0 for the LZMA module). tauri-bundler 2.10.4 downloads NSIS 3.13 and defaults to LZMA compression; the NSIS "
        "version and compression used for any installer are UNVERIFIED (no Windows build exists)"),
    "WiX3-LICENSE.txt": (
        f"{RAW}/wixtoolset/wix3/wix3141rtm/LICENSE.TXT",
        "WiX Toolset v3 LICENSE.TXT (Microsoft Reciprocal License). tauri-bundler 2.10.4 downloads wix314-binaries.zip "
        "(wix3141rtm); which WiX-supplied files, if any, end up inside an MSI is UNVERIFIED (no Windows build exists)"),
    "duckdb-1.5.5-LICENSE.txt": (
        f"{RAW}/duckdb/duckdb/{DUCK}/LICENSE", "DuckDB LICENSE (MIT) at tag v1.5.5"),
}
for _d in DUCK_THIRD_PARTY:
    _n = _d.replace("/", "-")
    ENTRIES[f"duckdb-1.5.5-third_party-{_n}-LICENSE.txt"] = (
        f"{RAW}/duckdb/duckdb/{DUCK}/third_party/{_d}/LICENSE",
        f"DuckDB source tree tag v1.5.5, third_party/{_d}/LICENSE. The wheel statically embeds third-party libraries; which of "
        "this tree's libraries are linked into the shipped _duckdb extension was NOT established")
ENTRIES["duckdb-1.5.5-third_party-tdigest-NOTICES.txt"] = (
    f"{RAW}/duckdb/duckdb/{DUCK}/third_party/tdigest/NOTICES",
    "DuckDB source tree tag v1.5.5, third_party/tdigest/NOTICES (as above: linkage not established)")


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def get(url: str, tries: int = 6) -> bytes:
    import time
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except (OSError, TimeoutError):          # includes URLError / HTTPError; a 404 is retried too, then raised
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def verify() -> int:
    man = json.loads((TEXTS / "MANIFEST.json").read_text(encoding="utf-8"))["files"]
    bad = 0
    for name, meta in sorted(man.items()):
        p = TEXTS / name
        if not p.is_file():
            print(f"MISSING {name}", file=sys.stderr); bad += 1
        elif sha256(p.read_bytes()) != meta["sha256"]:
            print(f"HASH MISMATCH {name}", file=sys.stderr); bad += 1
    print(f"{len(man)} manifest entries checked, {bad} problem(s)")
    return 1 if bad else 0


def fetch() -> int:
    mpath = TEXTS / "MANIFEST.json"
    man = json.loads(mpath.read_text(encoding="utf-8"))
    for name, (url, desc) in ENTRIES.items():
        data = get(url)
        if not data.strip():
            print(f"EMPTY {url}", file=sys.stderr)
            return 1
        (TEXTS / name).write_bytes(data)
        man["files"][name] = {"source": url, "description": desc, "sha256": sha256(data), "bytes": len(data)}
        print(f"{name}  {len(data)} bytes  {sha256(data)[:16]}")
    man["files"] = dict(sorted(man["files"].items()))
    mpath.write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args(argv)
    return verify() if a.verify else fetch()


if __name__ == "__main__":
    sys.exit(main())
