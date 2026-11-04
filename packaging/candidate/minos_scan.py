"""List Mach-O minos of every binary inside a PyInstaller onefile sidecar (read-only: extracts to --out, a new empty dir)."""
import argparse, os, subprocess, sys, struct, hashlib
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
ap = argparse.ArgumentParser(); ap.add_argument("--sidecar"); ap.add_argument("--out")
a = ap.parse_args()
out = Path(a.out); out.mkdir(parents=True)
rd = CArchiveReader(a.sidecar)
rows = []
for name, (pos, dlen, ulen, flag, typ) in rd.toc.items():
    if typ not in ("b", "x", "d", "s", "z", "m", "M", "o", "O", "l", "L") and False:
        pass
    if typ == "b":
        data = rd.extract(name)
        if isinstance(data, tuple): data = data[-1]
        p = out / name.replace("/", "__")
        p.write_bytes(data)
        v = subprocess.run(["vtool", "-show-build", str(p)], capture_output=True, text=True).stdout
        minos = [l.split()[-1] for l in v.splitlines() if "minos" in l]
        arch = subprocess.run(["lipo", "-archs", str(p)], capture_output=True, text=True).stdout.strip() or "?"
        rows.append((name, arch, ",".join(sorted(set(minos))) or "-", len(data)))
for r in sorted(rows): print("\t".join(map(str, r)))
vals = []
for r in rows:
    for m in r[2].split(","):
        if m not in ("-", ""): vals.append(tuple(int(x) for x in m.split(".")))
print("MAX minos:", ".".join(map(str, max(vals))) if vals else "n/a", " binaries:", len(rows), " archs:", sorted({r[1] for r in rows}), file=sys.stderr)
