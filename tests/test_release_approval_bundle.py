"""Consolidated owner approval (record header version 2): one signature, owner-only decisions, nothing weakened.
THROWAWAY test keys only (tests/about_fixture.Signer)."""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import NAMESPACE, ROOT, Signer, make_root, read_gate, write_gate

GATE_PY = ROOT / "packaging" / "check-distribution-gate.py"
_spec = importlib.util.spec_from_file_location("_gate_under_test", GATE_PY)
GATE = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = GATE
_spec.loader.exec_module(GATE)

C, T, M = "a" * 40, "b" * 40, "c" * 64


def header(decisions: dict, **over) -> str:
    h = {"bundle": "TEST-BUNDLE", "base-commit": C, "base-tree": T, "manifest-sha256": M}
    h.update(over)
    lines = ["redevops-legal-decision: 2"] + [f"{k}: {v}" for k, v in h.items() if v is not None]
    lines += [f"decision: {i} {o}" for i, o in decisions.items()]
    return "\n".join(lines) + "\n\n# body\nfree text\n"


def owner_ids(root):
    """owner-only decisions that block distribution (the gate verifies those; a non-blocking DECIDED entry is not checked at all)"""
    return [d["id"] for d in read_gate(root)["decisions"] if d["decides"] == "owner" and d["blocks_distribution"]]


def decide_bundle(root: Path, signer: Signer, text: str, ids, *, sign: bool = True, option=None) -> Path:
    rec = root / "docs" / "legal" / "approved" / "BUNDLE.md"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(text)
    if sign:
        signer.sign(rec)
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] in ids:
            d.update(status="DECIDED", chosen_option=option or d["options"][0]["key"], decision_reference=rec.relative_to(root).as_posix(),
                     decision_sha256=hashlib.sha256(rec.read_bytes()).hexdigest(), decided_by="test-owner", decided_on="2026-12-01")
            if "counsel" in d["decides"]:
                d["counsel_reference"] = "synthetic counsel reference"
    doc["legal_signer_fingerprints"] = [signer.fingerprint]
    write_gate(root, doc)
    return rec


def problems(root, signer, platform="all"):
    doc = read_gate(root)
    return GATE.signature_problems(doc["decisions"], root=root, allowed_signers=signer.allowed, platform=platform, signers=doc["legal_signer_fingerprints"])


@pytest.fixture
def env(tmp_path):
    root = make_root(tmp_path)
    s = Signer(tmp_path)
    yield root, s
    s.cleanup()


def firsts(root):
    ids = owner_ids(root)
    return ids, {i: next(o["key"] for o in d["options"]) for d in read_gate(root)["decisions"] for i in [d["id"]] if i in ids}


def test_one_signature_covers_every_owner_only_decision(env):
    root, s = env
    ids, opts = firsts(root)
    assert len(ids) >= 6
    decide_bundle(root, s, header(opts), ids)
    assert problems(root, s) == []


def test_a_bundle_never_covers_a_decision_that_names_counsel(env):
    root, s = env
    ids, opts = firsts(root)
    counsel = [d for d in read_gate(root)["decisions"] if "counsel" in d["decides"]]
    assert {d["decides"] for d in counsel} == {"counsel", "owner+counsel"}
    for d in counsel:
        o = dict(opts)
        o[d["id"]] = d["options"][0]["key"]
        decide_bundle(root, s, header(o), ids + [d["id"]])
        msgs = [m for m in problems(root, s) if m.startswith(d["id"])]
        assert msgs and "never one that names counsel" in " ".join(msgs), d["id"]
        # reset
        doc = read_gate(root)
        for e in doc["decisions"]:
            if e["id"] == d["id"]:
                e.update(status="OPEN", chosen_option=None, decision_reference=None, decision_sha256=None, decided_by=None, decided_on=None, counsel_reference=None)
        write_gate(root, doc)


def test_tampered_bundle_fails_every_decision_it_covers(env):
    root, s = env
    ids, opts = firsts(root)
    rec = decide_bundle(root, s, header(opts), ids)
    rec.write_text(rec.read_text() + "edited after signing\n")
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] in ids:
            d["decision_sha256"] = hashlib.sha256(rec.read_bytes()).hexdigest()       # hash fixed up; the signature is what stops it
    write_gate(root, doc)
    msgs = problems(root, s)
    assert all(any(m.startswith(f"{i} record") for m in msgs) for i in ids)


