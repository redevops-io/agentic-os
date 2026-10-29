"""The shared IDENTIFIER CONTRACT in the kernel plane (design decision).

An identifier (app, user, role, tenant, grant subject, resource name, owner/row column, mask column, action) is a non-empty string with no leading or
trailing Unicode White_Space and no Cc / Cf / Cs / Cn (incl. noncharacters) / Co (private use) / Zl / Zp code point or bidirectional formatting control
anywhere. NO case folding and NO Unicode
normalization: exact comparison, two visually similar ids are two principals. Combining marks, non-ASCII letters and plain emoji stay valid; private use and U+2028/U+2029 are rejected (the previous release accepted them). The
forbidden code points come from the shared generated table ``agentic_os/identifier_ranges.json`` (Unicode 17.0.0), never from ``unicodedata``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import subprocess
import sys
import unicodedata

import pytest

from agentic_os import identifiers as ids
from agentic_os.permissions import GrantError, GrantStore, Identity, PermissionsPlane, make_authorizer

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"i" * 32).decode()

REJECTED = {
    "ZWSP U+200B": "​", "ZWNJ U+200C": "‌", "ZWJ U+200D": "‍", "BOM/ZWNBSP U+FEFF": "﻿", "WORD JOINER U+2060": "⁠",
    "RLO U+202E": "‮", "NEL U+0085": "\u0085", "NUL": "\x00", "TAB": "\t", "LF": "\n", "CR": "\r", "US U+001F": "\x1f", "DEL": "\x7f",
    "C1 U+009F": "\x9f", "SOFT HYPHEN U+00AD": "­", "ALM U+061C": "؜", "LRM U+200E": "‎", "RLM U+200F": "‏",
    "LRE U+202A": "‪", "PDF U+202C": "‬", "LRI U+2066": "⁦", "PDI U+2069": "⁩", "ISOLATE U+2067": "⁧",
    "LINE SEP U+2028 (Zl)": "\u2028", "PARAGRAPH SEP U+2029 (Zp)": "\u2029",
    "private use U+E000 (Co)": "\ue000", "private use U+F8FF (Co)": "\uf8ff", "private use U+F0000 (Co)": "\U000f0000", "private use U+FFFFD (Co)": "\U000ffffd",
    "private use U+100000 (Co)": "\U00100000", "private use U+10FFFD (Co)": "\U0010fffd",
    "lone surrogate U+D800": "\ud800", "lone surrogate U+DFFF": "\udfff", "unassigned U+0378": "͸", "unassigned U+0380": "΀",
    "noncharacter U+FFFE": "￾", "noncharacter U+FFFF": "￿", "noncharacter U+FDD0": "﷐", "noncharacter U+1FFFE": "\U0001fffe",
    "noncharacter U+10FFFF": "\U0010ffff", "tag U+E0001": "\U000e0001", "tag U+E0020": "\U000e0020", "variation-free Cf U+FFF9": "￹",
    "interlinear U+FFFB": "￻", "invisible times U+2062": "⁢", "function application U+2061": "⁡", "MONGOLIAN VS U+180E": "᠎",
}

ACCEPTED = {
    "ascii": "alice", "ascii with an inner space": "alice smith", "ascii punctuation": "a.b-c_d@e:f/g", "digits": "user42", "uppercase": "ALICE",
    "latin letters": "Zoë Brontë", "precomposed é": "é", "decomposed é": "é", "greek": "Ωμέγα", "cyrillic": "Пользователь",
    "cjk": "名前", "japanese kana": "ひらがな", "korean": "사용자", "arabic": "مستخدم", "hebrew": "משתמש", "devanagari with marks": "उपयोगकर्ता",
    "thai with marks": "ผู้ใช้", "emoji": "😀", "flag (two regional indicators)": "🇺🇸", "emoji with skin tone": "👍🏽", "combining marks only after a base": "ạ̈",
    "nbsp inside": "a b", "ideographic space inside": "a　b", "max assigned plane-2 ideograph": "\U00020000",
    "one char": "x",
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return GrantStore(str(tmp_path / "p"))


# ───────────────────────── the predicate ─────────────────────────

class TestRejected:
    @pytest.mark.parametrize("name", sorted(REJECTED))
    def test_anywhere(self, name):
        c = REJECTED[name]
        for v in (c, "a" + c, c + "a", "a" + c + "b", "al" + c + "ice"):
            assert ids.valid_identifier(v) is False, (name, v)

    @pytest.mark.parametrize("ch", [" ", " ", " ", " ", " ", " ", " ", " ", "　", " ", "\u0085", "\t"])
    def test_leading_or_trailing_white_space(self, ch):
        assert not ids.valid_identifier(ch + "a") and not ids.valid_identifier("a" + ch) and not ids.valid_identifier(ch + "a" + ch)
        assert not ids.valid_identifier(ch)

    @pytest.mark.parametrize("bad", ["", None, 5, 5.0, b"alice", ["alice"], ("alice",), {"a": 1}, True, object()])
    def test_empty_and_non_strings(self, bad):
        assert ids.valid_identifier(bad) is False

    def test_a_str_subclass_is_still_judged_by_its_text(self):
        class S(str):
            pass
        assert ids.valid_identifier(S("alice")) is True and ids.valid_identifier(S("a​b")) is False

    def test_the_problem_text_never_echoes_the_value(self):
        for v in ("a​secret", " secret", ""):
            assert "secret" not in (ids.identifier_problem(v) or "")


class TestAccepted:
    @pytest.mark.parametrize("name", sorted(ACCEPTED))
    def test_valid(self, name):
        assert ids.valid_identifier(ACCEPTED[name]) is True, name

    def test_u2028_and_u2029_are_rejected_inside_as_well_as_at_the_ends(self):
        """MIGRATED: they were White_Space only (an inner U+2028 was accepted); Zl and Zp are now forbidden code points."""
        for cp in ("\u2028", "\u2029"):
            assert ids.is_forbidden_code_point(ord(cp)) and ids.is_white_space(cp)
            for v in ("a" + cp + "b", "a" + cp, cp + "a", cp, "al" + cp + "ice"):
                assert ids.valid_identifier(v) is False, ascii(v)
        assert ids.valid_identifier("a\u00a0b") is True and ids.valid_identifier("a b") is True          # ordinary and no-break spaces inside stay valid

    def test_private_use_is_rejected_anywhere(self):
        """MIGRATED: the previous release accepted private-use characters (Co)."""
        for cp in (0xE000, 0xF8FF, 0xF0000, 0xFFFFD, 0x100000, 0x10FFFD):
            for v in (chr(cp), "a" + chr(cp) + "b", "a" + chr(cp), chr(cp) + "a"):
                assert ids.valid_identifier(v) is False, hex(cp)
        assert ids.valid_identifier("\U00020000") is True and ids.valid_identifier("\ud7fb") is True and ids.valid_identifier("\uf900") is True    # neighbours


class TestNoFoldingNoNormalisation:
    def test_precomposed_and_decomposed_e_acute_are_distinct_principals(self, store):
        assert "é" != "é" and unicodedata.normalize("NFC", "é") == "é"
        store.add("role", "é", "table", "t")                                  # the grant is for the precomposed one
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(roles=["é"]), "table", "t").allowed is True
        assert plane.authorize(Identity(roles=["é"]), "table", "t").allowed is False          # same text to the eye, a different principal
        store.add("user", "é", "table", "u")
        assert plane.authorize(Identity(user="é"), "table", "u").allowed is False
        assert plane.authorize(Identity(user="é"), "table", "u").allowed is True

    def test_case_is_never_folded(self, store):
        store.add("role", "Clerk", "table", "t")
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(roles=["clerk"]), "table", "t").allowed is False
        assert plane.authorize(Identity(roles=["CLERK"]), "table", "t").allowed is False
        assert plane.authorize(Identity(roles=["Clerk"]), "table", "t").allowed is True

    def test_lookalikes_are_distinct(self, store):
        store.add("role", "admin", "table", "t")
        plane = PermissionsPlane(store)
        for look in ("аdmin", "admın", "ａdmin", "admiń"):         # Cyrillic а, dotless ı, fullwidth a, a combining mark
            assert ids.valid_identifier(look)
            assert plane.authorize(Identity(roles=[look]), "table", "t").allowed is False, look

    def test_the_own_rows_owner_match_is_exact(self, store):
        store.add("role", "r", "table", "t", row_scope="own", row_column="owner")
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(user="é", roles=["r"]), "table", "t")
        assert plane.row_allowed(d, {"owner": "é"}) is True and plane.row_allowed(d, {"owner": "é"}) is False
        assert plane.row_allowed(d, {"owner": "É"}) is False


# ───────────────────────── the principal ─────────────────────────

class TestPrincipal:
    @pytest.mark.parametrize("field", ["app", "user"])
    @pytest.mark.parametrize("name", sorted(REJECTED))
    def test_a_present_invalid_app_or_user_invalidates_the_principal_and_nothing_is_granted(self, store, field, name):
        store.add("role", "admin-like", "table", "t")
        store.add("user", "alice", "table", "t")
        store.add("app", "billing", "table", "t")
        evil = {"alice": "alice", "billing": "billing"}
        who = Identity(**{field: ("alice" if field == "user" else "billing") + REJECTED[name]}, roles=["admin"])     # even with a privileged role
        assert who.invalid_reason() is not None
        d = PermissionsPlane(store).authorize(who, "table", "t")
        assert d.allowed is False and d.reason.startswith("denied: invalid principal")

    @pytest.mark.parametrize("name", sorted(REJECTED))
    def test_an_invalid_role_invalidates_the_principal_no_admin_bypass_no_partial_roles(self, store, name):
        store.add("role", "clerk", "table", "t")
        who = Identity(user="u", roles=["clerk", "admin" + REJECTED[name]])
        assert who.invalid_reason() == "roles is not a valid role collection"
        assert PermissionsPlane(store).authorize(who, "table", "t").allowed is False          # "clerk" alone would have been allowed

    def test_padded_names_are_invalid_not_trimmed(self, store):
        store.add("user", "alice", "table", "t")
        for v in (" alice", "alice ", " alice", "alice　"):
            assert Identity(user=v).invalid_reason() == "user is not a valid identifier"
            assert PermissionsPlane(store).authorize(Identity(user=v), "table", "t").allowed is False

    def test_only_the_empty_string_is_absent_white_space_only_is_a_present_invalid_value(self, store):
        assert Identity(app="", user="", roles=[]).invalid_reason() is None
        store.add("role", "clerk", "table", "t")
        for v in ("   ", "\u3000", "\t\n", "\u00a0"):
            assert Identity(app=v).invalid_reason() == "app is not a valid identifier"
            assert Identity(user=v).invalid_reason() == "user is not a valid identifier"
            assert PermissionsPlane(store).authorize(Identity(user=v, roles=["clerk"]), "table", "t").allowed is False
        assert PermissionsPlane(store).authorize(Identity(user="", roles=["clerk"]), "table", "t").allowed is True

    def test_international_names_work_end_to_end(self, store):
        store.add("user", "名前", "table", "t")
        store.add("role", "مدير", "table", "t")
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(user="名前"), "table", "t").allowed is True
        assert plane.authorize(Identity(roles=["مدير"]), "table", "t").allowed is True
        assert plane.authorize(Identity(user="👍🏽"), "table", "t").allowed is False

    def test_the_gate_applies_the_same_rule(self, store):
        store.add("role", "clerk", "table", "t")
        auth = make_authorizer(store, tool_resources={"tool": ("table", "t")}, tool_actions={"tool": "read"})
        spec = type("S", (), {"name": "tool", "side_effecting": False})()
        ok = type("P", (), {"app": "", "user": "u", "roles": ["clerk"]})()
        bad = type("P", (), {"app": "", "user": "u", "roles": ["clerk​"]})()
        assert auth(ok, spec, {}) is None and auth(bad, spec, {}) is not None

    def test_a_request_for_a_resource_with_an_invalid_name_is_a_malformed_request(self, store):
        store.add("role", "clerk", "table", "t")
        d = PermissionsPlane(store).authorize(Identity(roles=["clerk"]), "table", "t​")
        assert d.allowed is False and "malformed request" in d.reason


# ───────────────────────── grants ─────────────────────────

BAD = "​"


class TestGrantNames:
    @pytest.mark.parametrize("kw", [
        dict(subject="role" + BAD), dict(resource_name="t" + BAD), dict(subject=" role"), dict(resource_name="t "),
        dict(actions=["read" + BAD]), dict(actions=["‮read"]), dict(masked_columns=["email" + BAD]), dict(masked_columns=[" email"]),
        dict(row_scope="own", row_column="owner" + BAD), dict(row_scope="in", row_column="region‮", row_values=["eu"]),
    ])
    def test_a_grant_with_such_a_name_is_refused_whole(self, store, kw):
        args = dict(subject="clerk", resource_name="t")
        args.update({k: v for k, v in kw.items() if k in ("subject", "resource_name")})
        extra = {k: v for k, v in kw.items() if k not in ("subject", "resource_name")}
        with pytest.raises(GrantError):
            store.add("role", args["subject"], "table", args["resource_name"], **extra)
        assert store.list() == []

    def test_row_values_are_data_not_identifiers_and_stay_exact(self, store):
        store.add("role", "clerk", "table", "t", row_scope="in", row_column="region", row_values=["éu"])
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(roles=["clerk"]), "table", "t")
        assert plane.row_allowed(d, {"region": "éu"}) is True and plane.row_allowed(d, {"region": "éu"}) is False

    def test_stored_grants_with_such_names_become_unusable_and_are_counted_as_malformed(self, store):
        good = store.add("role", "clerk", "table", "t")
        # plant entries the way an older release (which accepted them) would have stored them
        doc = store._load_raw()
        planted = []
        for i, mut in enumerate([dict(subject="clerk" + BAD), dict(resource_name="t" + BAD), dict(actions=["read" + BAD]),
                                 dict(masked_columns=["email" + BAD]), dict(row_scope="own", row_column="owner‮")]):
            d = dict(good.to_dict(), id=f"g-planted{i:02d}", **mut)
            planted.append(d)
        store._write(doc.items + planted, doc.meta)
        st = store.status()
        assert st["malformed"] == 5 and st["count"] == 1 and st["sealed"] is False
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(roles=["clerk"]), "table", "t").allowed is True            # the good grant is untouched
        assert plane.authorize(Identity(roles=["clerk" + BAD]), "table", "t").allowed is False
        assert sorted(g.id for g in store.list()) == [good.id]
        assert all(store.remove(f"g-planted{i:02d}") for i in range(5))                              # an admin can still remove them by id
        assert store.status()["malformed"] == 0

    def test_a_valid_international_grant_round_trips(self, store):
        g = store.add("role", "管理者", "table", "顧客", actions=["read"], masked_columns=["メール"], row_scope="in", row_column="地域", row_values=["東京"])
        again = GrantStore(store.dir).list()
        assert [x.id for x in again] == [g.id] and again[0].subject == "管理者" and again[0].masked_columns == ["メール"]


# ───────────────────────── the table ─────────────────────────

class TestSharedTable:
    def test_it_is_the_one_accepted_table_unicode_17_pinned_by_its_sha256(self):
        raw = (ROOT / "agentic_os" / "identifier_ranges.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ids.FILE_SHA256 == "5cb60e8d4c97612146bcd7df69a9aca0f31f1ac7f457ac97d4ae993902b8fc45"
        t = json.loads(raw)
        assert ids.UNICODE_VERSION == t["unicode_version"] == "17.0.0" and t["schema"] == 1
        assert set(t["categories"]) == {"Cc", "Cf", "Cs", "Cn", "Co", "Zl", "Zp"} and set(t) >= {"forbidden", "white_space", "bidi_controls"}

    def test_the_generator_is_the_kernel_owned_one_and_it_is_the_only_identifier_generator(self):
        """The table is pinned by its SHA-256 (above), not the generator by its hash; tests/test_identifier_ranges_generator.py regenerates it
        (UCD_DIR) and checks it against unicodedata. Here: the kernel generator exists, carries the category switch, and is the only identifier generator."""
        src = (ROOT / "scripts" / "generate_identifier_ranges.py").read_text()
        assert 'FORBIDDEN_CATEGORIES = ("Cc", "Cf", "Cs", "Cn", "Co", "Zl", "Zp")' in src
        assert sorted(p.name for p in (ROOT / "scripts").glob("*identifier*")) == ["generate_identifier_ranges.py"]
        assert (ROOT / "agentic_os" / "identifier_ranges.provenance.json").is_file()

    def test_the_bidi_controls_are_exactly_the_contract_list(self):
        assert ids.BIDI_CONTROLS == {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
        for cp in ids.BIDI_CONTROLS:
            assert ids.is_forbidden_code_point(cp)

    def test_the_module_never_asks_unicodedata_so_the_interpreters_unicode_version_cannot_decide(self):
        src = (ROOT / "agentic_os" / "identifiers.py").read_text()
        assert "import unicodedata" not in src and "unicodedata." not in src.replace("``unicodedata``", "")

    def test_the_table_covers_every_control_format_and_surrogate_python_knows(self):
        for cp in range(0x110000):
            if unicodedata.category(chr(cp)) in ("Cc", "Cf", "Cs"):
                assert ids.is_forbidden_code_point(cp), hex(cp)

    def test_noncharacters_and_unassigned_ranges_are_in_the_table(self):
        for cp in [*range(0xFDD0, 0xFDF0), *(p * 0x10000 + s for p in range(17) for s in (0xFFFE, 0xFFFF)), 0x378, 0x3FFFE, 0xE0FFF]:
            assert ids.is_forbidden_code_point(cp), hex(cp)

    def test_every_assigned_letter_mark_number_punctuation_and_symbol_python_knows_is_still_allowed(self):
        """A character Python (an older Unicode) calls L/M/N/P/S is accepted by the table unless Unicode 17 made it a control; none of those exist."""
        for cp in range(0x110000):
            if unicodedata.category(chr(cp))[0] in "LMNPS" and cp not in ids.BIDI_CONTROLS:
                assert not ids.is_forbidden_code_point(cp), hex(cp)

    def test_private_use_line_and_paragraph_separators_are_in_the_table(self):
        for cp in (0xE000, 0xF8FF, 0xF0000, 0xFFFFD, 0x100000, 0x10FFFD, 0x2028, 0x2029):
            assert ids.is_forbidden_code_point(cp), hex(cp)
        assert not ids.is_forbidden_code_point(0xF900) and not ids.is_forbidden_code_point(0x202F) and not ids.is_forbidden_code_point(0x2027)

    def test_white_space_is_the_unicode_property_not_pythons_isspace(self):
        assert ids.is_white_space(" ") and ids.is_white_space("　") and ids.is_white_space("\u0085")
        assert not ids.is_white_space("​") and not ids.is_white_space("﻿") and not ids.is_white_space("᠎")
        assert "\x1c".isspace() and not ids.is_white_space("\x1c")      # Python's isspace is a different set; this module does not use it

    def test_the_wheel_ships_the_table(self):
        assert "agentic_os/identifier_ranges.json" in (ROOT / "pyproject.toml").read_text()


# ───────────────────────── tools that take a name ─────────────────────────

def test_the_quickstart_refuses_an_invalid_user_name(tmp_path):
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", "init", "--dir", str(tmp_path / "p"), "--user", "alice​"],
                       env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert r.returncode != 0 and not (tmp_path / "p" / "grants.enc").exists()


def test_the_migration_guide_documents_the_contract_and_the_breaking_change():
    t = (ROOT / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    for needle in ("identifier contract", "identifier_ranges.json", "Unicode 17.0.0", "malformed", "no case folding", "normalization"):
        assert needle.lower() in t.lower(), needle
