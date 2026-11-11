"""The reproducibility analyzer classifies differences by reading bytes: it reports identical only for identical bytes, and separates timestamp-only differences from content differences."""
from __future__ import annotations

import gzip
import io
import struct
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release" / "native"))
import analyze_repro as ar  # noqa: E402


def tgz(files: dict, mtime: int, name_suffix=".gz") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as t:
        for n, d in files.items():
            ti = tarfile.TarInfo(n)
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid = len(d), mtime, 0o644, 0, 0
            t.addfile(ti, io.BytesIO(d))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as g:
        g.write(raw.getvalue())
    return out.getvalue()


def deb(files: dict, mtime: int, ar_mtime="0") -> bytes:
    def member(name, body):
        h = f"{name + '/':<16}{ar_mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(body):<10}`\n".encode()
        return h + body + (b"\n" if len(body) & 1 else b"")
    return b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("control.tar.gz", tgz({"control": b"Package: x\n"}, mtime)) + member("data.tar.gz", tgz(files, mtime))


def test_identical_bytes_are_identical_and_nothing_else_is(tmp_path):
    a = deb({"usr/bin/x": b"code"}, 1)
    assert ar.classify("p.deb", a, a)["verdict"] == "identical"


def test_a_deb_that_differs_only_in_tar_mtimes_is_a_timestamp_difference():
    r = ar.classify("p.deb", deb({"usr/bin/x": b"code", "usr/bin/y": b"more"}, 1), deb({"usr/bin/x": b"code", "usr/bin/y": b"more"}, 2))
    assert r["verdict"] == "content, modes and ownership identical; only timestamps differ", r
    assert r["members"]["data.tar.gz"]["content_mode_owner_identical"] is True and "mtime" in r["members"]["data.tar.gz"]["tar_fields_that_differ"]


def test_a_deb_whose_content_differs_is_reported_as_a_content_difference():
    r = ar.classify("p.deb", deb({"usr/bin/x": b"code"}, 1), deb({"usr/bin/x": b"CODE"}, 1))
    assert r["verdict"] == "CONTENT DIFFERS" and "sha256" in r["members"]["data.tar.gz"]["tar_fields_that_differ"]


def test_a_deb_whose_entry_set_differs_is_a_content_difference():
    r = ar.classify("p.deb", deb({"usr/bin/x": b"code"}, 1), deb({"usr/bin/x": b"code", "usr/bin/extra": b"e"}, 1))
    assert r["verdict"] == "CONTENT DIFFERS"


def appimage(mkfs: int, body: bytes, runtime=b"\x7fELF" + b"R" * 200) -> bytes:
    sb = bytearray(96)
    sb[0:4] = b"hsqs"
    struct.pack_into("<I", sb, 4, 10)
    struct.pack_into("<I", sb, 8, mkfs)
    struct.pack_into("<HH", sb, 28, 4, 0)
    struct.pack_into("<Q", sb, 40, len(body))
    return runtime + bytes(sb) + body


def test_an_appimage_with_an_identical_runtime_and_a_different_image_is_located():
    r = ar.classify("a.AppImage", appimage(100, b"X" * 500), appimage(200, b"X" * 500))
    assert r["kind"] == "appimage" and "mkfs_time" in r["superblock_fields_that_differ"]
    assert r["runtime_prefix"]["differing_bytes"] == 0 and r["squashfs_image"]["differing_bytes"] > 0 and r["verdict"].startswith("differs in the squashfs image only")
    r2 = ar.classify("a.AppImage", appimage(100, b"X" * 500), appimage(100, b"X" * 500, runtime=b"\x7fELF" + b"S" * 200))
    assert r2["verdict"] == "runtime prefix differs"


def pe(stamp: int, checksum: int = 0, tail=b"T" * 64, guid=b"G" * 16) -> bytes:
    d = bytearray(0x400)
    d[0:2] = b"MZ"
    struct.pack_into("<I", d, 0x3C, 0x80)
    d[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", d, 0x84, 0x8664, 1, stamp, 0, 0, 0xF0, 0x22)   # machine, sections, TimeDateStamp, symtab, nsyms, optional header size, characteristics
    opt = 0x80 + 24
    struct.pack_into("<H", d, opt, 0x20B)
    struct.pack_into("<I", d, opt + 64, checksum)
    dd = opt + 112
    struct.pack_into("<II", d, dd + 6 * 8, 0x1000, 28)                       # debug directory at RVA 0x1000
    sec = opt + 0xF0
    d[sec:sec + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", d, sec + 8, 0x200, 0x1000, 0x200, 0x200)       # vsize, va, rawsize, rawptr
    e = 0x200
    struct.pack_into("<IIHHIIII", d, e, 0, stamp, 0, 0, 2, 40, 0x1000, 0x240)  # debug entry: type 2 (CodeView), size, rva, ptr 0x240
    d[0x240:0x244] = b"RSDS"
    d[0x244:0x254] = guid
    d[0x300:0x300 + len(tail)] = tail
    return bytes(d)


def test_a_pe_that_differs_only_in_timestamp_checksum_and_debug_guid_is_said_so():
    r = ar.classify("a.exe", pe(1, 11, guid=b"A" * 16), pe(2, 22, guid=b"B" * 16))
    assert r["verdict"] == "differs only in PE timestamp/checksum/debug-GUID fields", r
    assert {"TimeDateStamp", "CheckSum"} <= set(r["header_fields_that_differ"])
    r2 = ar.classify("a.exe", pe(1, tail=b"ONE" + b"T" * 61), pe(1, tail=b"TWO" + b"T" * 61))
    assert r2["verdict"].startswith("differs beyond the PE timestamp fields")


def test_a_parser_failure_is_reported_not_hidden():
    r = ar.classify("broken.deb", b"not an ar archive", b"also not")
    assert r["kind"] == "error" and "differs" in r["verdict"]


def test_analyze_pairs_lists_identical_different_and_one_sided_files(tmp_path):
    for n, data in (("b1", b"AAA"), ("b2", b"AAA")):
        (tmp_path / n).mkdir()
        (tmp_path / n / "same.bin").write_bytes(data)
    (tmp_path / "b1" / "diff.bin").write_bytes(b"1")
    (tmp_path / "b2" / "diff.bin").write_bytes(b"2")
    (tmp_path / "b1" / "only1.bin").write_bytes(b"x")
    (tmp_path / "b2" / "only2.bin").write_bytes(b"y")
    rep = ar.analyze(tmp_path, [("b1", "b2")])["pairs"]["b1:b2"]
    assert rep["identical"] == ["same.bin"] and set(rep["different"]) == {"diff.bin", "only1.bin", "only2.bin"}
    assert "Passing tests is not reproducibility" in ar.markdown({"pairs": {"b1:b2": rep}})
