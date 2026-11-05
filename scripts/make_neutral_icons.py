#!/usr/bin/env python3
"""Generate the six launcher icon assets as TEMPORARY neutral artwork, drawn only from code.

The earlier icons carried a letter mark; its authorship and rights are an open owner decision (D-LIC-11), and
the owner decided not to retain the mark while that decision is unresolved. These files replace it with a plain
geometric glyph: a rounded square with a slate gradient, one pale rounded diamond and one round cut-out. They
contain no letters, no monogram, no text, no fonts, no third-party content and no model output, and they imitate
no brand. They are TEMPORARY placeholders, not a brand decision and not a legal statement.

    python scripts/make_neutral_icons.py [output_dir]      (default: deploy/launcher/src-tauri/icons)

Python standard library only (zlib, struct, math): no Pillow, so it runs wherever the repository's tests run.

Reproducibility: the drawing is plain IEEE-754 arithmetic and the encoders write no metadata (no timestamp, no
text chunks, no software name, no colour-profile chunk). The only toolchain-dependent part is the deflate stream
produced by the platform's zlib at level 9. The SHA-256 values recorded in deploy/launcher/src-tauri/icons/README.md
were produced with CPython 3.12.10 on macOS arm64 (zlib as linked by that interpreter) and checked identical on a
second run; tests/test_launcher_icons.py regenerates the files and compares them byte for byte.

Outputs (names and sizes are what tauri.conf.json `bundle.icon` and the Tauri bundler expect):
    32x32.png (32), 128x128.png (128), 128x128@2x.png (256), icon.png (512): PNG, 8-bit RGBA
    icon.ico: 16, 24, 32, 48, 64, 128, 256 (every image a PNG)
    icon.icns: icp4 16, icp5 32, ic11 32 (16@2x), ic12 64 (32@2x), ic07 128, ic13 256 (128@2x), ic08 256,
               ic14 512 (256@2x), ic09 512, ic10 1024 (512@2x)
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SLATE_TOP = (98, 111, 130)
SLATE_BOTTOM = (43, 52, 67)
PALE = (232, 237, 243)
DARK = (50, 60, 76)

# the glyph, in units of the icon width (the icon is 1.0 x 1.0)
BODY_INSET, BODY_RADIUS = 0.04, 0.21
DIAMOND_HALF, DIAMOND_RADIUS = 0.205, 0.065
HOLE_RADIUS = 0.085
SQRT1_2 = math.sqrt(0.5)


def _rrect(px: float, py: float, hx: float, hy: float, r: float) -> float:
    """Signed distance to a rounded rectangle centred at the origin (negative inside)."""
    qx, qy = abs(px) - (hx - r), abs(py) - (hy - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _cov(d: float, n: int) -> float:
    """Anti-aliased coverage of a shape from its signed distance (units of the icon width) at n pixels."""
    c = 0.5 - d * n
    return 0.0 if c <= 0.0 else 1.0 if c >= 1.0 else c


def render(n: int) -> bytes:
    """n x n RGBA, row-major, straight (non-premultiplied) alpha; fully transparent pixels are 0,0,0,0."""
    half_body = 0.5 - BODY_INSET
    out = bytearray(n * n * 4)
    i = 0
    for y in range(n):
        v = (y + 0.5) / n
        t = (v - BODY_INSET) / (1.0 - 2 * BODY_INSET)
        t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        bg = tuple(SLATE_TOP[k] + (SLATE_BOTTOM[k] - SLATE_TOP[k]) * t for k in range(3))
        py = v - 0.5
        for x in range(n):
            u = (x + 0.5) / n
            px = u - 0.5
            a = _cov(_rrect(px, py, half_body, half_body, BODY_RADIUS), n)
            if a > 0.0:
                rx, ry = (px + py) * SQRT1_2, (py - px) * SQRT1_2        # rotate 45 degrees: the diamond
                s = _cov(_rrect(rx, ry, DIAMOND_HALF, DIAMOND_HALF, DIAMOND_RADIUS), n)
                h = _cov(math.hypot(px, py) - HOLE_RADIUS, n)             # coverage of the round cut-out
                r = [bg[k] + (PALE[k] - bg[k]) * s for k in range(3)]
                hole = s * h                                               # the cut-out shows the dark tone
                r = [r[k] + (DARK[k] - r[k]) * hole for k in range(3)]
                out[i] = int(r[0] + 0.5)
                out[i + 1] = int(r[1] + 0.5)
                out[i + 2] = int(r[2] + 0.5)
                out[i + 3] = int(a * 255.0 + 0.5)
            i += 4
    return bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png(n: int, rgba: bytes) -> bytes:
    """A minimal PNG: signature, IHDR, one IDAT, IEND. Up filter on every row. No ancillary chunks."""
    stride = n * 4
    raw = bytearray()
    prev = bytes(stride)
    for y in range(n):
        row = rgba[y * stride:(y + 1) * stride]
        raw.append(2)                                                       # filter type 2 = Up
        raw += bytes((row[k] - prev[k]) & 0xFF for k in range(stride))
        prev = row
    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)                     # 8-bit, RGBA, no interlace
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """ICO container with PNG-encoded images (Windows Vista and later). Width/height byte 0 means 256."""
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for n, data in images:
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return head + entries + blobs


ICNS_TYPES = [("icp4", 16), ("icp5", 32), ("ic11", 32), ("ic12", 64), ("ic07", 128),
              ("ic13", 256), ("ic08", 256), ("ic14", 512), ("ic09", 512), ("ic10", 1024)]


def icns(pngs: dict[int, bytes]) -> bytes:
    """ICNS container: PNG payloads under the standard type codes (no table of contents, no metadata)."""
    body = b"".join(struct.pack(">4sI", t.encode(), 8 + len(pngs[n])) + pngs[n] for t, n in ICNS_TYPES)
    return struct.pack(">4sI", b"icns", 8 + len(body)) + body


SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build() -> dict[str, bytes]:
    pngs = {n: png(n, render(n)) for n in SIZES}
    return {
        "32x32.png": pngs[32],
        "128x128.png": pngs[128],
        "128x128@2x.png": pngs[256],
        "icon.png": pngs[512],
        "icon.ico": ico([(n, pngs[n]) for n in ICO_SIZES]),
        "icon.icns": icns(pngs),
    }


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (out / name).write_bytes(data)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else
         Path(__file__).resolve().parents[1] / "deploy" / "launcher" / "src-tauri" / "icons")
