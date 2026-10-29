# Identifier contract

Principal, role, app, user, tenant, grant-subject, resource, column and action names are *machine authorization keys*: the permissions plane compares them
byte for byte and grants access by them. This page states what such a name may contain, where the code-point table behind the rule comes from, and how to
regenerate and verify it.

Code: `agentic_os/identifiers.py` (the predicate), `agentic_os/identifier_ranges.json` (the table), `agentic_os/identifier_ranges.provenance.json` (its
inputs), `scripts/generate_identifier_ranges.py` (the generator). Tests: `tests/test_identifier_contract.py`, `tests/test_identifier_ranges_generator.py`.
The behaviour change for stored grants is in [PERMISSIONS_MIGRATION.md](PERMISSIONS_MIGRATION.md).

## The rules

| Rule | Allowed | Rejected |
|---|---|---|
| Empty | any non-empty `str` | `""` (the only value treated as *absent*), any non-`str` |
| Ends | no Unicode `White_Space` at the start or end | a leading or trailing space, tab, no-break space, ideographic space, U+0085, U+2028, ... |
| Control (`Cc`) | none anywhere | U+0000..U+001F, U+007F..U+009F |
| Format (`Cf`) | none anywhere | zero-width space/joiners U+200B..U+200D, U+FEFF, U+2060, soft hyphen, tag characters, ... |
| Surrogate (`Cs`) | none anywhere | U+D800..U+DFFF |
| Unassigned (`Cn`) | none anywhere, including the 66 noncharacters | U+0378, U+FDD0..U+FDEF, U+FFFE, U+FFFF, U+10FFFF, ... |
| Private use (`Co`) | none anywhere | U+E000..U+F8FF, U+F0000..U+FFFFD, U+100000..U+10FFFD |
| Line / paragraph separator (`Zl`, `Zp`) | none anywhere | U+2028, U+2029 |
| Bidirectional controls (`Bidi_Control`) | none anywhere | U+061C, U+200E, U+200F, U+202A..U+202E, U+2066..U+2069 |
| Inner spaces | ordinary space U+0020 and no-break space U+00A0 inside the name | (only at the ends are they rejected) |
| Letters, marks, symbols | non-ASCII letters (Cyrillic, CJK, Arabic, Hebrew, Devanagari, ...), combining marks (`Mn`, `Mc`, `Me`) and emoji (`So`) | (a ZWJ emoji sequence contains U+200D and is therefore rejected) |
| Case | exact comparison, no case folding | two names differing only in case are two principals |
| Normalization | none | `é` (U+00E9) and `e` + U+0301 are two different principals |

The set of rejected code points is the `forbidden` list of the table: the General_Category values `Cc`, `Cf`, `Cs`, `Cn`, `Co`, `Zl`, `Zp`, plus the
`Bidi_Control` property. `White_Space` is a separate list in the same table and is used only for the leading/trailing rule; U+0020 and U+00A0 are
`White_Space` and not `forbidden`, U+2028 and U+2029 are both. A present name that breaks a rule invalidates the whole principal (nothing is granted).

The predicate never calls the interpreter's `unicodedata`: the Unicode version of the running Python must not decide whether an identifier is valid, or two
runtimes would disagree about the same principal.

## The table

`identifier_ranges.json`, schema 1, canonical bytes (sorted keys, `,` and `:` separators without spaces, ASCII, one trailing newline):

| Key | Meaning |
|---|---|
| `schema` | `1`; the loader refuses any other value |
| `unicode_version` | `17.0.0` |
| `generated_by` | the generator's path |
| `contract` | the rule as one sentence |
| `categories` | per-category ranges for `Cc`, `Cf`, `Cn`, `Co`, `Cs`, `Zl`, `Zp` |
| `forbidden` | the merged union of `categories` and `bidi_controls` |
| `white_space` | the `White_Space` property |
| `bidi_controls` | the `Bidi_Control` property |

