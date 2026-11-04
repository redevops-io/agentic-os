#!/usr/bin/env python3
"""Generate agentic_os/projects_ui/hero.jpg: a neutral placeholder drawn only from code.

It replaces an earlier image whose embedded metadata named a third-party generator (C2PA claim generator
"Black Forest Labs API") and whose terms of use were never read. This one contains no third-party content:
a gradient and a few geometric shapes drawn with Pillow (a build-time tool; not shipped, not a runtime
dependency), no text, no fonts, no photographs, no model output, no metadata. The drawing is deterministic.

    python scripts/make_neutral_hero.py [output.jpg]

Requires Pillow only to run this script (`pip install pillow`).
"""
from __future__ import annotations

import sys
from pathlib import Path

W, H = 1024, 576
INK, TEAL, WASH, LINE = (15, 28, 30), (13, 143, 128), (220, 239, 236), (201, 214, 214)


def main(out: Path) -> None:
    from PIL import Image, ImageDraw   # lazy: only this script needs it

    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):                       # vertical gradient, ink -> teal
        for x in range(W):
            t = (0.65 * y / (H - 1)) + (0.35 * x / (W - 1))
            px[x, y] = tuple(int(INK[i] + (TEAL[i] - INK[i]) * t * 0.55) for i in range(3))
    d = ImageDraw.Draw(img, "RGBA")
    hub = (W * 0.70, H * 0.50)
    # three "apps" (rounded squares), a "database" (stacked ellipses), a "document" (page with rules)
    nodes = [(W * 0.16, H * 0.22), (W * 0.16, H * 0.50), (W * 0.16, H * 0.78)]
    for (cx, cy) in nodes:
        d.line([(cx + 60, cy), hub], fill=LINE + (110,), width=3)
    d.line([(W * 0.43, H * 0.50), hub], fill=LINE + (110,), width=3)
    for (cx, cy) in nodes:
        d.rounded_rectangle([cx - 56, cy - 56, cx + 56, cy + 56], radius=22, fill=WASH + (235,))
        d.rounded_rectangle([cx - 30, cy - 30, cx + 30, cy + 30], radius=12, fill=TEAL + (255,))
    dx, dy = W * 0.43, H * 0.50
    for k in (-34, 0, 34):
        d.ellipse([dx - 52, dy + k - 22, dx + 52, dy + k + 22], fill=WASH + (235,), outline=TEAL + (255,), width=4)
    d.rounded_rectangle([W * 0.43 - 44, H * 0.50 + 92, W * 0.43 + 44, H * 0.50 + 196], radius=8, fill=WASH + (235,))
    for k in range(4):
        d.line([(W * 0.43 - 28, H * 0.50 + 118 + 20 * k), (W * 0.43 + 28, H * 0.50 + 118 + 20 * k)],
               fill=TEAL + (255,), width=4)
    for r, a in ((150, 40), (112, 80), (78, 255)):   # the "one governed system": concentric rings
        d.ellipse([hub[0] - r, hub[1] - r, hub[0] + r, hub[1] + r],
                  fill=(WASH if r == 78 else TEAL) + (a,), outline=LINE + (150,), width=3)
    d.ellipse([hub[0] - 28, hub[1] - 28, hub[0] + 28, hub[1] + 28], fill=TEAL + (255,))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=88, optimize=False, progressive=False)   # no exif/xmp/icc/c2pa is written


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else
         Path(__file__).resolve().parents[1] / "agentic_os" / "projects_ui" / "hero.jpg")
