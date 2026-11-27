#!/usr/bin/env python3
"""The rAIls mark: a square (Ulam) spiral of the integers, one continuous path, a marker on each prime.

    python brand/rails_mark.py [output_dir]          write every asset (default: brand/assets)
    python brand/rails_mark.py --sheet out.png       write the grid-size comparison sheet

Definition (the same for every variant):
  * 1 is at the centre. 2 is to its right (east). The spiral then turns anticlockwise as seen on screen: north, west,
    south, east, with run lengths 1, 1, 2, 2, 3, 3, ...
  * An n x n grid (n odd) holds the integers 1 ... n*n; the path visits their cell centres in order and ends at n*n.
  * A marker (a filled circle) sits on a cell exactly when its integer is prime.
  * The lettering of the lockups is drawn here as strokes: no font is used.

Standard library only. One scene description is written both as SVG and as anti-aliased PNG (distance fields, so line
joins and caps are round in both). No metadata, so output depends only on this file and the platform's zlib.
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

VERSION = "1"

CYAN = "#1896D6"      # the path
INK = "#0F1B33"       # markers and lettering on light
SNOW = "#EEF2FB"      # markers and lettering on dark
PAPER = "#FFFFFF"     # light tile
NIGHT = "#0B1222"     # dark tile

CELL = 10.0           # scene units per grid cell
#: variant -> (grid side, path width, marker radius), widths and radii in cells
PRIMARY = (11, 0.20, 0.36)
COMPACT = (7, 0.20, 0.36)
TINY = (5, 0.22, 0.37)          # 16 and 24 px only
FULL = (49, 0.14, 0.36)         # decorative, never an icon


# ── the mathematics ─────────────────────────────────────────────────────────────
def is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0:
        return False
    return all(n % d for d in range(3, math.isqrt(n) + 1, 2))


def spiral(count: int) -> List[Tuple[int, int]]:
    """(x, y) of the integers 1 ... count, x to the right and y UP, 1 at (0, 0)."""
    x = y = 0
    out = [(0, 0)]
    run, turn = 1, 0
    steps = ((1, 0), (0, 1), (-1, 0), (0, -1))           # east, north, west, south
    while len(out) < count:
        for _ in range(2):                               # two runs of each length
            dx, dy = steps[turn % 4]
            for _ in range(run):
                if len(out) >= count:
                    return out
                x, y = x + dx, y + dy
                out.append((x, y))
            turn += 1
        run += 1
    return out


# ── the scene: strokes, discs and rounded rectangles ───────────────────────────
Point = Tuple[float, float]


class Scene:
    def __init__(self, width: float, height: float) -> None:
        self.width, self.height, self.layers = width, height, []

    def rect(self, color: str, x: float, y: float, w: float, h: float, r: float) -> None:
        self.layers.append(("rect", color, (x, y, w, h, r)))

    def strokes(self, color: str, width: float, lines: Sequence[Sequence[Point]]) -> None:
        self.layers.append(("strokes", color, (width, [list(p) for p in lines])))

    def discs(self, color: str, radius: float, centres: Sequence[Point]) -> None:
        self.layers.append(("discs", color, (radius, list(centres))))


def _corners(points: List[Point]) -> List[Point]:
    """The polyline without the points that lie on a straight run."""
    out = [points[0]]
    for a, b, c in zip(points, points[1:], points[2:]):
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-9:
            out.append(b)
    out.append(points[-1])
    return out


def add_spiral(scene: Scene, spec: Tuple[int, float, float], left: float, top: float, side: float, *, path: str, marker: str) -> None:
    """Draw the mark of ``spec`` in the square at (left, top) of ``side`` scene units, markers fully inside it."""
    n, w, r = spec
    cell = side / (n - 1 + 2 * r)
    half = (n - 1) / 2
    place = lambda p: (left + r * cell + (p[0] + half) * cell, top + r * cell + (half - p[1]) * cell)  # noqa: E731
    cells = spiral(n * n)
    scene.strokes(path, w * cell, [_corners([place(p) for p in cells])])
    scene.discs(marker, r * cell, [place(p) for i, p in enumerate(cells, 1) if is_prime(i)])


def mark(spec: Tuple[int, float, float], *, path: str = CYAN, marker: str = INK) -> Scene:
    side = spec[0] * CELL
    scene = Scene(side, side)
    add_spiral(scene, spec, 0, 0, side, path=path, marker=marker)
    return scene


def tile(spec: Tuple[int, float, float], *, background: str = PAPER, path: str = CYAN, marker: str = INK) -> Scene:
    """The application icon: the mark on a rounded square."""
    scene = Scene(100, 100)
    scene.rect(background, 3, 3, 94, 94, 21)
    add_spiral(scene, spec, 15, 15, 70, path=path, marker=marker)
    return scene


def icon_spec(pixels: int) -> Tuple[int, float, float]:
    return TINY if pixels <= 24 else COMPACT if pixels <= 128 else PRIMARY


# ── lettering: r A I l s p as strokes on a grid with cap height 100 ────────────
def _bezier(p0, p1, p2, p3, steps: int = 24) -> List[Point]:
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append(tuple(u * u * u * p0[k] + 3 * u * u * t * p1[k] + 3 * u * t * t * p2[k] + t * t * t * p3[k] for k in (0, 1)))
    return out


def _path(start: Point, *segments) -> List[Point]:
    pts = [start]
    for seg in segments:
        if len(seg) == 2 and isinstance(seg[0], (int, float)):
            pts.append(seg)
        else:
            pts += _bezier(pts[-1], *seg)[1:]
    return pts


GLYPHS: Dict[str, Tuple[float, List[List[Point]]]] = {
    "r": (40, [_path((0, 100), (0, 35)), _path((0, 58), ((1, 42), (12, 34), (24, 34)), ((28, 34), (31, 35), (34, 36)))]),
    "A": (68, [_path((0, 100), (34, 4), (68, 100)), _path((13, 68), (55, 68))]),
    "I": (30, [_path((0, 4), (30, 4)), _path((15, 4), (15, 100)), _path((0, 100), (30, 100))]),
    "l": (18, [_path((0, 0), (0, 82), ((0, 94), (6, 100), (18, 100)))]),
    "s": (42, [_path((39, 41), ((35, 36), (29, 34), (21, 34)), ((10, 34), (2, 39), (2, 50)), ((2, 61), (12, 64), (22, 66)),
                     ((34, 69), (42, 72), (42, 83)), ((42, 94), (33, 100), (21, 100)), ((12, 100), (5, 97), (0, 92)))]),
    "p": (48, [_path((0, 34), (0, 130)), _path((0, 52), ((4, 41), (13, 34), (25, 34)), ((39, 34), (48, 48), (48, 67)),
                                            ((48, 86), (39, 100), (25, 100)), ((13, 100), (4, 93), (0, 82)))]),
}
LETTER_STROKE, LETTER_GAP = 11.5, 19.0


def word_width(text: str) -> float:
    return sum(GLYPHS[ch][0] for ch in text) + LETTER_GAP * (len(text) - 1)


def add_word(scene: Scene, text: str, left: float, top: float, cap: float, color: str) -> None:
    k = cap / 100.0
    x = 0.0
    lines = []
    for ch in text:
        advance, strokes = GLYPHS[ch]
        lines += [[(left + (x + px) * k, top + py * k) for px, py in s] for s in strokes]
        x += advance + LETTER_GAP
    scene.strokes(color, LETTER_STROKE * k, lines)


def lockup(text: str, *, ink: str = INK, path: str = CYAN, marker: str = INK) -> Scene:
    """The compact mark and the name, on one line. The name keeps its casing: rAIls, rAIlsApps."""
    side, gap, cap = 100.0, 30.0, 62.0
    width = side + gap + word_width(text) * cap / 100 + LETTER_STROKE
    scene = Scene(width, side)
    add_spiral(scene, COMPACT, 0, 0, side, path=path, marker=marker)
    add_word(scene, text, side + gap, 15.0, cap, ink)
    return scene


# ── SVG ────────────────────────────────────────────────────────────────────────
def _f(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def svg(scene: Scene, title: str = "rAIls") -> str:
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(scene.width)} {_f(scene.height)}" role="img" aria-label="{title}">']
    for kind, color, data in scene.layers:
        if kind == "rect":
            x, y, w, h, r = data
            out.append(f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}" rx="{_f(r)}" fill="{color}"/>')
        elif kind == "strokes":
            width, lines = data
            d = "".join("M" + " L".join(f"{_f(px)} {_f(py)}" for px, py in line) for line in lines)
            out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{_f(width)}" stroke-linecap="round" stroke-linejoin="round"/>')
        else:
            radius, centres = data
            out.append(f'<g fill="{color}">' + "".join(f'<circle cx="{_f(x)}" cy="{_f(y)}" r="{_f(radius)}"/>' for x, y in centres) + "</g>")
    return "\n".join(out + ["</svg>"]) + "\n"


# ── raster ─────────────────────────────────────────────────────────────────────
def _rgb(color: str) -> Tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def render(scene: Scene, width: int, background: "str | None" = None) -> Tuple[int, int, bytes]:
    """(width, height, RGBA with straight alpha)."""
    k = width / scene.width
    height = max(1, round(scene.height * k))
    pix = [[0.0, 0.0, 0.0, 0.0] for _ in range(width * height)]          # premultiplied
    if background:
        c = _rgb(background)
        pix = [[c[0], c[1], c[2], 1.0] for _ in range(width * height)]

    def paint(color: str, cover: Dict[int, float]) -> None:
        c = _rgb(color)
        for i, a in cover.items():
            p = pix[i]
            keep = 1.0 - a
            p[0], p[1], p[2], p[3] = c[0] * a + p[0] * keep, c[1] * a + p[1] * keep, c[2] * a + p[2] * keep, a + p[3] * keep

    def span(lo: float, hi: float, limit: int) -> range:
        return range(max(0, int(math.floor(lo))), min(limit, int(math.ceil(hi)) + 1))

    def put(cover: Dict[int, float], i: int, a: float) -> None:
        if a > 0.0 and a > cover.get(i, 0.0):                            # a union: overlapping parts are not painted twice
            cover[i] = min(a, 1.0)

    for kind, color, data in scene.layers:
        cover: Dict[int, float] = {}
        if kind == "rect":
            x, y, w, h, r = (v * k for v in data)
            cx, cy, hx, hy = x + w / 2, y + h / 2, w / 2, h / 2
            for py in span(y - 1, y + h + 1, height):
                for px in span(x - 1, x + w + 1, width):
                    qx, qy = abs(px + 0.5 - cx) - (hx - r), abs(py + 0.5 - cy) - (hy - r)
                    d = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
                    put(cover, py * width + px, 0.5 - d)
        elif kind == "strokes":
            stroke, lines = data
            half = stroke * k / 2
            for line in lines:
                for (ax, ay), (bx, by) in zip(line, line[1:]):
                    ax, ay, bx, by = ax * k, ay * k, bx * k, by * k
                    dx, dy = bx - ax, by - ay
                    length2 = dx * dx + dy * dy
                    for py in span(min(ay, by) - half - 1, max(ay, by) + half + 1, height):
                        for px in span(min(ax, bx) - half - 1, max(ax, bx) + half + 1, width):
                            ux, uy = px + 0.5 - ax, py + 0.5 - ay
                            t = 0.0 if length2 == 0 else max(0.0, min(1.0, (ux * dx + uy * dy) / length2))
                            put(cover, py * width + px, 0.5 - (math.hypot(ux - t * dx, uy - t * dy) - half))
        else:
            radius, centres = data
            rr = radius * k
            for x, y in centres:
                x, y = x * k, y * k
                for py in span(y - rr - 1, y + rr + 1, height):
                    for px in span(x - rr - 1, x + rr + 1, width):
                        put(cover, py * width + px, 0.5 - (math.hypot(px + 0.5 - x, py + 0.5 - y) - rr))
        paint(color, cover)

    out = bytearray(width * height * 4)
    for i, (r, g, b, a) in enumerate(pix):
        alpha = int(a * 255.0 + 0.5)
        if alpha:
            out[i * 4:i * 4 + 4] = bytes((min(255, int(r / a + 0.5)), min(255, int(g / a + 0.5)), min(255, int(b / a + 0.5)), alpha))
    return width, height, bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png(width: int, height: int, rgba: bytes) -> bytes:
    stride = width * 4
    raw, prev = bytearray(), bytes(stride)
    for y in range(height):
        row = rgba[y * stride:(y + 1) * stride]
        raw.append(2)
        raw += bytes((row[i] - prev[i]) & 0xFF for i in range(stride))
        prev = row
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


ICO_SIZES = (32, 16, 24, 48, 64, 128, 256)        # 32 first: Windows builds of Tauri take the first entry as the window icon
ICNS_TYPES = [("icp4", 16), ("icp5", 32), ("ic11", 32), ("ic12", 64), ("ic07", 128),
              ("ic13", 256), ("ic08", 256), ("ic14", 512), ("ic09", 512), ("ic10", 1024)]
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def ico(pngs: Dict[int, bytes]) -> bytes:
    offset = 6 + 16 * len(ICO_SIZES)
    entries, blobs = b"", b""
    for n in ICO_SIZES:
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(pngs[n]), offset + len(blobs))
        blobs += pngs[n]
    return struct.pack("<HHH", 0, 1, len(ICO_SIZES)) + entries + blobs


def icns(pngs: Dict[int, bytes]) -> bytes:
    body = b"".join(struct.pack(">4sI", t.encode(), 8 + len(pngs[n])) + pngs[n] for t, n in ICNS_TYPES)
    return struct.pack(">4sI", b"icns", 8 + len(body)) + body


# ── the asset set ──────────────────────────────────────────────────────────────
def masters() -> Dict[str, Scene]:
    """The editable SVG masters, by file name."""
    return {
        "mark.svg": mark(PRIMARY),
        "mark-dark.svg": mark(PRIMARY, marker=SNOW),
        "mark-mono-ink.svg": mark(PRIMARY, path=INK, marker=INK),
        "mark-mono-white.svg": mark(PRIMARY, path=PAPER, marker=PAPER),
        "compact.svg": mark(COMPACT),
        "compact-dark.svg": mark(COMPACT, marker=SNOW),
        "icon-tile.svg": tile(COMPACT),
        "icon-tile-dark.svg": tile(COMPACT, background=NIGHT, marker=SNOW),
        "icon-tile-tiny.svg": tile(TINY),
        "icon-tile-large.svg": tile(PRIMARY),
        "spiral-full.svg": mark(FULL),
        "spiral-full-dark.svg": mark(FULL, marker=SNOW),
        "lockup-rails.svg": lockup("rAIls"),
        "lockup-rails-dark.svg": lockup("rAIls", ink=SNOW, marker=SNOW),
        "lockup-railsapps.svg": lockup("rAIlsApps"),
        "lockup-railsapps-dark.svg": lockup("rAIlsApps", ink=SNOW, marker=SNOW),
    }


def icon_png(n: int) -> bytes:
    return png(*render(tile(icon_spec(n)), n))


def build() -> Dict[str, bytes]:
    """Every file of the asset set: relative path -> bytes."""
    files = {f"svg/{name}": svg(scene, "rAIlsApps" if "railsapps" in name else "rAIls").encode() for name, scene in masters().items()}
    pngs = {n: icon_png(n) for n in ICON_SIZES}
    files.update({f"png/icon-{n}.png": data for n, data in pngs.items()})
    files["icon.ico"] = ico(pngs)
    files["icon.icns"] = icns(pngs)
    for name, scene in (("mark", mark(PRIMARY)), ("mark-dark", mark(PRIMARY, marker=SNOW)), ("spiral-full", mark(FULL)),
                        ("spiral-full-dark", mark(FULL, marker=SNOW))):
        files[f"png/{name}-1024.png"] = png(*render(scene, 1024))
    for name in ("lockup-rails", "lockup-rails-dark", "lockup-railsapps", "lockup-railsapps-dark"):
        files[f"png/{name}-1200.png"] = png(*render(masters()[name + ".svg"], 1200))
    return files


def comparison_sheet() -> bytes:
    """Candidate grids as application icons at small sizes, on light and on dark, for choosing by eye. One row per grid
    (5, 7, 11, 17); columns are 16, 24, 32, 48, 64, 128 px, each drawn at its true size and then enlarged 4x for 16-32."""
    grids = [(5, 0.22, 0.37), (7, 0.20, 0.36), (11, 0.17, 0.35), (17, 0.16, 0.35)]
    sizes, shown = (16, 24, 32, 48, 64, 128), (64, 96, 128, 48, 64, 128)
    pad, cell = 16, 144
    width, height = pad + cell * len(sizes), pad + cell * len(grids) * 2
    canvas = bytearray(width * height * 4)
    for row, (spec, back) in enumerate([(g, b) for b in ("#F3F4F6", "#1B2236") for g in grids]):
        r, g, b = _rgb(back)
        for y in range(row * cell, (row + 1) * cell + (pad if row == len(grids) * 2 - 1 else 0)):
            canvas[y * width * 4:(y + 1) * width * 4] = bytes((r, g, b, 255)) * width
        for col, (n, big) in enumerate(zip(sizes, shown)):
            w, h, data = render(tile(spec), n)
            zoom = big // n
            ox, oy = pad + col * cell, pad + row * cell
            for y in range(h * zoom):
                for x in range(w * zoom):
                    s = ((y // zoom) * w + (x // zoom)) * 4
                    a = data[s + 3] / 255
                    d = ((oy + y) * width + ox + x) * 4
                    canvas[d:d + 3] = bytes(int(data[s + i] * a + canvas[d + i] * (1 - a) + 0.5) for i in range(3))
    return png(width, height, bytes(canvas))


def main(argv: List[str]) -> None:
    if argv[:1] == ["--sheet"]:
        Path(argv[1]).write_bytes(comparison_sheet())
        return
    out = Path(argv[0]) if argv else Path(__file__).resolve().parent / "assets"
    for name, data in build().items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)


if __name__ == "__main__":
    main(sys.argv[1:])