def test_unsigned_or_wrong_namespace_or_unknown_key_is_refused(env, tmp_path):
    root, s = env
    ids, opts = firsts(root)
    decide_bundle(root, s, header(opts), ids, sign=False)
    assert any("record" in m for m in problems(root, s))
    rec = root / "docs/legal/approved/BUNDLE.md"
    s.sign(rec, namespace="some-other-namespace@v1")
    assert any("not a valid" in m for m in problems(root, s))
    other = Signer(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    try:
        other.sign(rec)
        assert any("not a valid" in m for m in problems(root, s))        # signed by a key that is not listed
    finally:
        other.cleanup()


def test_option_and_listing_must_match_the_gate_entry(env):
    root, s = env
    ids, opts = firsts(root)
    decide_bundle(root, s, header({**opts, ids[0]: "zz"}), ids)
    assert any(m.startswith(ids[0]) and "names option" in m for m in problems(root, s))
    short = {k: v for k, v in opts.items() if k != ids[1]}
    decide_bundle(root, s, header(short), ids)
    assert any(m.startswith(ids[1]) and "does not list" in m for m in problems(root, s))


@pytest.mark.parametrize("mutate,needle", [
    (lambda h: h.replace("redevops-legal-decision: 2", "redevops-legal-decision: 4"), "start with the header"),
    (lambda h: h.replace("base-commit: " + C, "base-commit: xyz"), "base-commit"),
    (lambda h: h.replace("manifest-sha256: " + M, ""), "manifest-sha256"),
    (lambda h: h.replace("bundle: TEST-BUNDLE\n", "bundle: A\nbundle: B\n"), "given twice"),
    (lambda h: h.replace("\n\n", "\nslot: LEGAL.NO_WARRANTY sha256 " + "d" * 64 + "\n\n", 1), "slot"),
    (lambda h: h.replace("\n\n", "\nsurprise: 1\n\n", 1), "unknown header key"),
])
def test_malformed_bundle_headers_are_refused(env, mutate, needle):
    root, s = env
    ids, opts = firsts(root)
    decide_bundle(root, s, mutate(header(opts)), ids)
    msgs = " ".join(problems(root, s))
    assert needle in msgs


def test_duplicate_decision_line_is_refused(env):
    root, s = env
    ids, opts = firsts(root)
    text = header(opts).replace("\n\n", f"\ndecision: {ids[0]} {opts[ids[0]]}\n\n", 1)
    decide_bundle(root, s, text, ids)
    assert "listed twice" in " ".join(problems(root, s))


def test_a_bundle_entry_with_slot_texts_is_refused(env):
    root, s = env
    ids, opts = firsts(root)
    decide_bundle(root, s, header(opts), ids)
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] == ids[0]:
            d["slot_texts"] = {"LEGAL.NO_WARRANTY": {"file": "docs/legal/approved/x.txt", "sha256": "e" * 64}}
    write_gate(root, doc)
    assert any(m.startswith(ids[0]) and "authorizes no About-slot text" in m for m in problems(root, s))


def test_a_distribution_build_still_refuses_while_counsel_decisions_are_open(env):
    root, s = env
    ids, opts = firsts(root)
    decide_bundle(root, s, header(opts), ids)
    r = subprocess.run([sys.executable, "-I", str(root / "packaging/check-distribution-gate.py"), "--gate", str(root / "packaging/distribution-gate.json"),
                        "--root", str(root), "--allowed-signers", str(s.allowed), "--platform", "macos", "--distribution"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120)
    assert r.returncode == 1
    for cid in ("D-LIC-3", "D-LIC-4", "D-LIC-5", "pins-03", "D-LIC-1", "D-LIC-18"):
        assert cid in r.stderr, cid
    for oid in ("D-LIC-2", "D-LIC-13", "D-LIC-15", "pins-04"):
        assert f"{oid}:" not in r.stderr or "open" not in r.stderr.split(f"{oid}:")[1][:60].lower(), oid


def test_version_1_records_are_unchanged_and_the_release_key_is_still_refused(env):
    root, s = env
    src = GATE_PY.read_text()
    assert "RELEASE_KEY_FPR" in src and "redevops-legal-decision: {RECORD_VERSION}" in src
    doc = read_gate(root)
    doc["legal_signer_fingerprints"] = [GATE.RELEASE_KEY_FPR]
    assert GATE.separation_problems(root / "packaging/legal-allowed-signers") == []   # the shipped file lists no key
    assert NAMESPACE == GATE.SIG_NAMESPACE


def test_a_bundle_refuses_frozen_counsel_decisions_even_if_the_gate_file_says_owner(env):
    root, s = env
    ids, opts = firsts(root)
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] in ("D-LIC-3", "pins-03", "D-LIC-4"):
            d["decides"] = "owner"
    write_gate(root, doc)
    o = dict(opts)
    o.update({"D-LIC-3": "d", "pins-03": "a", "D-LIC-4": "a"})
    decide_bundle(root, s, header(o), ids + ["D-LIC-3", "pins-03", "D-LIC-4"])
    msgs = " ".join(problems(root, s))
    for cid in ("D-LIC-3", "pins-03", "D-LIC-4"):
        assert f"{cid}: decides is" in msgs and "never one that names counsel" in msgs
