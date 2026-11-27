"""The channel derivatives of the rAIls mark meet the store specifications they were made for, and are deterministic.

The specifications are the ones recorded at the top of brand/channels.py (checked 2026-11-27). A change in a store's rules is a change to
that docstring and to these numbers together, on purpose.
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brand"))
import channels as C  # noqa: E402
import rails_mark as R  # noqa: E402


def dims(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def pixels(data: bytes):
    """(width, height, RGBA rows) of a PNG this generator wrote (8-bit RGBA, filter 2 = Up)."""
    w, h = dims(data)
    pos, idat = 8, b""
    while pos < len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw, stride, rows, prev = zlib.decompress(idat), w * 4, [], bytes(w * 4)
    for y in range(h):
        line = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        assert raw[y * (stride + 1)] == 2
        prev = bytes((line[i] + prev[i]) & 0xFF for i in range(stride))
        rows.append(prev)
    return w, h, rows


@pytest.fixture(scope="module")
def stores():
    return C.stores()


@pytest.fixture(scope="module")
def site():
    return C.site()


# ── Microsoft Store ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,size", [
    ("microsoft/ms-01-app-tile-icon-300x300-v1.png", (300, 300)),
    ("microsoft/ms-02-box-art-1080x1080-v1.png", (1080, 1080)),
    ("microsoft/ms-03-poster-art-720x1080-v1.png", (720, 1080)),
    ("microsoft/ms-04-super-hero-1920x1080-optional-v1.png", (1920, 1080)),
])
def test_microsoft_listing_art_has_the_published_pixel_sizes(stores, name, size):
    assert dims(stores[name]) == size
    assert len(stores[name]) < 50 * 1024 * 1024                      # the limit is 50 MB per PNG


def test_box_art_and_poster_keep_everything_in_the_top_two_thirds(stores):
    """Text overlays may cover the bottom third: no non-background pixel there."""
    for name in ("microsoft/ms-02-box-art-1080x1080-v1.png", "microsoft/ms-03-poster-art-720x1080-v1.png"):
        w, h, rows = pixels(stores[name])
        bg = rows[h - 1][:4]
        for y in range(h * 2 // 3, h):
            assert rows[y] == bg * w, f"{name}: content in the bottom third at row {y}"


# ── Snap Store ─────────────────────────────────────────────────────────────────
def test_snap_icon_is_square_at_most_512_and_256_kb(stores):
    data = stores["snap/snap-01-icon-512x512-v1.png"]
    w, h = dims(data)
    assert w == h <= 512 and w >= 40
    assert len(data) <= 256 * 1024


def test_snap_banner_is_3_to_1_within_the_published_bounds_and_2_mb(stores):
    data = stores["snap/snap-02-banner-1920x640-v1.png"]
    w, h = dims(data)
    assert w == 3 * h and 720 <= w <= 4320 and 240 <= h <= 1440
    assert len(data) <= 2 * 1024 * 1024


def test_the_bundle_manifest_covers_every_file_and_matches(stores):
    import hashlib
    listed = dict(line.split("  ", 1)[::-1] for line in stores["MANIFEST.sha256"].decode().splitlines())
    assert set(listed) == {n for n in stores if n != "MANIFEST.sha256"}
    assert all(hashlib.sha256(stores[n]).hexdigest() == h for n, h in listed.items())


def test_stores_are_deterministic(stores):
    assert C.stores() == stores


# ── website ────────────────────────────────────────────────────────────────────
def test_site_assets_have_the_sizes_the_manifest_declares(site):
    manifest = json.loads(site["site.webmanifest"])
    assert manifest["short_name"] == "rAIlsApps"
    for icon in manifest["icons"]:
        w, h = dims(site[icon["src"].lstrip("/")])
        assert f"{w}x{h}" == icon["sizes"]
    assert dims(site["apple-touch-icon.png"]) == (180, 180)
    assert dims(site["social-rails.png"]) == (1200, 630)


def test_apple_touch_and_maskable_icons_are_opaque_squares(site):
    for name in ("apple-touch-icon.png", "icon-maskable-512.png"):
        _, _, rows = pixels(site[name])
        assert all(row[3::4] == b"\xff" * (len(row) // 4) for row in rows), f"{name} has transparency"


def test_maskable_spiral_stays_inside_the_80_percent_safe_circle():
    """The spiral's square must fit a circle of 40% of the canvas: its corner distance from the centre is side / sqrt(2)."""
    scene = C.square_tile(R.COMPACT, side=56.0)
    for kind, _color, data in scene.layers:
        if kind == "discs":
            radius, centres = data
            assert all(((x - 50) ** 2 + (y - 50) ** 2) ** 0.5 + radius <= 40.0 + 1e-6 for x, y in centres)


def test_favicon_svg_is_the_tiny_tile_with_a_dark_scheme_rule(site):
    text = site["favicon.svg"].decode()
    assert text.count("<circle") == sum(1 for i in range(1, 26) if R.is_prime(i))      # the 5 x 5 grid holds 1..25
    assert "prefers-color-scheme: dark" in text and R.NIGHT in text and R.SNOW in text


def test_favicon_ico_lists_16_32_and_48_with_32_first(site):
    count = struct.unpack("<H", site["favicon.ico"][4:6])[0]
    sizes = [site["favicon.ico"][6 + 16 * i] for i in range(count)]
    assert sizes == [32, 16, 48]


def test_site_is_deterministic(site):
    assert C.site() == site


def test_the_snap_set_has_no_banner_icon_because_the_banner_carries_the_mark_and_the_cap_is_five(stores):
    names = [n for n in stores if n.startswith("snap/")]
    assert names == ["snap/snap-01-icon-512x512-v1.png", "snap/snap-02-banner-1920x640-v1.png"]
    assert "banner-icon" not in " ".join(stores)
