# Copyright (c) 2025-2026 RedevOps.
# Licensed under the GNU Affero General Public License v3.0 or later, as modified by the Commons Clause License
# Condition v1.0 (source-available; see LICENSE, LICENSE.md and LICENSE-COMMONS-CLAUSE).
# SPDX-License-Identifier: LicenseRef-AGPL-3.0-or-later-with-Commons-Clause
"""``scripts/generate_identifier_ranges.py`` and the table it produces (``agentic_os/identifier_ranges.json``).

Three layers:

* a SYNTHETIC mini-UCD (hand-written, about twenty lines per file) exercises the generator logic: First/Last expansion, unassigned gaps, merging,
  White_Space, Bidi_Control, Co/Zl/Zp/Cn, determinism, canonical serialization and malformed input;
* the checked-in table and its provenance file are validated and pinned BY THEIR SHA-256 (the table is pinned, not the script);
* with ``UCD_DIR`` set to a directory (missing UCD 17.0.0 files are downloaded into it, hash-verified), the real files are regenerated and
  must reproduce the checked-in bytes; on an interpreter whose ``unicodedata`` is Unicode 17.0.0 the whole table is cross-checked against it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import random
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLE = ROOT / "agentic_os" / "identifier_ranges.json"
PROVENANCE = ROOT / "agentic_os" / "identifier_ranges.provenance.json"
TABLE_SHA256 = "5cb60e8d4c97612146bcd7df69a9aca0f31f1ac7f457ac97d4ae993902b8fc45"        # the generated table, Unicode 17.0.0

_spec = importlib.util.spec_from_file_location("generate_identifier_ranges", ROOT / "scripts" / "generate_identifier_ranges.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

# ───────────────────────── the synthetic mini-UCD ─────────────────────────

MINI_UNICODE_DATA = """\
0000;<control>;Cc;0;BN;;;;;N;NULL;;;;
0020;SPACE;Zs;0;WS;;;;;N;;;;;
0041;LATIN CAPITAL LETTER A;Lu;0;L;;;;;N;;;;0061;
0042;LATIN CAPITAL LETTER B;Lu;0;L;;;;;N;;;;0062;
00A0;NO-BREAK SPACE;Zs;0;CS;<noBreak> 0020;;;;N;NON-BREAKING SPACE;;;;
0301;COMBINING ACUTE ACCENT;Mn;230;NSM;;;;;N;NON-SPACING ACUTE;;;;
200E;LEFT-TO-RIGHT MARK;Cf;0;L;;;;;N;;;;;
2028;LINE SEPARATOR;Zl;0;WS;;;;;N;;;;;
2029;PARAGRAPH SEPARATOR;Zp;0;B;;;;;N;;;;;
4E00;<CJK Ideograph, First>;Lo;0;L;;;;;N;;;;;
4E02;<CJK Ideograph, Last>;Lo;0;L;;;;;N;;;;;
D800;<Surrogate, First>;Cs;0;L;;;;;N;;;;;
DFFF;<Surrogate, Last>;Cs;0;L;;;;;N;;;;;
E000;<Private Use, First>;Co;0;L;;;;;N;;;;;
E002;<Private Use, Last>;Co;0;L;;;;;N;;;;;
1F600;GRINNING FACE;So;0;ON;;;;;N;;;;;
"""
MINI_PROP_LIST = """\
# PropList-17.0.0.txt
# a comment line and a blank line follow

