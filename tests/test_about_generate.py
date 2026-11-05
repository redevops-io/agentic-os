"""About record generator, schema validator, consistency check and the legal-slot matrix (packaging/notices/generate_about.py,
check_about.py, agentic_os/about_record.py, packaging/check-distribution-gate.py `slot_texts`).

Offline. Everything runs against a synthetic repository root; the committed gate file is only READ (all decisions stay OPEN).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import (ALL_SLOT_DECISIONS, NOTICES, ROOT, SLOTS, SYNTH, about_record, chk_about, decide, gate, gen_about,
                            make_notices, make_root, read_gate, run_generate, stage_folder, write_gate)

FORBIDDEN = ("open source", "cleared", "compliant", "approved by counsel", "legally")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)
    root = make_root(tmp_path)
    return root, make_notices(tmp_path)


def _record(notices: Path) -> dict:
    return json.loads((notices / "about.json").read_bytes())


# ------------------------------------------------------------------------------------------- the committed gate file
def test_committed_gate_has_every_slot_decision_and_all_stay_open():
    ids = {d["id"] for d in json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())["decisions"]}
    assert set(ALL_SLOT_DECISIONS) <= ids
    for d in json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())["decisions"]:
        assert d["status"] == "OPEN" and not d.get("slot_texts"), d["id"]      # this change decides nothing and writes no text


def test_slot_tables_agree_between_gate_record_validator_and_tests():
    assert {s: list(v["decisions"]) for s, v in gate.ABOUT_SLOTS.items()} == SLOTS == about_record.SLOT_DECISIONS
    assert [s for s, v in gate.ABOUT_SLOTS.items() if v["omittable"]] == list(about_record.OMITTABLE_SLOTS)


def test_gate_json_documents_slot_texts():
    doc = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())
    assert "slot_texts" in doc.get("slot_texts_schema", "")


# ------------------------------------------------------------------------------------------- generator: shape
def test_generated_record_is_valid_deterministic_ascii_and_free_of_paths(env, monkeypatch):
    root, notices = env
    assert run_generate(root, notices) == 0
    raw1 = (notices / "about.json").read_bytes()
    assert run_generate(root, notices) == 0
    assert (notices / "about.json").read_bytes() == raw1                      # same inputs, same bytes
    raw1.decode("ascii")
    doc = json.loads(raw1)
    assert about_record.validate_record(doc) == []
    assert doc["schema"] == "redevops.about/1" and doc["build"]["mode"] == "private-candidate"
    text = raw1.decode()
    for needle in ("/Users/", "/private/", "/home/", "wt-", "C:\\", str(root), str(notices)):
        assert needle not in text, needle
    # no wall-clock time: the only timestamp is the commit time of the fixture commit, not "now"
    assert re.search(r'"commit_time_utc": "20\d\d-\d\d-\d\dT', text)
    ident = json.loads((ROOT / "packaging" / "identity.json").read_text())["r0prime"]
    assert doc["product"]["name"] == ident["product_name"] and doc["product"]["version"] == ident["version"]
    assert doc["build"]["ui_bundle"]["id"] == "index-TESTBUNDLE1.js"


def test_top_level_and_section_fields_are_exactly_the_allowlist(env):
    root, notices = env
    run_generate(root, notices)
    doc = _record(notices)
    assert set(doc) == {"schema", "product", "build", "licence_files", "notices", "legal", "source_access", "ui"}
    assert set(doc["build"]) == {"mode", "platform", "source", "ui_bundle", "inputs"}
    assert set(doc["build"]["inputs"]) == {"sbom_sha256", "gate_file_sha256"}          # no hash of the containing artifact
    assert set(doc["notices"]["components"][0]) == {"n", "name", "version", "declared", "type", "status", "offset", "length", "files"}
    assert set(doc["legal"]["slots"]) == set(SLOTS)


def test_offsets_point_at_the_component_headings_of_the_notices_file(env):
    root, notices = env
    run_generate(root, notices)
    doc = _record(notices)
    raw = (notices / "THIRD_PARTY_NOTICES.txt").read_bytes()
    sep = b"=" * 72 + b"\n"
    comps = doc["notices"]["components"]
    assert [c["name"] for c in comps] == ["alpha", "gamma"] and doc["notices"]["missing"] == [{"name": "gamma", "version": "3.0"}]
    for c in comps:
        assert raw[c["offset"]:].startswith(sep + f"[{c['n']:03d}] {c['name']} {c['version']}\n".encode())
    ref = doc["notices"]["reference_notices"][0]
    assert raw[ref["offset"]:].startswith(sep + b"[R01] Lib X (synthetic)\n")
    # component sections tile the file from the first heading to the REFERENCE NOTICES block; reference sections end at the file end
    for a, b in zip(comps, comps[1:]):
        assert a["offset"] + a["length"] == b["offset"]
    assert comps[-1]["offset"] + comps[-1]["length"] == raw.index(sep + b"REFERENCE NOTICES\n")
    assert ref["offset"] + ref["length"] == len(raw)
    assert doc["notices"]["sha256"] == hashlib.sha256(raw).hexdigest() and doc["notices"]["bytes"] == len(raw)


def test_licence_files_are_the_product_files_plus_the_gaps_list(env):
    root, notices = env
    run_generate(root, notices)
    files = {f["file"]: f for f in _record(notices)["licence_files"]}
    assert set(files) == {"LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "NOTICES_MISSING.md"}
    for name, f in files.items():
        src = (root / name) if (root / name).is_file() else notices / name
        assert f["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest() and f["bytes"] == src.stat().st_size


def test_non_ascii_and_control_characters_in_names_are_escaped_not_copied():
    assert gen_about.ascii_safe("caf\u00e9\x07x") == "caf\\xe9 x"
    assert gen_about.ascii_safe("a" * 500, 10) == "a" * 10


def test_source_facts_come_from_git_not_from_the_clock(env):
    root, notices = env
    run_generate(root, notices)
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert _record(notices)["build"]["source"]["commit"] == commit


# ------------------------------------------------------------------------------------------- generator: slot matrix
def _all_placeholders(doc):
    return all(s["state"] == "placeholder" for s in doc["legal"]["slots"].values())


def test_private_build_with_every_decision_open_has_neutral_placeholders(env):
    root, notices = env
    assert run_generate(root, notices) == 0
    doc = _record(notices)
    assert _all_placeholders(doc) and doc["source_access"] == {"state": "not-published"}
    for sid, ids in SLOTS.items():
        s = doc["legal"]["slots"][sid]
        assert s["pending"] == ids and s["decisions"] == ids
        assert s["text"] == f"Legal text pending decision {', '.join(ids)}. This is a private candidate; nothing here is a legal " \
                            "statement or clearance."
        assert "private candidate" in s["text"] and "pending" in s["text"]
        low = s["text"].lower()
        assert not [w for w in FORBIDDEN if w in low]


def test_open_decision_never_opens_the_text_file(env):
    """Even when a text file is named on the entry (bypassing the gate validator), an OPEN slot never reads it."""
    root, notices = env
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] == "D-LIC-10":
            d["slot_texts"] = {"LEGAL.NO_WARRANTY": {"file": "docs/legal/approved/draft.en.txt", "sha256": "0" * 64}}
    reads = []
    slots, errs, refusals = gate.resolve_about_slots(doc["decisions"], root, False, read=lambda p: reads.append(p) or b"x")
    assert reads == [] and errs == [] and slots["LEGAL.NO_WARRANTY"]["state"] == "placeholder"
    # the gate validator refuses that file outright (an OPEN entry carries no slot_texts)
    assert any("OPEN but carries" in e for e in gate.validate(doc["decisions"], root))


@pytest.mark.parametrize("slot", sorted(SLOTS))
@pytest.mark.parametrize("dist", [False, True], ids=["private", "distribution"])
def test_decided_slot_with_a_matching_approved_text_is_filled(env, slot, dist):
    root, notices = env
    decide(root, [i for i in SLOTS[slot]] + ALL_SLOT_DECISIONS)     # everything decided -> every slot is filled
    assert run_generate(root, notices, distribution=dist) == 0
    doc = _record(notices)
    s = doc["legal"]["slots"][slot]
    assert s["state"] == "filled" and s["pending"] == [] and s["text"] == SYNTH.format(slot=slot)
    assert s["text_sha256"] == hashlib.sha256(s["text"].encode()).hexdigest()
    assert doc["build"]["mode"] == ("distribution" if dist else "private-candidate")
    assert doc["source_access"] == {"state": "offer-recorded"}
    assert about_record.validate_record(doc) == []


@pytest.mark.parametrize("dist", [False, True], ids=["private", "distribution"])
@pytest.mark.parametrize("bad", ["none", "missing", "mismatch"])
@pytest.mark.parametrize("slot", sorted(SLOTS))
def test_decided_slot_with_a_missing_or_wrong_text_fails_in_every_mode(env, slot, bad, dist):
    root, notices = env
    others = [i for i in ALL_SLOT_DECISIONS if i not in SLOTS[slot]]
    decide(root, others)
    decide(root, SLOTS[slot], texts=bad)
    (notices / "about.json").write_text("stale")
    assert run_generate(root, notices, distribution=dist) == 2                 # error, not a placeholder, not exit 3
    assert not (notices / "about.json").exists()                                # nothing (and no stale record) is left behind


@pytest.mark.parametrize("slot", sorted(SLOTS))
def test_distribution_build_with_an_open_slot_fails_with_exit_3_and_writes_nothing(env, slot, capsys):
    root, notices = env
    open_ids = SLOTS[slot]
    decide(root, [i for i in ALL_SLOT_DECISIONS if i not in open_ids])
    (notices / "about.json").write_text("stale private record")
    assert run_generate(root, notices, distribution=True) == 3
    assert not (notices / "about.json").exists()
    assert slot in capsys.readouterr().err
    # the same inputs are fine for a private candidate (placeholder for that slot only)
    assert run_generate(root, notices) == 0
    doc = _record(notices)
    assert doc["legal"]["slots"][slot]["state"] == "placeholder"
    assert sum(s["state"] == "placeholder" for s in doc["legal"]["slots"].values()) == sum(
        1 for ids in SLOTS.values() if set(ids) & set(open_ids))


def test_partly_decided_multi_decision_slot_stays_a_placeholder_naming_only_the_open_ones(env):
    root, notices = env
    decide(root, ["D-LIC-1"])                                  # LICENCE_STATEMENT also needs D-LIC-15
    reads = []
    doc = read_gate(root)
    slots, errs, refusals = gate.resolve_about_slots(doc["decisions"], root, True, read=lambda p: reads.append(p) or p.read_bytes())
    assert errs == [] and slots["LEGAL.LICENCE_STATEMENT"]["state"] == "placeholder"
    assert slots["LEGAL.LICENCE_STATEMENT"]["pending"] == ["D-LIC-15"] and "D-LIC-15" in slots["LEGAL.LICENCE_STATEMENT"]["text"]
    assert "D-LIC-1," not in slots["LEGAL.LICENCE_STATEMENT"]["text"] and "D-LIC-1." not in slots["LEGAL.LICENCE_STATEMENT"]["text"]
    assert slots["LEGAL.COPYRIGHT_LINE"]["state"] == "filled"
    assert all("licence_statement" not in str(p).lower() for p in reads)      # the partly decided slot's file is not read
    assert any("LEGAL.LICENCE_STATEMENT" in r for r in refusals)


def test_section13_may_be_recorded_as_not_applicable_and_is_then_not_rendered(env):
    root, notices = env
    decide(root, ALL_SLOT_DECISIONS, texts="na")
    assert run_generate(root, notices, distribution=True) == 0
    s = _record(notices)["legal"]["slots"]["LEGAL.SECTION13_NOTICE"]
    assert s == {"state": "not-applicable", "decisions": ["D-LIC-5"], "pending": [], "text": ""}
    # only the omittable slot may be recorded that way
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] == "D-LIC-10":
            d["slot_texts"] = {"LEGAL.NO_WARRANTY": {"not_applicable": True}}
    assert any("not_applicable" in e for e in gate.validate(doc["decisions"], root))


@pytest.mark.parametrize("patch,needle", [
    (lambda st: st["LEGAL.NO_WARRANTY"].update(file="../outside.txt"), "must match"),
    (lambda st: st["LEGAL.NO_WARRANTY"].update(file="/etc/hosts"), "must match"),
    (lambda st: st["LEGAL.NO_WARRANTY"].update(file="docs/other/x.txt"), "must match"),
    (lambda st: st["LEGAL.NO_WARRANTY"].update(file="docs/legal/approved/sub/x.txt"), "must match"),
    (lambda st: st["LEGAL.NO_WARRANTY"].update(sha256="ABC"), "sha256"),
    (lambda st: st["LEGAL.NO_WARRANTY"].update(extra=1), "exactly the keys"),
    (lambda st: st.update({"LEGAL.UNKNOWN": {"file": "docs/legal/approved/x.txt", "sha256": "0" * 64}}), "unknown slot"),
])
def test_malformed_slot_texts_are_refused_by_the_gate_validator(env, patch, needle):
    root, _ = env
    decide(root, ["D-LIC-10"])
    doc = read_gate(root)
    entry = next(d for d in doc["decisions"] if d["id"] == "D-LIC-10")
    patch(entry["slot_texts"])
    assert any(needle in e for e in gate.validate(doc["decisions"], root))


def test_slot_texts_on_a_non_primary_decision_is_refused(env):
    root, _ = env
    decide(root, ["D-LIC-1", "D-LIC-15"])
    doc = read_gate(root)
    d15 = next(d for d in doc["decisions"] if d["id"] == "D-LIC-15")
    d15["slot_texts"] = copy.deepcopy(next(d for d in doc["decisions"] if d["id"] == "D-LIC-1")["slot_texts"])
    assert any("primary decision" in e for e in gate.validate(doc["decisions"], root))


def test_approved_text_must_be_utf8_lf_small_and_non_empty(env):
    root, _ = env
    for body, why in ((b"", "empty"), (b"a\r\nb\n", "control"), (b"\xff\xfe", "UTF-8"), (b"x" * 17000, "larger"),
                      (b"bell\x07\n", "control")):
        decide(root, ["D-LIC-10"])
        f = root / "docs/legal/approved/legal-no-warranty.en.txt"
        f.write_bytes(body)
        doc = read_gate(root)
        next(d for d in doc["decisions"] if d["id"] == "D-LIC-10")["slot_texts"]["LEGAL.NO_WARRANTY"]["sha256"] = \
            hashlib.sha256(body).hexdigest()
        slots, errs, _ = gate.resolve_about_slots(doc["decisions"], root, False)
        assert errs and "LEGAL.NO_WARRANTY" in errs[0], why


def test_a_decision_missing_from_the_gate_file_is_an_error_not_a_placeholder(env):
    root, _ = env
    doc = read_gate(root)
    doc["decisions"] = [d for d in doc["decisions"] if d["id"] != "pins-03"]
    slots, errs, refusals = gate.resolve_about_slots(doc["decisions"], root, False)
    assert any("pins-03 is not in the gate file" in e for e in errs)


def test_decided_but_incomplete_entry_is_an_error_in_every_mode(env):
    root, notices = env
    decide(root, ["D-LIC-10"])
    doc = read_gate(root)
    next(d for d in doc["decisions"] if d["id"] == "D-LIC-10")["decision_sha256"] = "0" * 64
    write_gate(root, doc)
    assert run_generate(root, notices) == 2


# ------------------------------------------------------------------------------------------- distribution detection
@pytest.mark.parametrize("value,dist", [("", False), ("0", False), (" 0 ", False), ("1", True), ("true", True), ("false", True),
                                        ("no", True), ("off", True), ("2", True)])
def test_environment_switch_fails_closed(env, monkeypatch, value, dist):
    root, notices = env
    monkeypatch.setenv("REDEVOPS_DISTRIBUTION_BUILD", value)
    rc = gen_about.main(["--notices-dir", str(notices), "--platform", "macos-arm64", "--root", str(root)])
    assert rc == (3 if dist else 0)


def test_the_committed_gate_refuses_a_distribution_about_record_today(tmp_path, monkeypatch):
    """Real gate file (every decision OPEN): distribution = exit 3; private = placeholders. Uses the real repository root."""
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)
    notices = make_notices(tmp_path)
    out = tmp_path / "real-out"
    assert gen_about.main(["--notices-dir", str(notices), "--out", str(out), "--platform", "macos-arm64", "--distribution"]) == 3
    assert not (out / "about.json").exists()
    assert gen_about.main(["--notices-dir", str(notices), "--out", str(out), "--platform", "macos-arm64"]) == 0
    assert _all_placeholders(json.loads((out / "about.json").read_bytes()))


def test_gate_cli_about_slots_flag(tmp_path):
    base = [sys.executable, "-I", str(ROOT / "packaging" / "check-distribution-gate.py"), "--platform", "macos", "--about-slots"]
    env_ = {"PATH": "/usr/bin:/bin"}
    r = subprocess.run(base, capture_output=True, text=True, env=env_)
    assert r.returncode == 0 and "5 of 5 are placeholders" in r.stdout
    r = subprocess.run(base + ["--distribution"], capture_output=True, text=True, env=env_)
    assert r.returncode == 1 and "REFUSED" in r.stderr


def test_gate_cli_all_decided_with_texts_passes_and_without_texts_is_a_slot_error(tmp_path):
    root = make_root(tmp_path)
    ids = [d["id"] for d in read_gate(root)["decisions"] if d["blocks_distribution"]] + ALL_SLOT_DECISIONS
    decide(root, sorted(set(ids)))
    base = [sys.executable, "-I", str(root / "packaging" / "check-distribution-gate.py"), "--platform", "macos", "--about-slots",
            "--gate", str(root / "packaging" / "distribution-gate.json"), "--root", str(root)]
    env_ = {"PATH": "/usr/bin:/bin"}
    r = subprocess.run(base + ["--distribution"], capture_output=True, text=True, env=env_)
    assert r.returncode == 0, r.stderr
    f = root / "docs/legal/approved/legal-no-warranty.en.txt"
    f.write_text("edited\n")
    r = subprocess.run(base, capture_output=True, text=True, env=env_)
    assert r.returncode == 2 and "ABOUT SLOT PROBLEM" in r.stderr             # private mode too


# ------------------------------------------------------------------------------------------- schema validator
@pytest.fixture()
def good(env):
    root, notices = env
    run_generate(root, notices)
    return _record(notices)


def _mut(doc, fn):
    d = copy.deepcopy(doc)
    fn(d)
    return d


@pytest.mark.parametrize("name,fn", [
    ("extra top-level field", lambda d: d.update(extra=1)),
    ("missing field", lambda d: d.pop("ui")),
    ("wrong schema", lambda d: d.update(schema="redevops.about/2")),
    ("extra product field", lambda d: d["product"].update(host="x")),
    ("wrong type", lambda d: d["product"].update(version=1)),
    ("bad version", lambda d: d["product"].update(version="v1;rm")),
    ("path in a name", lambda d: d["notices"]["components"][0].update(name="/Users/me/x")),
    ("worktree name", lambda d: d["notices"]["components"][0].update(name="wt-rc3")),
    ("windows path", lambda d: d["notices"]["components"][0].update(declared="C:\\Users\\me")),
    ("non-ASCII", lambda d: d["product"].update(name="Agentic \u00e9")),
    ("bad hash", lambda d: d["licence_files"][0].update(sha256="xyz")),
    ("upper-case hash", lambda d: d["licence_files"][0].update(sha256="A" * 64)),
    ("bad slug", lambda d: d["licence_files"][0].update(id="../etc")),
    ("reserved slug", lambda d: d["licence_files"][0].update(id="notices")),
    ("bad file name", lambda d: d["licence_files"][0].update(file="../LICENSE")),
    ("duplicate slug", lambda d: d["licence_files"][1].update(id=d["licence_files"][0]["id"])),
    ("bad mode", lambda d: d["build"].update(mode="release")),
    ("bad commit", lambda d: d["build"]["source"].update(commit="abc")),
    ("bad time", lambda d: d["build"]["source"].update(commit_time_utc="yesterday")),
    ("negative dirty", lambda d: d["build"]["source"].update(dirty_files=-1)),
    ("boolean as number", lambda d: d["notices"].update(bytes=True)),
    ("offset beyond file", lambda d: d["notices"]["components"][0].update(offset=10 ** 9)),
    ("overlapping sections", lambda d: d["notices"]["components"][1].update(offset=d["notices"]["components"][0]["offset"])),
    ("component count", lambda d: d["notices"].update(component_count=99)),
    ("component position", lambda d: d["notices"]["components"][0].update(n=7)),
    ("bad status", lambda d: d["notices"]["components"][0].update(status="cleared")),
    ("unknown slot", lambda d: d["legal"]["slots"].update({"LEGAL.EXTRA": d["legal"]["slots"]["LEGAL.NO_WARRANTY"]})),
    ("missing slot", lambda d: d["legal"]["slots"].pop("LEGAL.NO_WARRANTY")),
    ("slot decisions altered", lambda d: d["legal"]["slots"]["LEGAL.NO_WARRANTY"].update(decisions=["D-LIC-99"])),
    ("placeholder without wording", lambda d: d["legal"]["slots"]["LEGAL.NO_WARRANTY"].update(text="Some text")),
    ("placeholder without private-candidate wording", lambda d: d["legal"]["slots"]["LEGAL.NO_WARRANTY"].update(
        text="Legal text pending decision D-LIC-10.")),
    ("placeholder in a distribution record", lambda d: d["build"].update(mode="distribution")),
    ("filled slot with bad text hash", lambda d: d["legal"]["slots"]["LEGAL.NO_WARRANTY"].update(
        state="filled", pending=[], text="x", text_sha256="0" * 64)),
    ("non-omittable slot not-applicable", lambda d: d["legal"]["slots"]["LEGAL.NO_WARRANTY"].update(
        state="not-applicable", pending=[], text="")),
    ("source access without offer", lambda d: d["source_access"].update(state="offer-recorded")),
    ("url field", lambda d: d["source_access"].update(location="https://example.invalid/x")),
    ("ui entry off", lambda d: d["ui"].update(entry=False)),
    ("locales", lambda d: d["ui"].update(locales=["en", "de"])),
])
def test_validator_rejects(good, name, fn):
    assert about_record.validate_record(good) == []
    assert about_record.validate_record(_mut(good, fn)), name


def test_parse_record_rejects_oversize_non_ascii_duplicates_and_nan(good):
    ok = about_record.canonical_bytes(good)
    assert about_record.parse_record(ok)[0] is not None
    assert about_record.parse_record(b"x" * (about_record.MAX_BYTES_READ + 1))[0] is None
    assert about_record.parse_record(ok.replace(b'"schema"', '"sch\u00e9ma"'.encode()))[0] is None
    assert about_record.parse_record(b'{"schema": "a", "schema": "b"}')[0] is None
    assert about_record.parse_record(b'{"schema": NaN}')[0] is None
    assert about_record.parse_record(b"[" * 100000)[0] is None
    assert about_record.parse_record(b"not json")[0] is None


def test_generator_refuses_to_write_an_oversize_record(env, monkeypatch):
    root, notices = env
    monkeypatch.setattr(about_record, "MAX_BYTES_WRITE", 1000)
    # the generator loads its own copy of the module from the root; patch the file it reads instead
    p = root / "agentic_os" / "about_record.py"
    p.write_text(p.read_text().replace("MAX_BYTES_WRITE = 1024 * 1024", "MAX_BYTES_WRITE = 1000"))
    assert run_generate(root, notices) == 2
    assert not (notices / "about.json").exists()


# ------------------------------------------------------------------------------------------- forbidden words
def test_generated_record_and_placeholders_contain_no_forbidden_word(env):
    root, notices = env
    run_generate(root, notices)
    doc = _record(notices)
    blob = " ".join(about_record.record_strings(doc)).lower()
    assert not [w for w in FORBIDDEN if w in blob]
    assert about_record.forbidden_in("This is an Open Source thing") == ["open source"]
    # the checker lints product strings too
    doc["product"]["name"] = "Cleared product"
    assert about_record.forbidden_in(doc["product"]["name"]) == ["cleared"]


# ------------------------------------------------------------------------------------------- check_about
def _staged(root, notices, tmp):
    run_generate(root, notices)
    return stage_folder(root, notices, tmp / "licenses")


def test_check_about_passes_on_a_consistent_folder_and_its_copies(env, tmp_path):
    root, notices = env
    lic = _staged(root, notices, tmp_path)
    copy_ = tmp_path / "copy-about.json"
    copy_.write_bytes((lic / "about.json").read_bytes())
    assert chk_about.check(lic, root=root, notices_report=notices / "notices-report.json", copies=(copy_,)) == []


def test_stage_lists_about_json_in_the_manifest(env, tmp_path):
    root, notices = env
    lic = _staged(root, notices, tmp_path)
    man = json.loads((lic / "LICENSES-MANIFEST.json").read_text())["files"]
    assert man["about.json"]["sha256"] == hashlib.sha256((lic / "about.json").read_bytes()).hexdigest()


@pytest.mark.parametrize("what", ["about", "licence", "notices", "copy", "gate", "identity", "slot", "words", "report"])
def test_check_about_catches_drift(env, tmp_path, what):
    root, notices = env
    lic = _staged(root, notices, tmp_path)
    kw = {"notices_report": notices / "notices-report.json"}
    if what == "about":
        d = json.loads((lic / "about.json").read_bytes())
        d["product"]["version"] = "9.9.9"
        (lic / "about.json").write_bytes(about_record.canonical_bytes(d))
    elif what == "licence":
        (lic / "LICENSE").write_text("tampered")
    elif what == "notices":
        (lic / "THIRD_PARTY_NOTICES.txt").write_text("tampered")
    elif what == "copy":
        c = tmp_path / "other.json"
        c.write_text("{}")
        kw["copies"] = (c,)
    elif what == "gate":
        doc = read_gate(root)
        doc["purpose"] = doc["purpose"] + " "
        write_gate(root, doc)
    elif what == "identity":
        p = root / "packaging" / "identity.json"
        d = json.loads(p.read_text())
        d["r0prime"]["version"] = "0.9.9"
        p.write_text(json.dumps(d))
    elif what == "slot":
        decide(root, ["D-LIC-10"])                      # decided now, but the staged record still says placeholder
    elif what == "words":
        d = json.loads((lic / "about.json").read_bytes())
        d["product"]["name"] = "Agentic Apps cleared"
        (lic / "about.json").write_bytes(about_record.canonical_bytes(d))
    elif what == "report":
        rep = json.loads((notices / "notices-report.json").read_text())
        rep["records"][0]["version"] = "9"
        (tmp_path / "rep.json").write_text(json.dumps(rep))
        kw["notices_report"] = tmp_path / "rep.json"
    assert chk_about.check(lic, root=root, **kw), what


def test_check_about_distribution_requires_a_distribution_record(env, tmp_path):
    root, notices = env
    lic = _staged(root, notices, tmp_path)
    errs = chk_about.check(lic, root=root, distribution=True)
    assert any("private-candidate record" in e for e in errs) and any("OPEN" in e for e in errs)


def test_check_about_cli_on_the_real_repository_files_fails_without_a_folder(tmp_path):
    r = subprocess.run([sys.executable, "-I", str(NOTICES / "check_about.py"), "--licenses-dir", str(tmp_path)],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 1 and "about.json" in r.stderr


# ------------------------------------------------------------------------------------------- wiring
def test_build_scripts_run_the_generator_and_the_checks():
    bl = (NOTICES / "build-licenses.sh").read_text()
    assert bl.index("generate_notices.py") < bl.index("generate_about.py") < bl.index("stage_licenses.py")
    ba = (ROOT / "packaging" / "build-all.sh").read_text()
    assert "--about-slots" in ba and ba.index("check_notices_vs_sbom.py") < ba.index("check_about.py")
    assert '"about.json"' in (ROOT / "packaging" / "redevops-sidecar.spec").read_text()
    assert "about.json" in (ROOT / "packaging" / "candidate" / "build-local-macos-candidate.sh").read_text()
    assert "about.json" in (NOTICES / "stage_licenses.py").read_text()
