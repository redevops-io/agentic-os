"""The rAIls mark is mathematically what it says: a square spiral with 1 at the centre, east first, anticlockwise, and a
marker exactly on each prime, in every variant; the committed assets are the generator's output."""
from __future__ import annotations

import importlib.util
import re
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "brand" / "assets"
_spec = importlib.util.spec_from_file_location("rails_mark", ROOT / "brand" / "rails_mark.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

PRIMES_BELOW_122 = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113]


def test_primality():
    assert [n for n in range(-3, 122) if M.is_prime(n)] == PRIMES_BELOW_122
    sieve = [True] * 2402
    sieve[0] = sieve[1] = False
    for i in range(2, 2402):
        if sieve[i]:
            for j in range(i * i, 2402, i):
                sieve[j] = False
    assert [M.is_prime(n) for n in range(2402)] == sieve


def test_the_spiral_starts_at_the_centre_goes_east_and_turns_anticlockwise():
    cells = M.spiral(121)
    known = {1: (0, 0), 2: (1, 0), 3: (1, 1), 4: (0, 1), 5: (-1, 1), 6: (-1, 0), 7: (-1, -1), 9: (1, -1), 10: (2, -1),
             13: (2, 2), 17: (-2, 2), 25: (2, -2), 49: (3, -3), 81: (4, -4), 121: (5, -5)}      # odd squares: the bottom right corners
    assert {n: cells[n - 1] for n in known} == known
    assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(cells, cells[1:]))      # one continuous path
    for n in (5, 7, 11, 49):
        half = (n - 1) // 2
        assert set(M.spiral(n * n)) == {(x, y) for x in range(-half, half + 1) for y in range(-half, half + 1)}


@pytest.mark.parametrize("spec", [M.TINY, M.COMPACT, M.PRIMARY, M.FULL])
def test_a_marker_sits_on_each_prime_cell_and_nowhere_else(spec):
    n = spec[0]
    scene = M.mark(spec)
    (_, _, (_, lines)), (_, _, (radius, centres)) = scene.layers
    cell = scene.width / (n - 1 + 2 * spec[2])
    half = (n - 1) / 2

    def grid(p):
        return (round((p[0] - spec[2] * cell) / cell - half), round(half - (p[1] - spec[2] * cell) / cell))

    cells = M.spiral(n * n)
    assert sorted(grid(c) for c in centres) == sorted(cells[i - 1] for i in range(1, n * n + 1) if M.is_prime(i))
    assert grid(lines[0][0]) == (0, 0) and grid(lines[0][-1]) == cells[-1]
    assert all(radius - 1e-9 <= v <= scene.width - radius + 1e-9 for c in centres for v in c)    # nothing is clipped


def _pixels(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, chunks = 8, {}
    while at < len(data):
        size, tag = struct.unpack(">I4s", data[at:at + 8])
        chunks.setdefault(tag, b"")
        chunks[tag] += data[at + 8:at + 8 + size]
        at += 12 + size
    assert set(chunks) == {b"IHDR", b"IDAT", b"IEND"}                  # no metadata
    return chunks[b"IHDR"], zlib.decompress(chunks[b"IDAT"])


def test_the_committed_assets_are_what_the_generator_makes():
    built = M.build()
    on_disk = {p.relative_to(ASSETS).as_posix(): p.read_bytes() for p in ASSETS.rglob("*") if p.is_file()}
    assert set(on_disk) == set(built)
    for name, data in built.items():
        if name.endswith(".svg"):
            assert on_disk[name] == data, name
        elif name.endswith(".png"):
            assert _pixels(on_disk[name]) == _pixels(data), name      # compared decoded: the deflate stream depends on zlib


def test_icon_containers_hold_every_size_with_32_first():
    ico = (ASSETS / "icon.ico").read_bytes()
    count = struct.unpack("<H", ico[4:6])[0]
    sizes = [ico[6 + 16 * i] or 256 for i in range(count)]
    assert sizes == list(M.ICO_SIZES) and sizes[0] == 32
    icns = (ASSETS / "icon.icns").read_bytes()
    assert icns[:4] == b"icns" and struct.unpack(">I", icns[4:8])[0] == len(icns)
    types, at = [], 8
    while at < len(icns):
        tag, size = struct.unpack(">4sI", icns[at:at + 8])
        types.append(tag.decode())
        at += size
    assert types == [t for t, _ in M.ICNS_TYPES]


def test_small_icons_use_the_grid_that_stays_readable():
    assert [M.icon_spec(n)[0] for n in (16, 24, 32, 48, 64, 128, 256, 1024)] == [5, 5, 7, 7, 7, 7, 11, 11]


def test_the_name_keeps_its_casing_and_uses_no_font():
    for name in ("lockup-rails.svg", "lockup-railsapps.svg"):
        text = (ASSETS / "svg" / name).read_text()
        assert "<text" not in text and "font" not in text
    assert set("rAIlsApps") <= set(M.GLYPHS) and re.search(r'aria-label="rAIlsApps"', (ASSETS / "svg" / "lockup-railsapps.svg").read_text())
