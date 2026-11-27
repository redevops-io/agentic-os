"""Channel derivatives of the rAIls mark: the website's assets and the owner's store-upload bundle. Standard library only.

Everything is drawn from the same scenes as ``rails_mark.py`` (one spiral algorithm); a channel only changes padding, background and canvas.
Copyright (c) 2025-2026 RedevOps. Same terms and the same classification (core) as rails_mark.py; standard library only.

    python brand/channels.py site   OUT_DIR     favicon, apple-touch icon, manifest icons, social preview
    python brand/channels.py stores OUT_DIR     Microsoft Store listing art, Snap Store listing art, a preview contact sheet, a manifest

Store specifications used (checked 2026-11-27; the owner re-checks them in Partner Center and the Snap Store dashboard at upload time):
  Microsoft Store, MSI/EXE listing (learn.microsoft.com .../publish-your-app/msi/screenshots-and-images and the MSIX page that gives sizes):
    1:1 app tile icon 300 x 300 PNG; 1:1 box art 1080 x 1080 (or 2160) with the title in the top two thirds; 2:3 poster art 720 x 1080 (or 1440 x 2160);
    16:9 super hero art 1920 x 1080 PNG with NO text and no app UI; screenshots (at least one is required, 1366 x 768 or larger) are real captures
    and are not generated here. Text overlays may cover the bottom third of box art and poster art, so nothing important sits there.
  Snap Store (forum.snapcraft.io "Store listing and branding", 2020 with a 2024 correction; snapcraft.io/about/listing and the Snapcraft blog, read 2026-11-27):
    icon 1:1, PNG/JPEG (SVG reported to work), 512 x 512 recommended and the maximum, at most 256 kB (that figure is from the forum only);
    banner 3:1 between 720 x 240 and 4320 x 1440 (1920 x 640 or larger recommended), at most 2 MB, and it must be uploaded with the exact name banner.png or banner.jpg;
    screenshots 480 x 480 to 3840 x 2160, at most 2 MB each, **at most 5 in all, and the banner and the banner-icon count towards those 5**;
    an optional banner-icon.png/.jpg is overlaid on the LEFT of the banner, so a banner that already carries the mark at the left (this one) ships without it.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rails_mark as R  # noqa: E402

DIM_PATH, DIM_MARKER = "#17507A", "#34415F"      # the decorative spiral on the dark canvas: present, not competing with the lettering


# ── scenes ─────────────────────────────────────────────────────────────────────
def square_tile(spec, *, background: str = R.PAPER, marker: str = R.INK, side: float = 70.0) -> R.Scene:
    """Full-bleed square (no rounded corners, no transparency): for platforms that apply their own mask (iOS touch icon, maskable icons)."""
    scene = R.Scene(100, 100)
    scene.rect(background, 0, 0, 100, 100, 0)
    off = (100 - side) / 2
    R.add_spiral(scene, spec, off, off, side, path=R.CYAN, marker=marker)
    return scene


def poster(width: int, height: int, *, word: bool, mark_side: float, top: float, cap: float = 0.0, background: str = R.NIGHT) -> R.Scene:
    """The primary mark centred horizontally, optionally the name under it. ``top`` and the name stay in the top two thirds of the canvas."""
    scene = R.Scene(width, height)
    scene.rect(background, 0, 0, width, height, 0)
    R.add_spiral(scene, R.PRIMARY, (width - mark_side) / 2, top, mark_side, path=R.CYAN, marker=R.SNOW)
    if word:
        w = R.word_width("rAIlsApps") * cap / 100 + R.LETTER_STROKE
        R.add_word(scene, "rAIlsApps", (width - w) / 2, top + mark_side + cap * 0.35, cap, R.SNOW)
    return scene


def super_hero() -> R.Scene:
    """16:9, no text, no app UI: the full spiral large and central, dim enough to read as art, with the compact mark's colours."""
    scene = R.Scene(1920, 1080)
    scene.rect(R.NIGHT, 0, 0, 1920, 1080, 0)
    R.add_spiral(scene, R.FULL, 480, 60, 960, path=R.CYAN, marker=R.SNOW)
    return scene


def banner() -> R.Scene:
    """3:1: the lockup at the left, the full spiral cropped by nothing at the right."""
    scene = R.Scene(1920, 640)
    scene.rect(R.NIGHT, 0, 0, 1920, 640, 0)
    side, cap = 150.0, 96.0
    R.add_spiral(scene, R.COMPACT, 150, 245, side, path=R.CYAN, marker=R.SNOW)
    R.add_word(scene, "rAIlsApps", 150 + side + 50, 245 + side * 0.15, cap, R.SNOW)
    R.add_spiral(scene, R.FULL, 1220, 60, 520, path=DIM_PATH, marker=DIM_MARKER)
    return scene


def social() -> R.Scene:
    """1200 x 630 preview: the lockup at the left over a dark canvas, the dim full spiral at the right. The name only: no lettering beyond what the generator draws."""
    scene = R.Scene(1200, 630)
    scene.rect(R.NIGHT, 0, 0, 1200, 630, 0)
    side, cap = 140.0, 88.0
    R.add_spiral(scene, R.COMPACT, 80, 245, side, path=R.CYAN, marker=R.SNOW)
    R.add_word(scene, "rAIlsApps", 80 + side + 40, 245 + side * 0.15, cap, R.SNOW)
    R.add_spiral(scene, R.FULL, 800, 135, 360, path=DIM_PATH, marker=DIM_MARKER)
    return scene


