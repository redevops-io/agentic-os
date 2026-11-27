# The rAIls mark (version 1)

A square spiral of the integers, one continuous path, a marker on each prime. It is drawn from the mathematics by
[`rails_mark.py`](rails_mark.py) (standard library only); nothing is traced from another image.

**Status: proposed.** The owner approves the primary mark and the compact variant before they are applied to the site,
the desktop application or store listings. Mathematical originality is not trademark clearance; none is claimed.

## Definition

| | |
|---|---|
| Start | **1 at the centre** |
| Orientation and winding | 2 is east of 1; then the path turns **anticlockwise** as seen on screen (north, west, south, east), with run lengths 1, 1, 2, 2, 3, 3, … |
| Grid | n × n, n odd, holding 1 … n². The path visits cell centres in order and ends at n² |
| Markers | a filled circle on a cell exactly when its integer is prime |

## Variants

| Variant | Grid | Path width / marker radius (cells) | Use | Smallest size |
|---|---|---|---|---|
| Tiny icon | 5 | 0.22 / 0.37 | 16 and 24 px icons only | 16 px |
| **Compact** | 7 | 0.20 / 0.36 | 32–128 px icons, header, favicon, lockups | 32 px (24 px in a lockup's height) |
| **Primary mark** | 11 | 0.20 / 0.36 | 160 px and larger, 256 px+ icons | 96 px |
| Full spiral | 49 | 0.14 / 0.36 | decoration only, never an icon or a logo | 480 px (below that it reads as texture) |

The grids were chosen by eye from 5, 7, 11 and 17 at 16–128 px (`python brand/rails_mark.py --sheet sheet.png`): 17 is
unreadable as an icon, 11 loses its path below about 96 px, and only 5 holds at 16 px.

## Colour and contrast

| Name | Value | Use |
|---|---|---|
| Cyan | `#1896D6` | the path, always |
| Ink | `#0F1B33` | markers and lettering on light |
| Snow | `#EEF2FB` | markers and lettering on dark |
| Paper / Night | `#FFFFFF` / `#0B1222` | tile and page backgrounds |

Contrast (WCAG formula): ink on white 17.1:1; snow on night 16.7:1; cyan on white 3.3:1, on the site's `#FBFAF7` 3.2:1,
on night 5.7:1; a marker against the path 5.2:1. One-colour versions use ink or white for both; markers stay distinct by
their size.

**Clear space:** at least one fifth of the mark's width on every side (for a lockup, one fifth of its height).
**Do not** rotate, mirror or recolour the spiral, move or add markers, or set the name in another casing: it is
**rAIls** and **rAIlsApps**. The lockups' lettering is drawn as strokes by the generator; no font is involved.

## Assets

`assets/svg/` are the editable masters; `assets/png/`, `assets/icon.ico` (32 first, for Windows builds of Tauri) and
`assets/icon.icns` are rendered from the same scenes. Regenerate with `python brand/rails_mark.py`;
`tests/test_brand_mark.py` checks the primes, the spiral's coordinates, one marker per prime in every variant, and that
the committed files are the generator's output. Platform and store packages (padding, backgrounds) are derived from
these; see the store bundle's own checklist.

Suggested explanation (a proposal, not final copy): "The rAIls mark is a square spiral of prime numbers. It represents how
meaningful structure emerges when information is organized and filtered." Optional line: "Intelligence through
structure."