0020          ; White_Space # Zs       SPACE
00A0          ; White_Space # Zs       NO-BREAK SPACE
2028..2029    ; White_Space # Zl   [2] LINE SEPARATOR..PARAGRAPH SEPARATOR
200E..200F    ; Bidi_Control # Cf   [2] LEFT-TO-RIGHT MARK..RIGHT-TO-LEFT MARK
"""
MINI_DERIVED_GC = """\
# DerivedGeneralCategory-17.0.0.txt
0000          ; Cc
0001..001F    ; Cn
0020          ; Zs
0021..0040    ; Cn
0041..0042    ; Lu
0043..009F    ; Cn
00A0          ; Zs
00A1..0300    ; Cn
0301          ; Mn
0302..200D    ; Cn
200E          ; Cf
200F..2027    ; Cn
2028          ; Zl
2029          ; Zp
202A..4DFF    ; Cn
4E00..4E02    ; Lo
4E03..D7FF    ; Cn
D800..DFFF    ; Cs
E000..E002    ; Co
E003..1F5FF   ; Cn
1F600         ; So
1F601..10FFFF ; Cn
"""
# What the generator must make of them: Cc 0 and the Cn gap 1..1F touch (merge); the three gaps around U+0041..42, the Zl/Zp/Cf/Cn run around
# U+2028, and the Cs/Co/Cn run D800..1F5FF each merge across categories; the assigned First/Last ranges and the letters stay allowed.
MINI_FORBIDDEN = [[0x0, 0x1F], [0x21, 0x40], [0x43, 0x9F], [0xA1, 0x300], [0x302, 0x4DFF], [0x4E03, 0x1F5FF], [0x1F601, 0x10FFFF]]


def mini(**over):
    args = dict(unicode_data=MINI_UNICODE_DATA, prop_list=MINI_PROP_LIST, derived_gc=MINI_DERIVED_GC, check_stable=False)
    args.update(over)
    return gen.build_table(**args)


def in_ranges(ranges, cp):
    return any(lo <= cp <= hi for lo, hi in ranges)


class TestMiniUCD:
    def test_forbidden_merges_adjacent_ranges_across_categories(self):
        t = mini()
        assert t["forbidden"] == MINI_FORBIDDEN

    def test_first_last_pairs_are_expanded_to_ranges(self):
        rows = gen.parse_unicode_data(MINI_UNICODE_DATA)
        assert (0x4E00, 0x4E02, "Lo") in rows and (0xD800, 0xDFFF, "Cs") in rows and (0xE000, 0xE002, "Co") in rows and len(rows) == 13
        t = mini()
        assert t["categories"]["Cs"] == [[0xD800, 0xDFFF]] and t["categories"]["Co"] == [[0xE000, 0xE002]]
        assert not in_ranges(t["forbidden"], 0x4E00) and not in_ranges(t["forbidden"], 0x4E01) and not in_ranges(t["forbidden"], 0x4E02)

    def test_unassigned_gaps_are_cn_and_the_complement_of_unicode_data(self):
        t = mini()
        listed = {cp for lo, hi, _ in gen.parse_unicode_data(MINI_UNICODE_DATA) for cp in range(lo, hi + 1)}
        cn = {cp for lo, hi in t["categories"]["Cn"] for cp in range(lo, hi + 1)}
        assert cn == set(range(gen.MAX_CP + 1)) - listed
        assert 0x378 in cn and 0xFFFE in cn and 0x10FFFF in cn          # unassigned and noncharacters are Cn

    def test_co_zl_zp_are_forbidden_and_still_white_space(self):
        t = mini()
        assert t["categories"]["Zl"] == [[0x2028, 0x2028]] and t["categories"]["Zp"] == [[0x2029, 0x2029]]
        for cp in (0xE000, 0xE001, 0xE002, 0x2028, 0x2029):
            assert in_ranges(t["forbidden"], cp), hex(cp)
        assert in_ranges(t["white_space"], 0x2028) and in_ranges(t["white_space"], 0x2029)

    def test_white_space_and_bidi_controls_come_from_the_property_list(self):
        t = mini()
        assert t["white_space"] == [[0x20, 0x20], [0xA0, 0xA0], [0x2028, 0x2029]]
        assert t["bidi_controls"] == [[0x200E, 0x200F]]
        assert in_ranges(t["forbidden"], 0x200F)                        # a bidi control is forbidden even when it is not Cf in the data

    def test_space_nbsp_letters_marks_and_symbols_are_allowed(self):
        t = mini()
        for cp in (0x20, 0xA0, 0x41, 0x42, 0x301, 0x4E00, 0x1F600):
            assert not in_ranges(t["forbidden"], cp), hex(cp)

    def test_bidi_control_that_is_not_a_listed_category_is_still_forbidden(self):
        t = mini(prop_list=MINI_PROP_LIST + "0301          ; Bidi_Control # test\n")           # a Mn made Bidi_Control by the property list
        assert in_ranges(t["forbidden"], 0x301) and [0x301, 0x301] in t["bidi_controls"]

    def test_the_category_list_is_cc_cf_cs_cn_co_zl_zp(self):
        assert set(mini()["categories"]) == {"Cc", "Cf", "Cs", "Cn", "Co", "Zl", "Zp"}
        assert gen.FORBIDDEN_CATEGORIES == ("Cc", "Cf", "Cs", "Cn", "Co", "Zl", "Zp")

    def test_output_is_deterministic_and_independent_of_line_order_in_the_property_list(self):
        a = gen.serialize(mini())
        assert a == gen.serialize(mini())
        lines = MINI_PROP_LIST.splitlines()[1:]
        random.Random(7).shuffle(lines)
        shuffled = "# PropList-17.0.0.txt\n" + "\n".join(lines) + "\n"
        assert gen.serialize(mini(prop_list=shuffled)) == a
        rev = "# DerivedGeneralCategory-17.0.0.txt\n" + "\n".join(reversed(MINI_DERIVED_GC.splitlines()[1:])) + "\n"
        assert gen.serialize(mini(derived_gc=rev)) == a

    def test_serialization_is_canonical(self):
        data = gen.serialize(mini())
        assert data.endswith(b"]}\n") and data.count(b"\n") == 1 and b'": ' not in data and b'", ' not in data
        obj = json.loads(data)
        assert data == (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode()
        assert list(obj) == sorted(obj) and list(obj["categories"]) == sorted(obj["categories"])
        assert set(obj) == {"schema", "unicode_version", "generated_by", "contract", "categories", "forbidden", "white_space", "bidi_controls"}
        assert obj["schema"] == 1 and obj["unicode_version"] == "17.0.0"

    def test_the_structural_validator_accepts_it_and_rejects_broken_tables(self):
        t = mini()
        gen.validate_table(t)
        for mutate in (lambda x: x["forbidden"].reverse(),
                       lambda x: x["forbidden"].insert(1, [0x1F, 0x22]),                        # overlaps
                       lambda x: x["forbidden"].__setitem__(0, [0, 0x20]),                     # touches the next range, and is not the union
                       lambda x: x["white_space"].__setitem__(0, [0x20, 0x10]),                # inverted
                       lambda x: x["white_space"].append([0x110000, 0x110000]),                # out of range
                       lambda x: x["categories"].pop("Zl"),
                       lambda x: x.__setitem__("schema", 2)):
            bad = json.loads(gen.serialize(t))
            mutate(bad)
            with pytest.raises(gen.UCDError):
                gen.validate_table(bad)

    def test_strict_mode_rejects_a_ucd_that_lacks_the_unicode_facts(self):
        with pytest.raises(gen.UCDError, match="Noncharacter"):
            gen.build_table(MINI_UNICODE_DATA, MINI_PROP_LIST, MINI_DERIVED_GC)

    def test_a_ucd_inconsistent_with_the_unicode_standard_fails_strict_mode(self):
        # the noncharacter list is right but Bidi_Control is wrong
        nc = "FDD0..FDEF ; Noncharacter_Code_Point\n" + "".join(f"{p:X}FFFE..{p:X}FFFF ; Noncharacter_Code_Point\n" for p in range(17))
        with pytest.raises(gen.UCDError, match="Bidi_Control"):
            gen.build_table(MINI_UNICODE_DATA, MINI_PROP_LIST + nc, MINI_DERIVED_GC)


class TestMalformedInput:
    @pytest.mark.parametrize("bad, why", [
        ("0000;<control>;Cc;0;BN;;;;;N;NULL;;;\n", "15"),                                      # 14 fields
        ("00G0;X;Lu;0;L;;;;;N;;;;;\n", "not a code point"),
        ("110000;X;Lu;0;L;;;;;N;;;;;\n", r"above U\+10FFFF"),
        ("0041;X;Xx;0;L;;;;;N;;;;;\n", "unknown General_Category"),
        ("0042;X;Lu;0;L;;;;;N;;;;;\n0041;Y;Lu;0;L;;;;;N;;;;;\n", "ascending"),
        ("0041;X;Lu;0;L;;;;;N;;;;;\n0041;Y;Lu;0;L;;;;;N;;;;;\n", "ascending"),
        ("4E00;<CJK, First>;Lo;0;L;;;;;N;;;;;\n", "unterminated"),
        ("4E00;<CJK, First>;Lo;0;L;;;;;N;;;;;\n4E02;<Other, Last>;Lo;0;L;;;;;N;;;;;\n", "matching"),
        ("4E00;<CJK, First>;Lo;0;L;;;;;N;;;;;\n4E02;<CJK, Last>;Ll;0;L;;;;;N;;;;;\n", "matching"),
        ("4E02;<CJK, Last>;Lo;0;L;;;;;N;;;;;\n", "without a preceding"),
        ("\n\n", "no data"),
    ])
    def test_unicode_data(self, bad, why):
        with pytest.raises(gen.UCDError, match=why):
            gen.parse_unicode_data(bad)

    @pytest.mark.parametrize("bad, why", [
        ("0020 ; White_Space\n", "header"),                                                    # no header line
        ("# PropList-17.0.0.txt\n0020 White_Space\n", "expected"),
        ("# PropList-17.0.0.txt\n0020 ; \n", "expected"),
        ("# PropList-17.0.0.txt\n0020 ; A ; B\n", "expected"),
        ("# PropList-17.0.0.txt\nzz ; White_Space\n", "not a code point"),
        ("# PropList-17.0.0.txt\n0030..0020 ; White_Space\n", "inverted"),
        ("# PropList-17.0.0.txt\n0020..0030..0040 ; White_Space\n", "malformed"),
        ("# PropList-17.0.0.txt\n20 ; White_Space\n", "not a code point"),                     # fewer than 4 hex digits
        ("# PropList-17.0.0.txt\n0020 ; White_Space\n2000000 ; X\n", "not a code point"),
        ("# PropList-17.0.0.txt\n# only comments\n", "no data"),
        ("", "empty"),
    ])
    def test_range_files(self, bad, why):
        with pytest.raises(gen.UCDError, match=why):
            gen.parse_ranges_file(bad, "PropList.txt")

    def test_a_trailing_comment_and_blank_lines_are_fine(self):
        v, rows = gen.parse_ranges_file("# PropList-17.0.0.txt\n\n0020 ; White_Space # c\n   \n", "x")
        assert v == "17.0.0" and rows == [(0x20, 0x20, "White_Space")]

    def test_the_two_derived_files_must_be_the_same_version(self):
        with pytest.raises(gen.UCDError, match="version"):
            mini(prop_list=MINI_PROP_LIST.replace("17.0.0", "16.0.0"))

    def test_derived_general_category_must_cover_every_code_point(self):
        with pytest.raises(gen.UCDError, match="does not classify"):
            mini(derived_gc=MINI_DERIVED_GC.replace("1F601..10FFFF ; Cn\n", "1F601..10FFFE ; Cn\n"))

    def test_derived_general_category_must_not_overlap(self):
        with pytest.raises(gen.UCDError, match="overlapping"):
            mini(derived_gc=MINI_DERIVED_GC + "0000..0001 ; Cc\n")

    def test_unicode_data_and_derived_general_category_must_agree(self):
        with pytest.raises(gen.UCDError, match="disagree"):
            mini(derived_gc=MINI_DERIVED_GC.replace("0041..0042    ; Lu", "0041          ; Lu\n0042          ; Ll"))
        # a code point UnicodeData lists but DerivedGeneralCategory calls unassigned (and the reverse)
        with pytest.raises(gen.UCDError, match="disagree"):
            mini(derived_gc=MINI_DERIVED_GC.replace("1F600         ; So\n1F601..10FFFF ; Cn", "1F600..10FFFF ; Cn"))
        with pytest.raises(gen.UCDError, match="disagree"):
            mini(unicode_data=MINI_UNICODE_DATA.replace("1F600;GRINNING FACE;So;0;ON;;;;;N;;;;;\n", ""))

    def test_an_unknown_category_in_the_derived_file(self):
        with pytest.raises(gen.UCDError, match="unknown General_Category"):
            mini(derived_gc=MINI_DERIVED_GC.replace("0020          ; Zs", "0020          ; Zz"))

    def test_the_cli_reports_a_malformed_file_with_exit_2_and_no_output(self, tmp_path, capsys):
        for name, text in (("UnicodeData.txt", "garbage\n"), ("PropList.txt", MINI_PROP_LIST), ("DerivedGeneralCategory.txt", MINI_DERIVED_GC)):
            (tmp_path / name).write_text(text)
        assert gen.main(["--ucd-dir", str(tmp_path)]) == 2
        out = capsys.readouterr()
        assert out.out == "" and "error:" in out.err and "SHA-256" in out.err            # the pin refuses the file before parsing

    def test_a_missing_input_file_is_an_error(self, tmp_path, capsys):
        assert gen.main(["--ucd-dir", str(tmp_path)]) == 2 and "not found" in capsys.readouterr().err


# ───────────────────────── the checked-in table ─────────────────────────

def load_table():
    return json.loads(TABLE.read_bytes())


class TestCheckedInTable:
    def test_the_table_is_pinned_by_its_sha256(self):
        assert hashlib.sha256(TABLE.read_bytes()).hexdigest() == TABLE_SHA256

    def test_the_file_is_in_canonical_form_and_structurally_valid(self):
        raw = TABLE.read_bytes()
        t = json.loads(raw)
        assert raw == gen.serialize(t)
        gen.validate_table(t)
        assert t["schema"] == 1 and t["unicode_version"] == "17.0.0"
        assert list(t["categories"]) == ["Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"]
        assert t["contract"] == gen.CONTRACT and t["generated_by"] == "scripts/generate_identifier_ranges.py"

    def test_the_whole_code_space_is_classified_and_the_per_category_counts_are_unicode_17(self):
        t = load_table()
        n = {c: sum(hi - lo + 1 for lo, hi in rs) for c, rs in t["categories"].items()}
        assert n == {"Cc": 65, "Cf": 170, "Cn": 814730, "Co": 137468, "Cs": 2048, "Zl": 1, "Zp": 1}
        assert sum(hi - lo + 1 for lo, hi in t["forbidden"]) == 954483           # categories are disjoint and the bidi controls are all Cf

    def test_the_forbidden_facts_of_the_contract(self):
        f = load_table()["forbidden"]
        forbidden = [0x378, 0xFFFE, 0xFFFF, 0x10FFFF, 0xE000, 0xF0000, 0x10FFFD, 0x2028, 0x2029, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060, 0x202E,
                     0x85, *range(0, 0x20), *range(0x7F, 0xA0), 0x61C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xD800, 0xDFFF]
        nonchars = [*range(0xFDD0, 0xFDF0), *(p * 0x10000 + s for p in range(17) for s in (0xFFFE, 0xFFFF))]
        assert len(nonchars) == 66
        for cp in [*forbidden, *nonchars]:
            assert in_ranges(f, cp), hex(cp)

    def test_what_is_not_forbidden(self):
        t = load_table()
        f = t["forbidden"]
        allowed = [*range(0x20, 0x7F), 0xA0, *range(0x410, 0x450), *range(0x4E00, 0x9FFF), *range(0x620, 0x640), *range(0x5D0, 0x5EB),
                   *range(0x905, 0x93A), *range(0x300, 0x370), 0x93F, 0x1F600, 0x1F64F, 0x20000, 0x3000, 0x1680, *range(0x2000, 0x200B)]
        for cp in allowed:
            assert not in_ranges(f, cp), hex(cp)
        ws = t["white_space"]
        assert in_ranges(ws, 0x20) and in_ranges(ws, 0xA0) and not in_ranges(f, 0x20) and not in_ranges(f, 0xA0)       # White_Space only for the ends
        assert in_ranges(ws, 0x2028) and in_ranges(ws, 0x2029)                                                           # and now also forbidden anywhere
        assert t["bidi_controls"] == [[0x61C, 0x61C], [0x200E, 0x200F], [0x202A, 0x202E], [0x2066, 0x2069]]

    def test_the_provenance_file_matches_the_table_and_the_pinned_inputs(self):
        raw = PROVENANCE.read_bytes()
        p = json.loads(raw)
        assert raw == gen.serialize(p)
        assert p["table"]["sha256"] == TABLE_SHA256 == hashlib.sha256(TABLE.read_bytes()).hexdigest() and p["table"]["bytes"] == TABLE.stat().st_size
        assert {n: v["sha256"] for n, v in p["inputs"].items()} == gen.PINNED_SHA256
        assert p["unicode_version"] == "17.0.0" and p["ucd_base_url"] == "https://www.unicode.org/Public/17.0.0/ucd/"

    def test_the_loader_reads_it(self):
        from agentic_os import identifiers as ids
        assert ids.FILE_SHA256 == TABLE_SHA256 and ids.UNICODE_VERSION == "17.0.0"
        assert ids.is_forbidden_code_point(0x2028) and ids.is_forbidden_code_point(0xE000) and not ids.is_forbidden_code_point(0xA0)


# ───────────────────────── the real UCD (opt in: UCD_DIR) ─────────────────────────

UCD_DIR = os.environ.get("UCD_DIR")
needs_ucd = pytest.mark.skipif(not UCD_DIR, reason="set UCD_DIR to a directory to regenerate from the real Unicode 17.0.0 UCD "
                                                   "(missing files are downloaded into it from unicode.org and SHA-256 verified)")


@pytest.fixture(scope="module")
def ucd_dir():
    d = pathlib.Path(UCD_DIR)
    d.mkdir(parents=True, exist_ok=True)
    for name in gen.UCD_FILES:
        if not any((d / p).is_file() for p in (name, gen.UCD_FILES[name])):
            gen._download(name, d)
    return d


@needs_ucd
class TestRegenerate:
    def test_the_real_ucd_reproduces_the_checked_in_table_and_provenance_byte_for_byte(self, ucd_dir):
        data, prov = gen.generate(ucd_dir)
        assert data == TABLE.read_bytes() and hashlib.sha256(data).hexdigest() == TABLE_SHA256
        assert prov == PROVENANCE.read_bytes()

    def test_the_check_option_passes_on_the_table_and_fails_on_any_difference(self, ucd_dir, tmp_path):
        assert gen.main(["--ucd-dir", str(ucd_dir), "--check", str(TABLE), "--check-provenance", str(PROVENANCE)]) == 0
        mutated = tmp_path / "t.json"
        mutated.write_bytes(TABLE.read_bytes().replace(b"[8232,8232]", b"[8232,8233]", 1))
        assert mutated.read_bytes() != TABLE.read_bytes()
        assert gen.main(["--ucd-dir", str(ucd_dir), "--check", str(mutated)]) == 1
        assert gen.main(["--ucd-dir", str(ucd_dir), "--check", str(tmp_path / "missing.json")]) == 1

    def test_out_writes_the_same_bytes(self, ucd_dir, tmp_path):
        out, prov = tmp_path / "o.json", tmp_path / "p.json"
        assert gen.main(["--ucd-dir", str(ucd_dir), "--out", str(out), "--provenance-out", str(prov)]) == 0
        assert out.read_bytes() == TABLE.read_bytes() and prov.read_bytes() == PROVENANCE.read_bytes()

    def test_a_modified_input_file_is_refused_by_the_pin(self, ucd_dir, tmp_path):
        for name in gen.UCD_FILES:
            (tmp_path / name).write_bytes(gen._read_local(ucd_dir, name))
        (tmp_path / "PropList.txt").write_bytes((tmp_path / "PropList.txt").read_bytes() + b"# tampered\n")
        with pytest.raises(gen.UCDError, match="SHA-256"):
            gen.generate(tmp_path)

    def test_the_real_ucd_files_agree_with_each_other_and_with_the_standard(self, ucd_dir):
        texts, _, _ = gen.load_inputs(ucd_dir, None)
        t = gen.build_table(texts["UnicodeData.txt"], texts["PropList.txt"], texts["DerivedGeneralCategory.txt"], check_stable=True)
        assert t["unicode_version"] == "17.0.0"


# ───────────────────────── cross-check against the interpreter's unicodedata ─────────────────────────

@pytest.mark.skipif(unicodedata.unidata_version != "17.0.0",
                    reason=f"unicodedata here is Unicode {unicodedata.unidata_version}; run with a 17.0.0 interpreter (CPython 3.15) to cross-check")
def test_the_table_agrees_with_unicodedata_for_every_code_point():
    t = load_table()
    forbidden = bytearray(0x110000)
    for lo, hi in t["forbidden"]:
        forbidden[lo:hi + 1] = b"\x01" * (hi - lo + 1)
    bidi = {cp for lo, hi in t["bidi_controls"] for cp in range(lo, hi + 1)}
    white = bytearray(0x110000)
    for lo, hi in t["white_space"]:
        white[lo:hi + 1] = b"\x01" * (hi - lo + 1)
    cats = set(gen.FORBIDDEN_CATEGORIES)
    seen = set()
    for cp in range(0x110000):
        gc = unicodedata.category(chr(cp))
        seen.add(gc)
        assert bool(forbidden[cp]) == (gc in cats or cp in bidi), (hex(cp), gc)
        if gc in ("Zs", "Zl", "Zp"):
            assert white[cp], hex(cp)
        if white[cp]:
            assert gc in ("Zs", "Zl", "Zp") or cp in (9, 10, 11, 12, 13, 0x85), hex(cp)
    assert {"Cc", "Cf", "Cs", "Cn", "Co", "Zl", "Zp", "Lu", "Mn", "So"} <= seen
