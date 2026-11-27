# Launcher icons: the rAIls mark

These six files are the rAIls mark (a square spiral of the integers with a marker on each prime), drawn by
`brand/rails_mark.py` (Python standard library only; SHA-256 of the generator `5e5aee8e6957582385795d4ebb66bd3166dae241f17d47457818e938a9e89b79`;
no fonts, no third-party content, no model output in the files). The owner approved the mark for the desktop
application on 2026-11-27; its definition, variants and colours are in `brand/README.md`. Nothing here is a
statement about rights or clearance either way.

History: on 2026-11-05 the owner had the previous letter-mark icons replaced with neutral temporary artwork while
decision D-LIC-11 ("Launcher icons and the mark") was unresolved. These files replace that temporary artwork. The decision
records for D-LIC-11 are the owner's and are not changed by this directory; `tests/test_launcher_icons.py` keeps
the previous letter mark out of the repository.

| File | SHA-256 |
|---|---|
| `32x32.png` | `1c4d5fcf74a9c05dd1e770fac573c775b59809272e4dcbc2aaf3a0f4175bf0be` |
| `128x128.png` | `4bb529e59018d23aba7b2c932d7b1bbd231d2f68ed902910d8079a5b12cbf398` |
| `128x128@2x.png` | `00f871b066fc94466716ddc561e0abcaa9d220b8208538d3900bc51f318a39cd` |
| `icon.png` | `3369df2ff1515a7cae6d3102fec7ed7561136cbd89165b0992f504de3b6e3dbf` |
| `icon.icns` | `019432dadd4b22ba2dd38e15492967838d99f1fe7bc280cd7a0b580ac8693fb8` |
| `icon.ico` | `ad7e56e6bb5b6430e9dfe43ef09961754c3efe047c6b70dd6ae4053b6c6b9fcc` |

Sizes by file: the mark's 5 x 5 tile is used up to 24 px, the 7 x 7 compact tile for 32 to 128 px and the 11 x 11
primary tile from 256 px (`icon_spec` in the generator). Formats: PNG 8-bit RGBA (32, 128, 256 for the `@2x` file, 512);
`icon.ico` holds 32, 16, 24, 48, 64, 128 and 256, 32 listed first (every image a PNG; tauri-codegen's Windows
`default_window_icon()` reads the first directory entry, so 32x32 must be first for the compiled default icon to match
`32x32.png` on Windows too); `icon.icns` holds the standard iconset sizes 16 to 1024 (including the `@2x` variants) as
PNG. The files carry no metadata (no timestamps, no text chunks, no software name).

Regenerate with `python brand/rails_mark.py <out dir>` and copy `png/icon-32.png`, `png/icon-128.png`,
`png/icon-256.png` (as `128x128@2x.png`), `png/icon-512.png` (as `icon.png`), `icon.ico` and `icon.icns`;
`tests/test_launcher_icons.py` checks that the files equal the generator output and that this table is current.
The compiled window icon (embedded into the launcher binary at build time from `32x32.png`) is checked by the Rust
test `the_compiled_default_window_icon_is_the_rails_mark_32x32`.
