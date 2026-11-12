"""RC3 round-2 review fixes (R2-01): the signed record binds MEANING (decision id, option, slots, text hashes), the signer list is pinned
by fingerprints in the gate file, and an unverified text never reaches a page as ordinary legal text. THROWAWAY test keys only."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import (ALL_SLOT_DECISIONS, ROOT, Signer, about_record, chk_about, decide, gate, gen_about, licenses_for, make_notices,
                            make_root, read_gate, record_text, run_generate, sign_all, signer, stage_folder, trust, write_gate)

DRAFT = "Unverified draft, not an approved text."


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)


def gate_cli(root, *args, allowed=None):
    cmd = [sys.executable, "-I", str(root / "packaging" / "check-distribution-gate.py"), "--gate", str(root / "packaging" / "distribution-gate.json"),
           "--root", str(root), "--allowed-signers", str(allowed or root / "packaging" / "legal-allowed-signers"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120)


def all_ids(root):
    return sorted({d["id"] for d in read_gate(root)["decisions"]})


def edit_gate(root, fn):
    doc = read_gate(root)
    fn(doc)
    write_gate(root, doc)


def entry(doc, i):
    return next(d for d in doc["decisions"] if d["id"] == i)


# ----------------------------------------------------------------------------------------------- the record header format
def test_header_parser_accepts_the_documented_format():
    raw = record_text({"id": "D-LIC-1", "chosen_option": "a"}, {"LEGAL.COPYRIGHT_LINE": {"sha256": "ab" * 32},
                                                              "LEGAL.SECTION13_NOTICE": {"not_applicable": True}}).encode()
    hdr, probs = gate.parse_decision_record(raw)
    assert probs == [] and hdr["decision"] == "D-LIC-1" and hdr["option"] == "a"
    assert hdr["slots"] == {"LEGAL.COPYRIGHT_LINE": "ab" * 32, "LEGAL.SECTION13_NOTICE": "not-applicable"}


@pytest.mark.parametrize("raw", [
    b"", b"decision: D-LIC-1\noption: a\n", b"redevops-legal-decision: 2\ndecision: D-LIC-1\noption: a\n",
    b"redevops-legal-decision: 1\noption: a\n", b"redevops-legal-decision: 1\ndecision: D-LIC-1\n",
    b"redevops-legal-decision: 1\ndecision: D-LIC-1\ndecision: D-LIC-2\noption: a\n",
    b"redevops-legal-decision: 1\ndecision: D-LIC-1\noption: a\nfoo: bar\n",
    b"redevops-legal-decision: 1\ndecision: D-LIC-1\noption: a\nslot: LEGAL.X sha256 zz\n",
    b"redevops-legal-decision: 1\ndecision: D-LIC-1\noption: a\nslot: LEGAL.X sha256 " + b"a" * 64 + b"\nslot: LEGAL.X not-applicable\n",
    b"redevops-legal-decision: 1\ndecision: D-LIC-1\noption: a\nnot a header line\n", b"\xff\xfe",
])
def test_header_parser_refuses_malformed_headers(raw):
    hdr, probs = gate.parse_decision_record(raw)
    assert probs, raw


def test_header_stops_at_the_first_empty_line_so_free_text_cannot_add_fields():
    raw = b"redevops-legal-decision: 1\ndecision: D-LIC-1\noption: a\n\ndecision: D-LIC-2\nslot: LEGAL.X not-applicable\n"
    hdr, probs = gate.parse_decision_record(raw)
    assert probs == [] and hdr["decision"] == "D-LIC-1" and hdr["slots"] == {}


# ----------------------------------------------------------------------------------------------- re-use forgeries (the review's reproducer)
def test_one_signed_record_and_one_signed_text_cannot_satisfy_every_decision_and_slot(tmp_path, signer):
    root = make_root(tmp_path)
    decide(root, all_ids(root))
    trust(root, signer)
    base = ["--platform", "all", "--distribution", "--about-slots"]
    assert gate_cli(root, *base).returncode == 0                    # the honest, signed set passes (positive control)
    doc = read_gate(root)
    one = entry(doc, "D-LIC-10")
    rec_rel, rec_sha = one["decision_reference"], one["decision_sha256"]
    txt = one["slot_texts"]["LEGAL.NO_WARRANTY"]

    def forge(d):
        for x in d["decisions"]:
            if x["status"] == "DECIDED":
                x["decision_reference"], x["decision_sha256"] = rec_rel, rec_sha          # every decision points to the ONE signed record
                if x.get("slot_texts"):
                    x["slot_texts"] = {k: dict(txt) for k in x["slot_texts"] if "file" in x["slot_texts"][k]}  # every slot to the ONE signed text
    edit_gate(root, forge)
    r = gate_cli(root, *base)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "the signed record is for decision 'D-LIC-10'" in r.stderr or "authorizes different slots" in r.stderr


def test_a_signed_text_reused_for_another_slot_is_refused(tmp_path, signer):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-1", "D-LIC-15"])
    trust(root, signer)
    d = read_gate(root)["decisions"]
    slots, errs, ref = gate.resolve_about_slots(d, root, True, allowed_signers=signer.allowed, signers=[signer.fingerprint])
    assert errs == [] and slots["LEGAL.COPYRIGHT_LINE"]["state"] == "approved-verified" and slots["LEGAL.LICENCE_STATEMENT"]["state"] == "approved-verified"
    # LICENCE_STATEMENT now claims the (signed) COPYRIGHT text: the signed record of D-LIC-1 lists another hash for that slot
    def reuse(doc):
        st = entry(doc, "D-LIC-1")["slot_texts"]
        st["LEGAL.LICENCE_STATEMENT"] = dict(st["LEGAL.COPYRIGHT_LINE"])
    edit_gate(root, reuse)
    d = read_gate(root)["decisions"]
    slots, errs, ref = gate.resolve_about_slots(d, root, True, allowed_signers=signer.allowed, signers=[signer.fingerprint])
    assert slots["LEGAL.LICENCE_STATEMENT"]["state"] == "unverified-draft" and any("LEGAL.LICENCE_STATEMENT" in r for r in ref)
    assert gate_cli(root, "--platform", "macos", "--distribution", "--about-slots").returncode == 1


def test_a_changed_option_or_slot_set_in_the_gate_entry_is_not_covered_by_the_signature(tmp_path, signer):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10"])
    trust(root, signer)
    fp = [signer.fingerprint]
    d = read_gate(root)["decisions"]
    assert gate.signature_problems(d, root, signer.allowed, "macos", fp) == []
    # (1) another option than the signed one (decision_sha256 untouched: the record file is unchanged)
    edit_gate(root, lambda doc: entry(doc, "D-LIC-10").update(chosen_option="b"))
    probs = gate.signature_problems(read_gate(root)["decisions"], root, signer.allowed, "macos", fp)
    assert any("names option 'a'" in p or "option" in p for p in probs)
    edit_gate(root, lambda doc: entry(doc, "D-LIC-10").update(chosen_option="a"))
    # (2) an extra slot text claimed by the decision
    def extra(doc):
        e = entry(doc, "D-LIC-10")
        e["slot_texts"]["LEGAL.COPYRIGHT_LINE"] = dict(e["slot_texts"]["LEGAL.NO_WARRANTY"])
    edit_gate(root, extra)
    probs = gate.signature_problems(read_gate(root)["decisions"], root, signer.allowed, "macos", fp)
    assert any("authorizes different slots" in p for p in probs)


def test_a_text_signed_alone_is_not_enough(tmp_path, signer):
    """The signed record must list `slot: <id> sha256 <hex>`; a record without the line does not authorize the text."""
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10"])
    rec = root / "docs/legal/decisions/D-LIC-10.signed.txt"
    rec.write_text("redevops-legal-decision: 1\ndecision: D-LIC-10\noption: a\n\nno slot line\n")
    edit_gate(root, lambda doc: entry(doc, "D-LIC-10").update(decision_sha256=hashlib.sha256(rec.read_bytes()).hexdigest()))
    trust(root, signer)
    probs = gate.signature_problems(read_gate(root)["decisions"], root, signer.allowed, "macos", [signer.fingerprint])
    assert any("only in the gate entry: ['LEGAL.NO_WARRANTY']" in p for p in probs)


# ----------------------------------------------------------------------------------------------- private builds: unverified text is labelled
def test_forged_text_in_a_private_build_is_a_labelled_unverified_draft_not_ordinary_text(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])                                      # DECIDED with an invented record and text, no signature anywhere
    forged = root / "docs/legal/approved/legal-no-warranty.en.txt"
    body = "FORGED TEXT: nobody warrants nothing.\n"
    forged.write_text(body)
    sha = hashlib.sha256(body.encode()).hexdigest()
    rec = root / "docs/legal/decisions/D-LIC-10.signed.txt"
    def fix(doc):
        e = entry(doc, "D-LIC-10")
        e["slot_texts"]["LEGAL.NO_WARRANTY"]["sha256"] = sha
        rec.write_text(record_text(e))
        e["decision_sha256"] = hashlib.sha256(rec.read_bytes()).hexdigest()
    edit_gate(root, fix)
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0                 # private: allowed, with a warning
    slot = json.loads((notices / "about.json").read_bytes())["legal"]["slots"]["LEGAL.NO_WARRANTY"]
    assert slot["state"] == "unverified-draft" and slot["tag"] == "Unverified draft, not an approved text"
    assert slot["text"].startswith(DRAFT) and "FORGED TEXT" in slot["text"] and slot["text"].index(DRAFT) < slot["text"].index("FORGED")
    assert slot["text_sha256"] == sha
    # never approved-verified, and a distribution build refuses it
    assert run_generate(root, notices, distribution=True, monkeypatch=monkeypatch) == 3
    assert not (notices / "about.json").exists()


def test_the_record_validator_knows_the_four_states_and_their_rules(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    doc = json.loads((notices / "about.json").read_bytes())
    assert about_record.validate_record(doc) == []
    s = doc["legal"]["slots"]["LEGAL.NO_WARRANTY"]
    assert s["state"] == "unverified-draft" and s["text"].startswith(about_record.DRAFT_PREFIX)
    s["text"] = s["text"][len(about_record.DRAFT_PREFIX):]                               # a draft without its label is invalid
    assert about_record.validate_record(doc)
    s["state"], s["tag"], s["pending"] = "approved-verified", about_record.SLOT_TAGS["approved-verified"], []
    assert about_record.validate_record(doc) == []                                     # only the build tool decides the state; the validator checks shape
    doc["build"]["mode"] = "distribution"
    assert about_record.validate_record(doc)                                           # but a distribution record still carries pending slots
    assert set(about_record.SLOT_STATES) == {"pending", "approved-verified", "unverified-draft", "not-applicable"}
    assert gate.TAGS == about_record.SLOT_TAGS and gate.DRAFT_PREFIX == about_record.DRAFT_PREFIX


def test_signed_and_trusted_text_is_approved_verified(tmp_path, signer, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    trust(root, signer)
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    s = json.loads((notices / "about.json").read_bytes())["legal"]["slots"]["LEGAL.NO_WARRANTY"]
    assert s["state"] == "approved-verified" and s["tag"] == "Approved text, signature verified" and not s["text"].startswith(DRAFT)


@pytest.mark.parametrize("break_it", ["unsign-text", "unsign-record", "other-key", "wrong-namespace"])
def test_any_missing_or_bad_signature_downgrades_an_approved_slot(tmp_path, signer, monkeypatch, break_it):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    trust(root, signer)
    txt, rec = root / "docs/legal/approved/legal-no-warranty.en.txt", root / "docs/legal/decisions/D-LIC-10.signed.txt"
    if break_it == "unsign-text":
        Path(str(txt) + ".sig").unlink()
    elif break_it == "unsign-record":
        Path(str(rec) + ".sig").unlink()
    elif break_it == "other-key":
        o = Signer(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
        try:
            o.sign(txt)
        finally:
            o.cleanup()
    else:
        signer.sign(rec, namespace="other@v1")
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    assert json.loads((notices / "about.json").read_bytes())["legal"]["slots"]["LEGAL.NO_WARRANTY"]["state"] == "unverified-draft"


def test_a_decided_not_applicable_slot_that_is_unverified_stays_a_visible_placeholder(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ALL_SLOT_DECISIONS, texts="na")
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    s = json.loads((notices / "about.json").read_bytes())["legal"]["slots"]["LEGAL.SECTION13_NOTICE"]
    assert s["state"] == "pending" and s["pending"] == ["D-LIC-5"] and "pending" in s["text"]


# ----------------------------------------------------------------------------------------------- the signer list is pinned (R2-01 c)
def test_fingerprint_computation_matches_ssh_keygen(signer):
    fps, probs = gate.allowed_signer_fingerprints(signer.allowed)
    assert probs == [] and fps == [signer.fingerprint]


def test_shipped_policy_is_empty_and_documented():
    g = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())
    assert gate.allowed_signer_fingerprints(ROOT / "packaging" / "legal-allowed-signers") == (g["legal_signer_fingerprints"], [])     # the policy pins exactly the keys the file lists
    p = g["signers_policy"]
    assert "BOTH files are repository data" in p and "independent channel" in p and "procedure step, not a code guarantee" in p


def test_a_key_added_to_the_allowed_signers_file_alone_is_not_accepted(tmp_path, signer):
    """Review case E: an attacker adds their own key to legal-allowed-signers and signs forgeries; the gate file's fingerprint list does
    not name it, so nothing verifies."""
    root = make_root(tmp_path)
    decide(root, all_ids(root))
    trust(root, signer)
    evil = Signer(tmp_path / "evil" if (tmp_path / "evil").mkdir() is None else tmp_path)
    try:
        sign_all(root, evil)
        (root / "packaging" / "legal-allowed-signers").write_text(evil.allowed.read_text())            # their key, not pinned in the gate file
        r = gate_cli(root, "--platform", "all", "--distribution", "--about-slots")
        assert r.returncode == 1 and "is not listed in legal_signer_fingerprints" in r.stderr
        # both lists edited together is possible for a repository writer: that is why the owner confirms the fingerprints independently
        edit_gate(root, lambda d: d.update(legal_signer_fingerprints=[evil.fingerprint]))
        assert gate_cli(root, "--platform", "all", "--distribution", "--about-slots").returncode == 0
    finally:
        evil.cleanup()


def test_pairing_rules(tmp_path, signer):
    fp = signer.fingerprint
    empty = tmp_path / "empty"
    empty.write_text("# no keys\n")
    assert gate.anchor_problems(empty, []) == []
    assert gate.anchor_problems(signer.allowed, [fp]) == []
    assert any("not listed" in m for m in gate.anchor_problems(signer.allowed, []))
    assert any("has no key" in m for m in gate.anchor_problems(empty, [fp]))
    ca = tmp_path / "ca"
    ca.write_text(signer.allowed.read_text().replace("namespaces=", "cert-authority,namespaces="))
    assert gate.anchor_problems(ca, [fp])
    junk = tmp_path / "junk"
    junk.write_text("not a key line\n")
    assert gate.anchor_problems(junk, [])


@pytest.mark.parametrize("fps", ["x", ["nope"], ["SHA256:short"], [1], ["SHA256:" + "A" * 43] * 2])
def test_a_malformed_fingerprint_list_exits_2(tmp_path, fps):
    root = make_root(tmp_path)
    edit_gate(root, lambda d: d.update(legal_signer_fingerprints=fps))
    r = gate_cli(root)
    assert r.returncode == 2 and "legal_signer_fingerprints" in r.stderr


def test_slot_texts_are_never_verified_against_an_unpinned_list(tmp_path, signer, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    trust(root, signer)
    edit_gate(root, lambda d: d.update(legal_signer_fingerprints=[]))                      # the pin is removed again
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    assert json.loads((notices / "about.json").read_bytes())["legal"]["slots"]["LEGAL.NO_WARRANTY"]["state"] == "unverified-draft"


# ----------------------------------------------------------------------------------------------- R2-03 no-follow reads
def test_read_repo_file_refuses_a_symlink_swapped_in_after_the_lstat(tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    assert gate.read_repo_file(tmp_path, "f.txt") == b"x"
    (tmp_path / "f.txt").unlink()
    os.symlink(outside, tmp_path / "f.txt")
    monkeypatch.setattr(gate, "unsafe_repo_file", lambda root, rel: "")                 # the race: the lstat check said "fine"
    with pytest.raises(OSError):
        gate.read_repo_file(tmp_path, "f.txt")
    monkeypatch.undo()
    with pytest.raises(OSError):
        gate.read_repo_file(tmp_path, "f.txt")
    (tmp_path / "big.txt").write_bytes(b"y" * 100)
    with pytest.raises(OSError):
        gate.read_repo_file(tmp_path, "big.txt", cap=10)


# ----------------------------------------------------------------------------------------------- docs
def test_docs_state_the_trust_model_and_that_codeowners_is_inactive():
    doc = " ".join((ROOT / "docs" / "ABOUT_LEGAL_UI.md").read_text().split())
    for needle in ("legal_signer_fingerprints", "BOTH are repository data", "independent channel", "procedure step, not a code guarantee",
                   "unverified-draft", "approved-verified", "inactive", "docs/legal/CODEOWNERS.proposed", "redevops-legal-decision: 1"):
        assert needle in doc, needle
    src = " ".join((ROOT / "packaging" / "check-distribution-gate.py").read_text().split())
    assert "BOTH files are repository data" in src and "independent channel" in src
    assert "redevops-legal-decision: 1" in src and "legal_signer_fingerprints" in src
    t = (ROOT / "docs" / "legal" / "CODEOWNERS.proposed").read_text()
    assert "NOT active" in t and not (ROOT / ".github" / "CODEOWNERS").exists()