Every list holds inclusive `[lo, hi]` integer pairs, sorted ascending, merged where they overlap or touch. The loader reads `schema`, `unicode_version`,
`forbidden`, `white_space` and `bidi_controls` and ignores other keys. The inputs of a generation (the SHA-256 of every UCD file used, and the table's
own SHA-256) are recorded in the separate file `identifier_ranges.provenance.json`, so the table itself keeps its keys.

The table's SHA-256 is pinned in `tests/test_identifier_contract.py` and `tests/test_identifier_ranges_generator.py` (the table is pinned, not the script):

    5cb60e8d4c97612146bcd7df69a9aca0f31f1ac7f457ac97d4ae993902b8fc45

## The generator

`scripts/generate_identifier_ranges.py` is stdlib-only and deterministic. Inputs are three files of the Unicode Character Database 17.0.0
(`UnicodeData.txt`, `PropList.txt`, `extracted/DerivedGeneralCategory.txt`); their SHA-256 values are pinned in the script and checked before parsing.

```bash
python scripts/generate_identifier_ranges.py --ucd-dir DIR --out agentic_os/identifier_ranges.json \
    --provenance-out agentic_os/identifier_ranges.provenance.json
python scripts/generate_identifier_ranges.py --ucd-dir DIR --check agentic_os/identifier_ranges.json   # non-zero exit when it differs
python scripts/generate_identifier_ranges.py --cache-dir DIR      # downloads from unicode.org/Public/17.0.0/ucd/ into DIR, verifies, prints
```

The generator asserts, on every run: the ranges are sorted, non-overlapping and non-adjacent; `DerivedGeneralCategory.txt` classifies every code point
0..U+10FFFF exactly once; the unassigned code points are exactly the complement of the code points listed in `UnicodeData.txt` (with `First`/`Last` pairs
expanded) and agree with the `Cn` entries of `DerivedGeneralCategory.txt`; all 66 `Noncharacter_Code_Point` code points and all `Bidi_Control` code points are
forbidden; and a fixed list of code points is forbidden or allowed as the table above says.

Tests: a synthetic mini-UCD exercises the generator logic and malformed input; the checked-in table and provenance are validated and pinned; with
`UCD_DIR=<directory>` the real files (downloaded into it if missing, hash-verified) are regenerated and must reproduce the checked-in bytes; and on an
interpreter whose `unicodedata.unidata_version` is `17.0.0` (CPython 3.15) the whole table is compared with `unicodedata` for all 1,114,112 code points.

## Provenance

The data is derived from the public Unicode Character Database (Unicode License v3). The code is `agentic-os`'s own, under the licence in
[LICENSE.md](../LICENSE.md).

The table `agentic_os/identifier_ranges.json` is generated by `scripts/generate_identifier_ranges.py` from the Unicode Character Database 17.0.0
(<https://www.unicode.org/Public/17.0.0/ucd/>). The generator was written for this repository from the Unicode documentation (UAX #44) and the functional
specification above; it does not derive from any other generator. Its inputs are three UCD files, identified by SHA-256 (pinned in the script, verified before
parsing, and recorded in `agentic_os/identifier_ranges.provenance.json`):

| UCD file | SHA-256 |
|---|---|
| `UnicodeData.txt` | `2e1efc1dcb59c575eedf5ccae60f95229f706ee6d031835247d843c11d96470c` |
| `extracted/DerivedGeneralCategory.txt` | `d62e5bab70ca74f099343f71224fa051cb1fdd61a1ab45c0488c44cfc0b6102e` |
| `PropList.txt` | `130dcddcaadaf071008bdfce1e7743e04fdfbc910886f017d9f9ac931d8c64dd` |

The resulting table has SHA-256 `5cb60e8d4c97612146bcd7df69a9aca0f31f1ac7f457ac97d4ae993902b8fc45`. The table covers the General_Category values `Cc`, `Cf`,
`Cn`, `Co`, `Cs`, `Zl` and `Zp` plus the `Bidi_Control` property.
