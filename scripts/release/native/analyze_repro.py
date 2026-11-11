#!/usr/bin/env python3
"""Reproducibility analysis of collected native builds: for every pair of builds, which files are byte-identical, and for those that are not, WHERE and WHY they differ.

  analyze_repro.py --root DIR --pair NAME_A:NAME_B [--pair ...] --platform linux|windows --out REPORT_PREFIX

DIR holds one folder per build (`b1`, `b2`, ...; Windows: the `vmout/out/<label>` folders). The report (`.json` and `.md`) states, per pair, the verdict for each file and, for a differing file, what the
difference is made of. It never says a build "is reproducible" because it passed tests: a pair is reported as identical only when the bytes are identical.

Classification of a differing file (all by reading bytes; nothing is executed):
  * .deb   : ar members (name, size, ar mtime); for tar members (control/data .tar.gz/.xz) the gzip/xz-independent tar entries (name, mode, uid, gid, size, link, content sha256) and their mtimes;
             verdict `content-identical, metadata differs` when every entry's content, mode and ownership agree and only mtimes (tar or ar header) differ.
  * .AppImage : the ELF runtime prefix and the squashfs image: the offset of the squashfs magic, the superblock fields (mkfs time, inode count, bytes used), the number and span of differing byte
             runs in the runtime prefix and in the squashfs; a differing mkfs time is reported as a timestamp difference.
  * .msi   : compound document: size, and the byte-run statistics; .exe (PE): the PE header fields TimeDateStamp, the debug-directory/PDB GUID bytes and the checksum are located and compared, and the
             bytes that differ outside those fields are counted (a PE whose only differences are the header timestamp, checksum and debug GUID is reported as such).
  * anything else: size and byte-run statistics.
Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import gzip
import hashlib
import io
import json
import lzma
import struct
import tarfile
from pathlib import Path


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def runs(a: bytes, b: bytes, limit: int = 400) -> dict:
    """Differing byte runs of two byte strings (positions up to the shorter length) with a cap on the listing."""
    n = min(len(a), len(b))
    out, i, total = [], 0, 0
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            total += j - i
            if len(out) < limit:
                out.append([i, j - i])
            i = j
        else:
            i += 1
    return {"len_a": len(a), "len_b": len(b), "same_length": len(a) == len(b), "differing_bytes": total + abs(len(a) - len(b)), "run_count": len(out) if len(out) < limit else f">={limit}", "runs": out[:40]}


def read_tar(data: bytes, name: str):
    if name.endswith(".gz"):
        raw = gzip.decompress(data)
        hdr = {"gzip_mtime": struct.unpack("<I", data[4:8])[0], "gzip_os": data[9]}
    elif name.endswith(".xz"):
        raw, hdr = lzma.decompress(data), {}
    elif name.endswith(".tar"):
        raw, hdr = data, {}
    else:
        return None, {"unsupported_compression": name}
    ents = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as t:
        for m in t.getmembers():
            content = sha(t.extractfile(m).read()) if m.isfile() else None
            ents[m.name] = {"type": m.type.decode(), "mode": m.mode, "uid": m.uid, "gid": m.gid, "uname": m.uname, "gname": m.gname, "size": m.size, "linkname": m.linkname, "mtime": m.mtime, "sha256": content}
    return ents, hdr


def read_ar(data: bytes):
    if data[:8] != b"!<arch>\n":
        raise ValueError("not an ar archive")
    i, members = 8, []
    while i + 60 <= len(data):
        h = data[i:i + 60]
        name, mtime, uid, gid, mode, size = h[:16].decode().strip().rstrip("/"), h[16:28].decode().strip(), h[28:34].decode().strip(), h[34:40].decode().strip(), h[40:48].decode().strip(), int(h[48:58].decode().strip())
        body = data[i + 60:i + 60 + size]
        members.append({"name": name, "ar_mtime": mtime, "uid": uid, "gid": gid, "mode": mode, "size": size, "body": body})
        i += 60 + size + (size & 1)
    return members


def cmp_deb(a: bytes, b: bytes) -> dict:
    ma, mb = {m["name"]: m for m in read_ar(a)}, {m["name"]: m for m in read_ar(b)}
    res: dict = {"kind": "deb", "ar_members": {"a": list(ma), "b": list(mb)}, "members": {}}
    content_ok, meta_only = True, False
    for n in sorted(set(ma) | set(mb)):
        if n not in ma or n not in mb:
            res["members"][n] = "only in one"
            content_ok = False
            continue
        x, y = ma[n], mb[n]
        r: dict = {"bytes_identical": x["body"] == y["body"], "ar_mtime": [x["ar_mtime"], y["ar_mtime"]], "size": [x["size"], y["size"]]}
        if ".tar" in n:
            ea, ha = read_tar(x["body"], n)
            eb, hb = read_tar(y["body"], n)
            if ea is not None and eb is not None:
                fields_differ = {}
                for e in sorted(set(ea) | set(eb)):
                    if e not in ea or e not in eb:
                        fields_differ.setdefault("entry set", []).append(e)
                        continue
                    for f in ea[e]:
                        if ea[e][f] != eb[e][f]:
                            fields_differ.setdefault(f, []).append(e)
                r["tar_entries"] = len(ea)
                r["gzip_header"] = {"a": ha, "b": hb}
                r["tar_fields_that_differ"] = {f: {"count": len(v), "examples": v[:3]} for f, v in fields_differ.items()}
                non_time = [f for f in fields_differ if f != "mtime"]
                r["content_mode_owner_identical"] = not non_time
                content_ok = content_ok and not non_time
                if fields_differ and not non_time:
                    meta_only = True
            else:
                r["note"] = "tar not readable: " + str(ha)
                content_ok = content_ok and x["body"] == y["body"]
        else:
            content_ok = content_ok and x["body"] == y["body"]
        res["members"][n] = r
    res["verdict"] = ("identical" if a == b else ("content, modes and ownership identical; only timestamps differ" if content_ok and meta_only else ("content identical; ar header / compression framing differs" if content_ok else "CONTENT DIFFERS")))
    return res


def squashfs_info(data: bytes):
    off = data.find(b"hsqs")
    while off != -1 and off < len(data) - 96:
        # squashfs 4.0 superblock: magic, inode_count, mkfs_time, block_size, fragments, compression, block_log, flags, no_ids, s_major(=4), s_minor(=0)
        major, minor = struct.unpack_from("<HH", data, off + 28)
        if (major, minor) == (4, 0):
            inode_count, mkfs_time = struct.unpack_from("<II", data, off + 4)[0], struct.unpack_from("<I", data, off + 8)[0]
            bytes_used = struct.unpack_from("<Q", data, off + 40)[0]
            return {"offset": off, "inode_count": inode_count, "mkfs_time": mkfs_time, "bytes_used": bytes_used}
        off = data.find(b"hsqs", off + 1)
    return None


def cmp_appimage(a: bytes, b: bytes) -> dict:
    sa, sb = squashfs_info(a), squashfs_info(b)
    res: dict = {"kind": "appimage", "squashfs": {"a": sa, "b": sb}}
    if not sa or not sb or sa["offset"] != sb["offset"]:
        res["verdict"] = "layout differs (squashfs offset)" if sa and sb else "squashfs not found"
        res["bytes"] = runs(a, b)
        return res
    off = sa["offset"]
    res["runtime_prefix"] = runs(a[:off], b[:off])
    sq = runs(a[off:], b[off:])
    res["squashfs_image"] = sq
    sup = {k: [sa[k], sb[k]] for k in ("inode_count", "mkfs_time", "bytes_used") if sa[k] != sb[k]}
    res["superblock_fields_that_differ"] = sup
    res["verdict"] = ("identical" if a == b else
                      ("differs in the squashfs image only (runtime prefix identical); superblock mkfs_time " + ("differs" if "mkfs_time" in sup else "equal") +
                       "; file contents inside are compared by the in-VM tree comparison (see compare-*.out)") if not res["runtime_prefix"]["differing_bytes"] else "runtime prefix differs")
    return res


PE_NOTE = "PE: located fields = TimeDateStamp, CheckSum, debug directory timestamps and GUID/age of the CodeView record; remaining differing bytes are reported separately"


def pe_fields(d: bytes):
    if d[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        return None
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    ranges = [("TimeDateStamp", pe + 8, 4), ("CheckSum", opt + 64, 4)]
    dd = opt + (112 if magic == 0x20B else 96)
    # data directory 6 = debug
    dbg_rva, dbg_size = struct.unpack_from("<II", d, dd + 6 * 8)
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    secoff = opt + struct.unpack_from("<H", d, pe + 20)[0]

    def rva2off(rva):
        for i in range(nsec):
            s = secoff + 40 * i
            vsz, va, rsz, raw = struct.unpack_from("<IIII", d, s + 8)
            if va <= rva < va + max(vsz, rsz):
                return raw + (rva - va)
        return None
    if dbg_rva and dbg_size:
        o = rva2off(dbg_rva)
        for i in range(dbg_size // 28):
            e = o + 28 * i
            ranges.append((f"debug[{i}].TimeDateStamp", e + 4, 4))
            typ, size, ptr = struct.unpack_from("<I", d, e + 12)[0], struct.unpack_from("<I", d, e + 16)[0], struct.unpack_from("<I", d, e + 24)[0]
            if typ == 2 and d[ptr:ptr + 4] == b"RSDS":
                ranges.append((f"debug[{i}].CodeView GUID+age", ptr + 4, 20))
    return ranges


def cmp_pe(a: bytes, b: bytes) -> dict:
    fa, fb = pe_fields(a), pe_fields(b)
    res: dict = {"kind": "pe", "note": PE_NOTE, "bytes": runs(a, b)}
    if not fa or not fb or len(a) != len(b):
        res["verdict"] = "not comparable as PE" if not (fa and fb) else "size differs"
        return res
    masked_a, masked_b = bytearray(a), bytearray(b)
    named = {}
    for (n, o, l) in fa:
        if a[o:o + l] != b[o:o + l]:
            named[n] = [a[o:o + l].hex(), b[o:o + l].hex()]
        masked_a[o:o + l] = b"\0" * l
        masked_b[o:o + l] = b"\0" * l
    res["header_fields_that_differ"] = named
    res["bytes_after_masking_those_fields"] = runs(bytes(masked_a), bytes(masked_b))["differing_bytes"]
    res["verdict"] = "identical" if a == b else ("differs only in PE timestamp/checksum/debug-GUID fields" if res["bytes_after_masking_those_fields"] == 0 else f"differs beyond the PE timestamp fields ({res['bytes_after_masking_those_fields']} bytes)")
    return res


def classify(name: str, a: bytes, b: bytes) -> dict:
    if a == b:
        return {"verdict": "identical", "sha256": sha(a), "size": len(a)}
    low = name.lower()
    try:
        if low.endswith(".deb"):
            r = cmp_deb(a, b)
        elif low.endswith(".appimage"):
            r = cmp_appimage(a, b)
        elif low.endswith((".exe", ".dll")):
            r = cmp_pe(a, b)
        else:
            r = {"kind": "other", "bytes": runs(a, b), "verdict": "differs"}
    except Exception as e:  # a parser failure is reported, never hidden
        r = {"kind": "error", "error": f"{type(e).__name__}: {e}", "bytes": runs(a, b), "verdict": "differs (could not classify)"}
    r["sha256"] = {"a": sha(a), "b": sha(b)}
    r["size"] = {"a": len(a), "b": len(b)}
    return r


def analyze(root: Path, pairs: list[tuple[str, str]]) -> dict:
    report: dict = {"pairs": {}}
    for x, y in pairs:
        dx, dy = root / x, root / y
        files = sorted(p.relative_to(dx).as_posix() for p in dx.rglob("*") if p.is_file() and not p.is_symlink())
        res = {}
        for f in files:
            if f in ("SHA256SUMS.local", "build.log", "SHA256-SIZES.txt"):
                continue          # host-local bookkeeping that names its own build
            fy = dy / f
            if not fy.is_file():
                res[f] = {"verdict": "only in " + x}
                continue
            res[f] = classify(f, (dx / f).read_bytes(), fy.read_bytes())
        for p in dy.rglob("*"):
            rel = p.relative_to(dy).as_posix()
            if p.is_file() and rel not in files and rel not in ("SHA256SUMS.local", "build.log", "SHA256-SIZES.txt"):
                res[rel] = {"verdict": "only in " + y}
        report["pairs"][f"{x}:{y}"] = {"files": res, "identical": sorted(k for k, v in res.items() if v["verdict"] == "identical"), "different": sorted(k for k, v in res.items() if v["verdict"] != "identical")}
    return report


def markdown(rep: dict) -> str:
    L = ["# Reproducibility analysis (byte comparison; generated by scripts/release/native/analyze_repro.py)", "",
         "A pair is reported identical only when the bytes are identical. Passing tests is not reproducibility.", ""]
    for pair, v in rep["pairs"].items():
        L += [f"## {pair}", "", f"* identical: {len(v['identical'])} files; different: {len(v['different'])} files", ""]
        for f in v["different"]:
            r = v["files"][f]
            L.append(f"* `{f}`: **{r['verdict']}**")
        L.append("")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--pair", action="append", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    pairs = [tuple(p.split(":", 1)) for p in a.pair]
    rep = analyze(a.root, pairs)
    a.out.with_suffix(".json").write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n")
    a.out.with_suffix(".md").write_text(markdown(rep))
    print(markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
