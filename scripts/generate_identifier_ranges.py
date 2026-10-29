#!/usr/bin/env python3
# Copyright (c) 2025-2026 RedevOps.
# Licensed under the GNU Affero General Public License v3.0 or later, as modified by the Commons Clause License
# Condition v1.0 (source-available; see LICENSE, LICENSE.md and LICENSE-COMMONS-CLAUSE).
# SPDX-License-Identifier: LicenseRef-AGPL-3.0-or-later-with-Commons-Clause
"""Generate ``agentic_os/identifier_ranges.json``, the code-point table behind the kernel's identifier contract.

Kernel-owned and written from the functional specification in ``docs/IDENTIFIER_CONTRACT.md`` and the Unicode Standard (UAX #44, the Unicode
Character Database). Stdlib only. Deterministic: the same three UCD input files always give the same bytes. It never consults the interpreter's
``unicodedata`` (that module is used only by an optional cross-check in the tests), so the Python version cannot change the table.

Input: the published Unicode Character Database 17.0.0 (data under the Unicode License v3), three files:

* ``UnicodeData.txt``                      the assigned code points and their General_Category (``<..., First>`` / ``<..., Last>`` pairs are ranges);
* ``extracted/DerivedGeneralCategory.txt`` the General_Category of EVERY code point, unassigned (Cn) included;
* ``PropList.txt``                         the White_Space, Bidi_Control and Noncharacter_Code_Point properties.

The SHA-256 of each input is pinned in this script (``PINNED_SHA256``) and verified before use, and is written to the provenance file.

Output (schema 1, canonical bytes: sorted keys, compact separators, ASCII, one trailing newline): ``forbidden`` (every code point whose
General_Category is Cc, Cf, Cs, Cn, Co, Zl or Zp, plus the Bidi_Control property), ``white_space`` (the White_Space property, used only for the
leading/trailing rule), ``bidi_controls``, ``categories`` (the per-category ranges that were merged into ``forbidden``) and descriptive strings.
All ranges are inclusive ``[lo, hi]``, sorted ascending, merged where they overlap or touch.

Usage::

    python scripts/generate_identifier_ranges.py --ucd-dir DIR --out agentic_os/identifier_ranges.json \\
        --provenance-out agentic_os/identifier_ranges.provenance.json
    python scripts/generate_identifier_ranges.py --ucd-dir DIR --check agentic_os/identifier_ranges.json
    python scripts/generate_identifier_ranges.py --cache-dir ~/.cache/ucd      # downloads (unicode.org only), verifies, prints to stdout
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterable

SCHEMA = 1
UNICODE_VERSION = "17.0.0"
MAX_CP = 0x10FFFF
GENERATOR = "scripts/generate_identifier_ranges.py"
UCD_BASE_URL = f"https://www.unicode.org/Public/{UNICODE_VERSION}/ucd/"

# local file name -> path below UCD_BASE_URL
UCD_FILES = {
    "UnicodeData.txt": "UnicodeData.txt",
    "PropList.txt": "PropList.txt",
    "DerivedGeneralCategory.txt": "extracted/DerivedGeneralCategory.txt",
}
# SHA-256 of the published 17.0.0 files (the files are immutable once a version is released).
PINNED_SHA256 = {
    "UnicodeData.txt": "2e1efc1dcb59c575eedf5ccae60f95229f706ee6d031835247d843c11d96470c",
    "PropList.txt": "130dcddcaadaf071008bdfce1e7743e04fdfbc910886f017d9f9ac931d8c64dd",
    "DerivedGeneralCategory.txt": "d62e5bab70ca74f099343f71224fa051cb1fdd61a1ab45c0488c44cfc0b6102e",
}

# The one switch: the General_Category values that make a code point forbidden in an identifier.
FORBIDDEN_CATEGORIES = ("Cc", "Cf", "Cs", "Cn", "Co", "Zl", "Zp")
ALL_CATEGORIES = frozenset(
    "Lu Ll Lt Lm Lo Mn Mc Me Nd Nl No Pc Pd Ps Pe Pi Pf Po Sm Sc Sk So Zs Zl Zp Cc Cf Cs Co Cn".split())
CONTRACT = ("identifiers: non-empty; no leading/trailing White_Space; no Cc/Cf/Cs/Cn/Co/Zl/Zp code point or bidi control anywhere; "
            "no case folding, no normalization")

# Facts fixed by the Unicode Standard / the contract, asserted against the data (see ``check_stable_facts``).
BIDI_CONTROL_CONTRACT = ((0x061C, 0x061C), (0x200E, 0x200F), (0x202A, 0x202E), (0x2066, 0x2069))
FORBIDDEN_SPOT = (0x0000, 0x001F, 0x007F, 0x0085, 0x009F, 0x0378, 0x200B, 0x200C, 0x200D, 0x2028, 0x2029, 0x202E, 0x2060, 0xD800, 0xDFFF,
                  0xE000, 0xF8FF, 0xFEFF, 0xFFFE, 0xFFFF, 0xF0000, 0xFFFFD, 0x100000, 0x10FFFD, 0x10FFFF)
ALLOWED_SPOT = (0x0020, 0x00A0, 0x0021, 0x002F, 0x0030, 0x0039, 0x0041, 0x005A, 0x0061, 0x007A, 0x007E, 0x00E9, 0x0301, 0x0416, 0x4E2D,
                0x0627, 0x05D0, 0x0905, 0x093F, 0x1F600, 0x20000)


class UCDError(ValueError):
    """The input is not a well-formed UCD file, or the files disagree with each other or with a fixed Unicode fact."""


# --------------------------------------------------------------------------------------------- ranges

def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[list[int]]:
    """Sort and merge inclusive ranges that overlap or touch."""
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if lo > hi:
            raise UCDError(f"inverted range {lo:#x}..{hi:#x}")
        if out and lo <= out[-1][1] + 1:
            if hi > out[-1][1]:
                out[-1][1] = hi
        else:
            out.append([lo, hi])
    return out


def _runs(values: bytearray) -> Iterable[tuple[int, int, int]]:
    """Yield (lo, hi, value) for each maximal run of equal bytes."""
    start = 0
    n = len(values)
    while start < n:
        v = values[start]
        end = start + 1
        while end < n and values[end] == v:
            end += 1
        yield start, end - 1, v
        start = end


# --------------------------------------------------------------------------------------------- parsing

_HEX = re.compile(r"^[0-9A-F]{4,6}$")
_HEADER = re.compile(r"^#\s*(UnicodeData|PropList|DerivedGeneralCategory)-(\d+\.\d+\.\d+)\.txt\s*$")


def _cp(token: str, where: str) -> int:
    token = token.strip()
    if not _HEX.match(token):
        raise UCDError(f"{where}: {token!r} is not a code point (4 to 6 upper-case hex digits)")
    cp = int(token, 16)
    if cp > MAX_CP:
        raise UCDError(f"{where}: code point {token} is above U+10FFFF")
    return cp


def parse_unicode_data(text: str, name: str = "UnicodeData.txt") -> list[tuple[int, int, str]]:
    """Assigned code points as ``(lo, hi, General_Category)`` (First/Last pairs expanded to one range); strictly ascending, no overlap."""
    out: list[tuple[int, int, str]] = []
    pending: tuple[int, str, str] | None = None            # (first code point, base name, category) of an open First line
    last_end = -1
    for n, line in enumerate(text.splitlines(), 1):
        where = f"{name}:{n}"
        if not line.strip():
            continue
        f = line.split(";")
        if len(f) != 15:
            raise UCDError(f"{where}: expected 15 ';'-separated fields, found {len(f)}")
        cp = _cp(f[0], where)
        label, gc = f[1], f[2]
        if gc not in ALL_CATEGORIES:
            raise UCDError(f"{where}: unknown General_Category {gc!r}")
        if cp <= last_end:
            raise UCDError(f"{where}: code points are not strictly ascending (U+{cp:04X} after U+{last_end:04X})")
        if pending is not None:
            lo, base, pgc = pending
            if not (label.startswith("<") and label.endswith(", Last>") and label[1:-len(", Last>")] == base and gc == pgc):
                raise UCDError(f"{where}: a '<{base}, First>' line must be followed by its matching ', Last>' line")
            out.append((lo, cp, gc))
            last_end, pending = cp, None
        elif label.startswith("<") and label.endswith(", First>"):
            pending = (cp, label[1:-len(", First>")], gc)
            last_end = cp
        elif label.startswith("<") and label.endswith(", Last>"):
            raise UCDError(f"{where}: a ', Last>' line without a preceding ', First>' line")
        else:
            out.append((cp, cp, gc))
            last_end = cp
    if pending is not None:
        raise UCDError(f"{name}: unterminated '<{pending[1]}, First>' range")
    if not out:
        raise UCDError(f"{name}: no data")
    return out


def parse_ranges_file(text: str, name: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Parse a ``XXXX[..YYYY] ; value # comment`` file (PropList.txt, DerivedGeneralCategory.txt).

    Returns ``(version from the header line, [(lo, hi, value), ...])`` in file order.
    """
    version: str | None = None
    out: list[tuple[int, int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        where = f"{name}:{n}"
        if n == 1:
            m = _HEADER.match(raw.lstrip("﻿"))
            if not m:
                raise UCDError(f"{where}: the first line must be the '# <File>-<version>.txt' header")
            version = m.group(2)
            continue
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != 2 or not parts[1]:
            raise UCDError(f"{where}: expected 'code point(s) ; value', found {raw!r}")
        span = parts[0].split("..")
        if len(span) == 1:
            lo = hi = _cp(span[0], where)
        elif len(span) == 2:
            lo, hi = _cp(span[0], where), _cp(span[1], where)
            if lo > hi:
                raise UCDError(f"{where}: inverted range {parts[0]}")
        else:
            raise UCDError(f"{where}: malformed range {parts[0]!r}")
        out.append((lo, hi, parts[1]))
    if version is None:
        raise UCDError(f"{name}: empty file")
    if not out:
        raise UCDError(f"{name}: no data")
    return version, out


# --------------------------------------------------------------------------------------------- the table

def _category_array(ranges: list[tuple[int, int, str]], index: dict[str, int], name: str, *, fill: int = 0) -> bytearray:
    """A byte per code point holding the category index; a code point given twice is an error. ``fill`` is the value of untouched cells."""
    arr = bytearray([fill]) * (MAX_CP + 1)
    seen = bytearray(MAX_CP + 1)
    for lo, hi, gc in ranges:
        if gc not in index:
            raise UCDError(f"{name}: unknown General_Category {gc!r}")
        if any(seen[lo:hi + 1]):
            raise UCDError(f"{name}: overlapping ranges around U+{lo:04X}..U+{hi:04X}")
        seen[lo:hi + 1] = b"\x01" * (hi - lo + 1)
        arr[lo:hi + 1] = bytes([index[gc]]) * (hi - lo + 1)
    return arr


def build_table(unicode_data: str, prop_list: str, derived_gc: str, *, check_stable: bool = True) -> dict:
    """Compute the table (a dict ready for ``serialize``) from the text of the three UCD files, asserting every invariant.

    ``check_stable=False`` skips only the assertions about the real Unicode Standard (66 noncharacters, the bidi list, the spot checks); the
    structural invariants and the cross-checks between the files always run.
    """
    ud = parse_unicode_data(unicode_data)
    v_prop, props = parse_ranges_file(prop_list, "PropList.txt")
    v_gc, dgc = parse_ranges_file(derived_gc, "DerivedGeneralCategory.txt")
    if v_prop != v_gc:
        raise UCDError(f"PropList.txt is version {v_prop} but DerivedGeneralCategory.txt is {v_gc}")
    version = v_prop

    cats = sorted(ALL_CATEGORIES)
    index = {c: i + 1 for i, c in enumerate(cats)}           # 0 is reserved for "not set"
    cn = index["Cn"]

    # DerivedGeneralCategory must give every code point exactly one category (Cn is listed explicitly).
    dgc_arr = _category_array(dgc, index, "DerivedGeneralCategory.txt")
    gaps = [(lo, hi) for lo, hi, v in _runs(dgc_arr) if v == 0]
    if gaps:
        raise UCDError(f"DerivedGeneralCategory.txt does not classify U+{gaps[0][0]:04X}..U+{gaps[0][1]:04X}"
                       f"{' (and more)' if len(gaps) > 1 else ''}")

    # UnicodeData lists the assigned code points; everything it does not list is unassigned (Cn). The two files must agree exactly.
    ud_arr = _category_array(ud, index, "UnicodeData.txt", fill=cn)
    if ud_arr != dgc_arr:
        i = next(i for i in range(MAX_CP + 1) if ud_arr[i] != dgc_arr[i])
        raise UCDError(f"UnicodeData.txt and DerivedGeneralCategory.txt disagree at U+{i:04X}: "
                       f"{cats[ud_arr[i] - 1]} vs {cats[dgc_arr[i] - 1]}")

    by_cat: dict[str, list[tuple[int, int]]] = {c: [] for c in FORBIDDEN_CATEGORIES}
    wanted = {index[c]: c for c in FORBIDDEN_CATEGORIES}
    for lo, hi, v in _runs(dgc_arr):
        if v in wanted:
            by_cat[wanted[v]].append((lo, hi))
    categories = {c: merge_ranges(r) for c, r in by_cat.items()}

    def prop(name: str) -> list[list[int]]:
        r = merge_ranges((lo, hi) for lo, hi, v in props if v == name)
        if not r:
            raise UCDError(f"PropList.txt has no {name} entries")
        return r

    bidi = prop("Bidi_Control")
    white_space = prop("White_Space")
    forbidden = merge_ranges([tuple(r) for rs in categories.values() for r in rs] + [tuple(r) for r in bidi])

    table = {
        "schema": SCHEMA,
        "unicode_version": version,
        "generated_by": GENERATOR,
        "contract": CONTRACT,
        "categories": categories,
        "forbidden": forbidden,
        "white_space": white_space,
        "bidi_controls": bidi,
    }
    validate_table(table)
    if check_stable:
        check_stable_facts(table, prop("Noncharacter_Code_Point"))
    return table


def _cover(ranges: list[list[int]], cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def validate_table(table: dict) -> None:
    """Assert the structural invariants of a table (also usable on a file loaded from disk)."""
    if table.get("schema") != SCHEMA:
        raise UCDError("unsupported schema")
    lists = {"forbidden": table["forbidden"], "white_space": table["white_space"], "bidi_controls": table["bidi_controls"],
             **{f"categories.{c}": r for c, r in table["categories"].items()}}
    for name, rs in lists.items():
        prev_hi = None
        for r in rs:
            if len(r) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in r):
                raise UCDError(f"{name}: {r!r} is not a [lo, hi] pair of integers")
            lo, hi = r
            if not 0 <= lo <= hi <= MAX_CP:
                raise UCDError(f"{name}: [{lo:#x}, {hi:#x}] is out of range or inverted")
            if prev_hi is not None and lo <= prev_hi + 1:
                raise UCDError(f"{name}: [{lo:#x}, {hi:#x}] is unsorted, overlapping or adjacent to the previous range")
            prev_hi = hi
    if set(table["categories"]) != set(FORBIDDEN_CATEGORIES):
        raise UCDError(f"categories must be exactly {', '.join(FORBIDDEN_CATEGORIES)}")
    union = merge_ranges([tuple(r) for rs in table["categories"].values() for r in rs] + [tuple(r) for r in table["bidi_controls"]])
    if union != table["forbidden"]:
        raise UCDError("forbidden is not the union of the categories and the bidi controls")


def check_stable_facts(table: dict, noncharacters: list[list[int]]) -> None:
    """Assert facts of the Unicode Standard and of the contract against the computed table."""
    forbidden = table["forbidden"]
    expected_nc = {*range(0xFDD0, 0xFDF0), *(p * 0x10000 + s for p in range(17) for s in (0xFFFE, 0xFFFF))}
    if len(expected_nc) != 66:
        raise UCDError("internal error: the noncharacter count is not 66")
    listed_nc = {cp for lo, hi in noncharacters for cp in range(lo, hi + 1)}
    if listed_nc != expected_nc:
        raise UCDError("PropList.txt Noncharacter_Code_Point is not the 66 code points U+FDD0..U+FDEF and U+xFFFE/U+xFFFF")
    for cp in sorted(expected_nc):
        if not _cover(forbidden, cp):
            raise UCDError(f"noncharacter U+{cp:04X} is not forbidden")
    if [list(r) for r in BIDI_CONTROL_CONTRACT] != table["bidi_controls"]:
        raise UCDError("Bidi_Control is not U+061C, U+200E..200F, U+202A..202E, U+2066..2069")
    for lo, hi in table["bidi_controls"]:
        for cp in (lo, hi):
            if not _cover(forbidden, cp):
                raise UCDError(f"bidi control U+{cp:04X} is not forbidden")
    ranges_expected = {"Co": [[0xE000, 0xF8FF], [0xF0000, 0xFFFFD], [0x100000, 0x10FFFD]], "Zl": [[0x2028, 0x2028]], "Zp": [[0x2029, 0x2029]],
                       "Cs": [[0xD800, 0xDFFF]]}
    for c, want in ranges_expected.items():
        if table["categories"][c] != want:
            raise UCDError(f"category {c} is not {want}")
    for cp in FORBIDDEN_SPOT:
        if not _cover(forbidden, cp):
            raise UCDError(f"U+{cp:04X} must be forbidden")
    for lo, hi in ((0x0000, 0x001F), (0x007F, 0x009F), (0x200B, 0x200D)):
        for cp in range(lo, hi + 1):
            if not _cover(forbidden, cp):
                raise UCDError(f"U+{cp:04X} must be forbidden")
    for cp in ALLOWED_SPOT:
        if _cover(forbidden, cp):
            raise UCDError(f"U+{cp:04X} must not be forbidden")
    for lo, hi in ((0x0300, 0x036F), (0x0410, 0x044F), (0x0620, 0x063F), (0x05D0, 0x05EA), (0x0904, 0x0939), (0x4E00, 0x9FFF),
                   (0x1F600, 0x1F64F), (0x0021, 0x007E)):
        for cp in range(lo, hi + 1):
            if _cover(forbidden, cp):
                raise UCDError(f"U+{cp:04X} must not be forbidden")
    ws = table["white_space"]
    for cp in (0x20, 0xA0, 0x2028, 0x2029, 0x85, 0x3000):
        if not _cover(ws, cp):
            raise UCDError(f"U+{cp:04X} must be White_Space")
    if _cover(ws, 0x200B) or _cover(ws, 0xFEFF):
        raise UCDError("U+200B and U+FEFF are not White_Space")


# --------------------------------------------------------------------------------------------- serialization

def serialize(obj: dict) -> bytes:
    """The canonical bytes: sorted keys, ``,`` and ``:`` separators with no spaces, ASCII, one trailing newline."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def provenance(table_bytes: bytes, file_hashes: dict[str, str], file_sizes: dict[str, int], version: str) -> dict:
    return {
        "schema": SCHEMA,
        "generator": GENERATOR,
        "unicode_version": version,
        "ucd_base_url": UCD_BASE_URL,
        "inputs": {n: {"path": UCD_FILES[n], "sha256": file_hashes[n], "bytes": file_sizes[n]} for n in sorted(file_hashes)},
        "table": {"file": "identifier_ranges.json", "sha256": hashlib.sha256(table_bytes).hexdigest(), "bytes": len(table_bytes)},
    }


# --------------------------------------------------------------------------------------------- inputs

def default_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "agentic-os" / f"ucd-{UNICODE_VERSION}"


def _verify(name: str, data: bytes, *, pinned: bool) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if pinned and digest != PINNED_SHA256[name]:
        raise UCDError(f"{name}: SHA-256 {digest} does not match the pinned UCD {UNICODE_VERSION} file ({PINNED_SHA256[name]})")
    return digest


def _read_local(directory: Path, name: str) -> bytes:
    for candidate in (directory / name, directory / UCD_FILES[name]):
        if candidate.is_file():
            return candidate.read_bytes()
    raise UCDError(f"{name} not found in {directory}")


def _download(name: str, cache: Path) -> bytes:
    url = UCD_BASE_URL + UCD_FILES[name]
    if not url.startswith("https://www.unicode.org/"):
        raise UCDError("refusing to download from anywhere but unicode.org")
    with urllib.request.urlopen(url, timeout=60) as resp:          # noqa: S310 (fixed https unicode.org URL)
        data = resp.read()
    _verify(name, data, pinned=True)                                # only a verified file ever reaches the cache
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / (name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, cache / name)
    return data


def load_inputs(ucd_dir: Path | None, cache_dir: Path | None, *, pinned: bool = True) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Return ``(texts, sha256s, sizes)`` keyed by file name, from ``ucd_dir`` or from the (verified) download cache."""
    texts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    cache = cache_dir or default_cache_dir()
    for name in UCD_FILES:
        if ucd_dir is not None:
            data = _read_local(ucd_dir, name)
        else:
            cached = cache / name
            data = cached.read_bytes() if cached.is_file() else b""
            if not data or hashlib.sha256(data).hexdigest() != PINNED_SHA256[name]:
                data = _download(name, cache)
        hashes[name] = _verify(name, data, pinned=pinned)
        sizes[name] = len(data)
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise UCDError(f"{name} is not UTF-8: {e}") from None
    return texts, hashes, sizes


def generate(ucd_dir: Path | None = None, cache_dir: Path | None = None, *, pinned: bool = True) -> tuple[bytes, bytes]:
    """Return ``(identifier_ranges.json bytes, provenance json bytes)``."""
    texts, hashes, sizes = load_inputs(ucd_dir, cache_dir, pinned=pinned)
    table = build_table(texts["UnicodeData.txt"], texts["PropList.txt"], texts["DerivedGeneralCategory.txt"])
    data = serialize(table)
    return data, serialize(provenance(data, hashes, sizes, table["unicode_version"]))


# --------------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--ucd-dir", type=Path, help="directory holding UnicodeData.txt, PropList.txt and DerivedGeneralCategory.txt "
                                                "(also accepted: extracted/DerivedGeneralCategory.txt)")
    ap.add_argument("--cache-dir", type=Path, help=f"download cache when --ucd-dir is not given (default {default_cache_dir()})")
    ap.add_argument("--out", type=Path, help="write identifier_ranges.json here (default: stdout)")
    ap.add_argument("--provenance-out", type=Path, help="also write the provenance file (input SHA-256s, table SHA-256) here")
    ap.add_argument("--check", type=Path, metavar="FILE", help="exit non-zero if FILE differs from the freshly generated bytes")
    ap.add_argument("--check-provenance", type=Path, metavar="FILE", help="exit non-zero if FILE differs from the freshly generated provenance")
    a = ap.parse_args(argv)
    try:
        data, prov = generate(a.ucd_dir, a.cache_dir)
    except (UCDError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    status = 0
    for flag, path, want in (("--check", a.check, data), ("--check-provenance", a.check_provenance, prov)):
        if path is not None:
            have = path.read_bytes() if path.is_file() else None
            if have != want:
                print(f"{flag}: {path} differs from the generated output", file=sys.stderr)
                status = 1
    if a.out:
        a.out.write_bytes(data)
    elif a.check is None and a.check_provenance is None:
        sys.stdout.buffer.write(data)
    if a.provenance_out:
        a.provenance_out.write_bytes(prov)
    return status


if __name__ == "__main__":
    sys.exit(main())
