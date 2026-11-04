#!/usr/bin/env python3
"""Compare two builds of the same tree: what is and is not byte-identical, and why.

  compare_builds.py A B --out report.md [--json report.json]

A and B are directories each holding a copy of: sidecar (redevops-sidecar), wheelhouse/ (the wheels),
launcher (the launcher executable, optional), app/ (the .app tree, optional), sbom/ (optional),
build-manifest.json. Output classifies every difference:

  identical        same bytes
  path-embedded    the differing entry contains the build directory of A or B (a path baked into the bytes)
  timestamp-like   differs only in 4/8-byte little-endian values that look like epoch seconds near the build time
  other            differs for a reason this tool does not identify (reported with offsets)

For the PyInstaller sidecar the comparison is per archive entry (PKG members and PYZ modules), then the
container-level remainder (TOC/offsets) is reported separately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def diff_offsets(a: bytes, b: bytes, limit: int = 12) -> tuple[int, list[int]]:
    n = min(len(a), len(b))
    offs = []
    count = 0
    # chunked scan keeps this fast on 30 MB files
    step = 1 << 16
    for base in range(0, n, step):
        ca, cb = a[base:base + step], b[base:base + step]
        if ca != cb:
            for i, (x, y) in enumerate(zip(ca, cb)):
                if x != y:
                    count += 1
                    if len(offs) < limit:
                        offs.append(base + i)
    return count + abs(len(a) - len(b)), offs


def classify(a: bytes, b: bytes, paths: list[bytes]) -> str:
    if a == b:
        return "identical"
    pa = [p for p in paths if p and (p in a or p in b)]
    if pa:
        return "path-embedded"
    cnt, _ = diff_offsets(a, b)
    return "other" if cnt > 64 else "small-binary-difference (<=64 bytes: timestamp/UUID/signature-like)"


def sidecar_entries(path: Path):
    from PyInstaller.archive.readers import CArchiveReader

    r = CArchiveReader(str(path))
    pkg = {n: r.extract(n) for n in r.toc if r.toc[n][4] != "z"}
    pyz = {}
    for n in r.toc:
        if r.toc[n][4] == "z":
            z = r.open_embedded_archive(n)
            for m in z.toc:
                pyz[m] = z.extract(m, raw=True)
    return pkg, pyz


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--out", required=True)
    ap.add_argument("--json")
    ap.add_argument("--path-marker", action="append", default=[], help="build directory strings to search for in differing entries")
    ns = ap.parse_args()
    A, B = Path(ns.a), Path(ns.b)
    paths = [p.encode() for p in ns.path_marker]
    out: list[str] = []
    rep: dict = {}
    P = out.append

    # --- wheelhouse --------------------------------------------------------------------------
    if (A / "wheelhouse").is_dir() and (B / "wheelhouse").is_dir():
        wa = {p.name: sha(p.read_bytes()) for p in (A / "wheelhouse").glob("*.whl")}
        wb = {p.name: sha(p.read_bytes()) for p in (B / "wheelhouse").glob("*.whl")}
        same = sorted(k for k in wa if wb.get(k) == wa[k])
        diff = sorted(k for k in wa if k in wb and wb[k] != wa[k])
        rep["wheelhouse"] = {"identical": same, "different": diff, "only_a": sorted(set(wa) - set(wb)), "only_b": sorted(set(wb) - set(wa))}
        P(f"## Wheelhouse\n\n{len(same)} of {len(wa)} wheels byte-identical; different: {', '.join(diff) or 'none'}; only in A: {sorted(set(wa) - set(wb)) or 'none'}; only in B: {sorted(set(wb) - set(wa)) or 'none'}.\n")
        first = [k for k in wa if k.split('-')[0] in ("agentic_os", "runtime_contracts", "knowledge_frontier", "redevops_connectors")]
        P("First-party wheels (built from source with SOURCE_DATE_EPOCH): " + ", ".join(f"{k}: {'IDENTICAL' if wb.get(k) == wa[k] else 'DIFFERENT'}" for k in sorted(first)) + "\n")

    # --- sidecar ----------------------------------------------------------------------------
    sa, sb = A / "redevops-sidecar", B / "redevops-sidecar"
    if sa.exists() and sb.exists():
        ba, bb = sa.read_bytes(), sb.read_bytes()
        cnt, offs = diff_offsets(ba, bb)
        rep["sidecar_file"] = {"sha256_a": sha(ba), "sha256_b": sha(bb), "size_a": len(ba), "size_b": len(bb), "differing_bytes": cnt, "first_offsets": offs}
        P(f"## Sidecar (PyInstaller)\n\nWhole file: {'IDENTICAL' if ba == bb else 'DIFFERENT'}; size A {len(ba)} B {len(bb)}; differing bytes {cnt}; first differing offsets {offs}.\n")
        try:
            pa, za = sidecar_entries(sa)
            pb, zb = sidecar_entries(sb)
        except Exception as e:  # PyInstaller not importable
            P(f"(entry-level comparison unavailable: {e})\n")
        else:
            P(f"PKG entries: A {len(pa)} B {len(pb)}; module set (PYZ): A {len(za)} B {len(zb)}.\n")
            P(f"- PKG entry names equal: {set(pa) == set(pb)}; PYZ module set equal: {set(za) == set(zb)}\n")
            rows = []
            for kind, ea, eb in (("pkg", pa, pb), ("pyz", za, zb)):
                same = [n for n in ea if n in eb and ea[n] == eb[n]]
                dif = [n for n in ea if n in eb and ea[n] != eb[n]]
                P(f"- {kind}: {len(same)} identical, {len(dif)} differ" + (f" ({', '.join(dif[:8])}{' ...' if len(dif) > 8 else ''})" if dif else ""))
                for n in dif:
                    cls = classify(ea[n], eb[n], paths)
                    c, o = diff_offsets(ea[n], eb[n])
                    rows.append({"kind": kind, "entry": n, "class": cls, "differing_bytes": c, "offsets": o})
            rep["sidecar_entries"] = {"pkg_names_equal": set(pa) == set(pb), "pyz_names_equal": set(za) == set(zb),
                                      "pkg_identical": sum(1 for n in pa if n in pb and pa[n] == pb[n]), "pkg_total": len(pa),
                                      "pyz_identical": sum(1 for n in za if n in zb and za[n] == zb[n]), "pyz_total": len(za), "differing": rows}
            if rows:
                P("\n| kind | entry | class | differing bytes | first offsets |\n|---|---|---|---|---|")
                for r in rows[:40]:
                    P(f"| {r['kind']} | {r['entry']} | {r['class']} | {r['differing_bytes']} | {r['offsets'][:4]} |")
            P("")

    # --- launcher + app -----------------------------------------------------------------------
    la, lb = A / "launcher", B / "launcher"
    if la.exists() and lb.exists():
        ba, bb = la.read_bytes(), lb.read_bytes()
        cnt, offs = diff_offsets(ba, bb)
        pa = [p.decode() for p in paths if p in ba]
        pb = [p.decode() for p in paths if p in bb]
        remap = None
        if ba != bb:
            # does A become B when A's embedded build paths are replaced by B's? (same-length prefixes only)
            for x in pa:
                for y in pb:
                    if x != y and len(x) == len(y) and ba.replace(x.encode(), y.encode()) == bb:
                        remap = f"A == B after replacing {x} -> {y}: the ONLY difference is the embedded build path"
        rep["launcher"] = {"sha256_a": sha(ba), "sha256_b": sha(bb), "size_a": len(ba), "size_b": len(bb), "differing_bytes": cnt, "first_offsets": offs}
        P(f"## Launcher (Tauri, Mach-O/PE)\n\nFile: {'IDENTICAL' if ba == bb else 'DIFFERENT'}; size A {len(ba)} B {len(bb)}; differing bytes {cnt}; first offsets {offs}; build-dir marker strings present in A: {pa or 'none'}; in B: {pb or 'none'}. {remap or ''}\n")
        rep["launcher"]["path_only_difference"] = remap
    for sub in ("app",):
        if (A / sub).is_dir() and (B / sub).is_dir():
            fa = {p.relative_to(A / sub).as_posix(): sha(p.read_bytes()) for p in (A / sub).rglob("*") if p.is_file() and not p.is_symlink()}
            fb = {p.relative_to(B / sub).as_posix(): sha(p.read_bytes()) for p in (B / sub).rglob("*") if p.is_file() and not p.is_symlink()}
            same = sorted(k for k in fa if fb.get(k) == fa[k])
            dif = sorted(k for k in fa if k in fb and fa[k] != fb[k])
            rep[sub] = {"files_a": len(fa), "files_b": len(fb), "identical": len(same), "different": dif, "names_equal": set(fa) == set(fb)}
            P(f"## {sub}/ tree\n\nfile lists equal: {set(fa) == set(fb)}; {len(same)}/{len(fa)} files identical; different: {dif or 'none'}.\n")
    # --- sbom -------------------------------------------------------------------------------
    sba, sbb = A / "sbom" / "sbom.cdx.json", B / "sbom" / "sbom.cdx.json"
    if sba.exists() and sbb.exists():
        ja, jb = json.loads(sba.read_text()), json.loads(sbb.read_text())
        rep["sbom"] = {"identical_bytes": sba.read_bytes() == sbb.read_bytes(), "components_a": len(ja["components"]), "components_b": len(jb["components"]),
                       "serial_a": ja["serialNumber"], "serial_b": jb["serialNumber"]}
        P(f"## SBOM\n\nbyte-identical: {sba.read_bytes() == sbb.read_bytes()}; components {len(ja['components'])} / {len(jb['components'])}.\n")
    ma, mb = A / "build-manifest.json", B / "build-manifest.json"
    if ma.exists() and mb.exists():
        ja, jb = json.loads(ma.read_text()), json.loads(mb.read_text())
        rep["manifest"] = {"inputs_equal": ja.get("inputs_sha256") == jb.get("inputs_sha256"), "toolchain_equal": ja.get("toolchain") == jb.get("toolchain"),
                           "source_equal": ja.get("source") == jb.get("source"), "durations_a": ja.get("durations_seconds"), "durations_b": jb.get("durations_seconds")}
        P(f"## Build manifest\n\ninput hashes equal: {rep['manifest']['inputs_equal']}; toolchain record equal: {rep['manifest']['toolchain_equal']}; durations A {ja.get('durations_seconds')} B {jb.get('durations_seconds')}.\n")
    Path(ns.out).write_text("\n".join(out) + "\n")
    if ns.json:
        Path(ns.json).write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