def _png(scene: R.Scene, width: int) -> bytes:
    return R.png(*R.render(scene, width))


def _ico(sizes: Tuple[int, ...]) -> bytes:
    import struct
    pngs = {n: R.icon_png(n) for n in sizes}
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for n in sizes:
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(pngs[n]), offset + len(blobs))
        blobs += pngs[n]
    return struct.pack("<HHH", 0, 1, len(sizes)) + entries + blobs


def favicon_svg() -> str:
    """The tiny tile (5 x 5, for 16-24 px), light by default and dark under prefers-color-scheme: dark."""
    text = R.svg(R.tile(R.TINY), "rAIls")
    text = text.replace(f'fill="{R.PAPER}"', 'class="bg" fill="%s"' % R.PAPER, 1)
    text = text.replace(f'<g fill="{R.INK}">', f'<g class="mk" fill="{R.INK}">', 1)
    style = f'<style>@media (prefers-color-scheme: dark){{.bg{{fill:{R.NIGHT}}}.mk{{fill:{R.SNOW}}}}}</style>\n'
    return text.replace("\n", "\n" + style, 1)


# ── channel builders: relative path -> bytes ───────────────────────────────────
def site() -> Dict[str, bytes]:
    files: Dict[str, bytes] = {
        "favicon.svg": favicon_svg().encode(),
        "favicon.ico": _ico((32, 16, 48)),
        "apple-touch-icon.png": _png(square_tile(R.COMPACT), 180),
        "icon-192.png": _png(R.tile(R.COMPACT), 192),
        "icon-512.png": _png(R.tile(R.PRIMARY), 512),
        "icon-maskable-512.png": _png(square_tile(R.COMPACT, background=R.PAPER, side=56.0), 512),
        "social-rails.png": _png(social(), 1200),
    }
    files["site.webmanifest"] = (json.dumps({
        "name": "redevops.app", "short_name": "rAIlsApps", "start_url": "/", "display": "browser",
        "background_color": R.PAPER, "theme_color": R.NIGHT,
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    }, indent=2) + "\n").encode()
    return files


def store_scenes() -> List[Tuple[str, R.Scene, int]]:
    """(file name, scene, pixel width), in the order of the checklist. File names carry the mark's version (``-v1``)."""
    v = f"-v{R.VERSION}"
    return [(re.sub(r"\.png$", v + ".png", name), scene, w) for name, scene, w in _store_scenes()]


def _store_scenes() -> List[Tuple[str, R.Scene, int]]:
    return [
        ("microsoft/ms-01-app-tile-icon-300x300.png", R.tile(R.COMPACT), 300),
        ("microsoft/ms-02-box-art-1080x1080.png", poster(1080, 1080, word=True, mark_side=380, top=90, cap=84), 1080),
        ("microsoft/ms-03-poster-art-720x1080.png", poster(720, 1080, word=True, mark_side=330, top=90, cap=58), 720),
        ("microsoft/ms-04-super-hero-1920x1080-optional.png", super_hero(), 1920),
        ("snap/snap-01-icon-512x512.png", R.tile(R.PRIMARY), 512),
        ("snap/snap-02-banner-1920x640.png", banner(), 1920),   # upload it named banner.png (the store's required name); the version stays in this name
    ]


def contact_sheet(items: List[Tuple[str, R.Scene, int]]) -> bytes:
    """Every listing graphic at thumbnail size on one page, on light and on dark grounds, in checklist order."""
    thumb, pad, cols = 300, 24, 4
    rows = (len(items) + cols - 1) // cols
    cell_w, cell_h = thumb + pad, thumb + pad
    width, height = pad + cell_w * cols, pad + cell_h * rows
    canvas = bytearray(b"\xf3\xf4\xf6\xff" * (width * height))
    for i, (_, scene, _w) in enumerate(items):
        w, h, data = R.render(scene, thumb if scene.width >= scene.height else max(1, round(thumb * scene.width / scene.height)))
        ox, oy = pad + (i % cols) * cell_w, pad + (i // cols) * cell_h
        for y in range(h):
            for x in range(w):
                s, d = (y * w + x) * 4, ((oy + y) * width + ox + x) * 4
                a = data[s + 3] / 255
                canvas[d:d + 3] = bytes(int(data[s + k] * a + canvas[d + k] * (1 - a) + 0.5) for k in range(3))
    return R.png(width, height, bytes(canvas))


def stores() -> Dict[str, bytes]:
    items = store_scenes()
    files = {name: _png(scene, w) for name, scene, w in items}
    files["preview-contact-sheet.png"] = contact_sheet(items)
    files["MANIFEST.sha256"] = "".join(f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in sorted(files.items())).encode()
    return files


def main(argv: List[str]) -> None:
    if len(argv) != 2 or argv[0] not in ("site", "stores"):
        raise SystemExit(__doc__)
    out = Path(argv[1])
    for name, data in (site() if argv[0] == "site" else stores()).items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)


if __name__ == "__main__":
    main(sys.argv[1:])
