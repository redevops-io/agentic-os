"""RC3 security review fixes (REVIEW_RC3_DEV.md): RC3-01 detached signatures and the honest trust statement, RC3-02 symlinks, RC3-03 no
trimming, exit code 2 for a malformed gate file. THROWAWAY test keys only (generated in a temp dir, deleted by name); no real key.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import (ALL_SLOT_DECISIONS, NAMESPACE, ROOT, Signer, decide, gate, gen_about, licenses_for, make_notices, make_root,
                            read_gate, run_generate, sign_all, signer, trust, write_gate)

GATE_PY = ROOT / "packaging" / "check-distribution-gate.py"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)


def all_ids(root):
    return sorted({d["id"] for d in read_gate(root)["decisions"]})


def gate_cli(root, *args, allowed=None):
    cmd = [sys.executable, "-I", str(root / "packaging" / "check-distribution-gate.py"), "--gate", str(root / "packaging" / "distribution-gate.json"),
           "--root", str(root), "--allowed-signers", str(allowed or root / "packaging" / "legal-allowed-signers"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120)


# ---------------------------------------------------------------------------------------------------- RC3-01
def test_the_shipped_allowed_signers_file_lists_no_key_and_says_so():
    text = (ROOT / "packaging" / "legal-allowed-signers").read_text()
    assert [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")] == []
    assert "EMPTY" in text and "redevops-legal-decision@v1" in text and "PUBLIC KEYS ONLY" in text
    assert "BEGIN OPENSSH PRIVATE KEY" not in text and "ssh-ed25519 " not in text and "ssh-rsa " not in text    # no key of any kind


def test_forgery_all_21_decided_with_invented_files_now_fails_a_distribution_build(tmp_path):
    """The review's reproducer: every decision DECIDED, invented records and invented slot texts, hashes written next to them."""
    root = make_root(tmp_path)
    decide(root, all_ids(root))
    assert len(all_ids(root)) == 21
    base = ["--platform", "all", "--distribution", "--about-slots"]
    r = gate_cli(root, *base)                                       # the shipped, EMPTY allowed-signers file
    assert r.returncode == 1 and "not an approved, signature-verified text" in r.stderr and "lists no signer" in r.stderr
    # even with a signer listed, unsigned files are refused
    s = Signer(tmp_path)
    try:
        doc = read_gate(root)
        doc["legal_signer_fingerprints"] = [s.fingerprint]
        write_gate(root, doc)
        r = gate_cli(root, *base, allowed=s.allowed)
        assert r.returncode == 1 and "D-LIC-5 record" in r.stderr and ".sig" in r.stderr
        sign_all(root, s)
        assert gate_cli(root, *base, allowed=s.allowed).returncode == 0          # only the owner's key makes it pass
    finally:
        s.cleanup()
    # the same forgery, with every blocker also made non-blocking, is caught by the slot pin
    doc = read_gate(root)
    for d in doc["decisions"]:
        if d["id"] == "D-LIC-10":
            d["blocks_distribution"] = False
    write_gate(root, doc)
    assert gate_cli(root, "--about-slots").returncode == 2


def test_forged_record_fails_the_about_generator_in_a_distribution_build(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, all_ids(root))
    assert run_generate(root, notices, distribution=True, monkeypatch=monkeypatch) == 3
    assert not (notices / "about.json").exists()


def test_private_build_with_a_bad_signature_warns_and_continues(tmp_path, monkeypatch, capsys):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])                                      # DECIDED, not signed
    r = gate_cli(root, "--platform", "macos", "--about-slots")
    assert r.returncode == 0 and "WARNING" in r.stderr and "D-LIC-10" in r.stderr and "distribution build would be refused" in r.stderr
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    assert "WARNING" in capsys.readouterr().err
    assert json.loads((notices / "about.json").read_bytes())["legal"]["slots"]["LEGAL.NO_WARRANTY"]["state"] == "unverified-draft"


def test_valid_signature_passes_and_each_defect_fails(tmp_path, signer):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10"])
    rec = "docs/legal/decisions/D-LIC-10.signed.txt"
    txt = "docs/legal/approved/legal-no-warranty.en.txt"
    allowed, fp = signer.allowed, [signer.fingerprint]
    for rel in (rec, txt):
        signer.sign(root / rel)
        assert gate.verify_sshsig(root, rel, allowed, fp) == "", rel
    assert gate.signature_problems(read_gate(root)["decisions"], root, allowed, "macos", fp) == []

    # tampered after signing
    (root / rec).write_text((root / rec).read_text() + "edited\n")
    assert "not a valid redevops-legal-decision@v1 signature" in gate.verify_sshsig(root, rec, allowed, fp)
    # wrong namespace
    signer.sign(root / txt, namespace="other-namespace@v1")
    assert "not a valid" in gate.verify_sshsig(root, txt, allowed, fp)
    # signed by another key
    other = Signer(tmp_path / "other-dir" if (tmp_path / "other-dir").mkdir() is None else tmp_path)
    try:
        other.sign(root / txt)
        assert "not a valid" in gate.verify_sshsig(root, txt, allowed, fp)
        assert gate.verify_sshsig(root, txt, other.allowed, [other.fingerprint]) == ""
    finally:
        other.cleanup()
    # missing signature
    (root / (txt + ".sig")).unlink()
    assert "not a regular file" in gate.verify_sshsig(root, txt, allowed, fp) or "not a file" in gate.verify_sshsig(root, txt, allowed, fp)
    # an allowed-signers line that restricts the namespace elsewhere does not authorise this namespace
    signer.sign(root / txt)
    restricted = tmp_path / "restricted"
    restricted.write_text(signer.allowed.read_text().replace(f'namespaces="{NAMESPACE}"', 'namespaces="something-else"'))
    assert "not a valid" in gate.verify_sshsig(root, txt, restricted, fp)
    # empty / comment-only allowed signers
    empty = tmp_path / "empty"
    empty.write_text("# nothing\n\n")
    assert "lists no signer" in gate.verify_sshsig(root, txt, empty, [])
    assert "unreadable" in gate.verify_sshsig(root, txt, tmp_path / "absent", fp)


def test_the_signature_must_cover_the_exact_bytes_of_every_listed_file(tmp_path, signer):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10", "D-LIC-5"])
    sign_all(root, signer)
    decisions = read_gate(root)["decisions"]
    fp = [signer.fingerprint]
    assert gate.signature_problems(decisions, root, signer.allowed, "macos", fp) == []
    (root / "docs/legal/approved/legal-section13-notice.en.txt").write_text("changed bytes\n")
    probs = gate.signature_problems(decisions, root, signer.allowed, "macos", fp)
    assert len(probs) == 1 and "LEGAL.SECTION13_NOTICE text" in probs[0]


def test_no_private_key_material_is_handled_by_the_gate():
    src = GATE_PY.read_text()
    assert "-Y\", \"sign\"" not in src and "ssh-keygen\", \"-t\"" not in src and "BEGIN OPENSSH PRIVATE KEY" not in src
    assert '"find-principals"' in src and '"verify"' in src


def test_codeowners_draft_covers_the_three_paths_and_is_not_installed():
    t = (ROOT / "docs" / "legal" / "CODEOWNERS.proposed").read_text()
    for path in ("/docs/legal/**", "/packaging/distribution-gate.json", "/packaging/legal-allowed-signers"):
        assert path in t
    assert not (ROOT / ".github" / "CODEOWNERS").exists()


def test_the_trust_boundary_is_stated_in_the_gate_the_docs_and_the_gate_file():
    src = GATE_PY.read_text()
    assert "WHAT THE GATE PROVES, AND WHAT IT DOES NOT" in src and "tripwire" in src and "does NOT prove who decided" in src
    doc = (ROOT / "docs" / "ABOUT_LEGAL_UI.md").read_text()
    assert "consistency" in doc and "no authenticity" in doc and "any editor of the repository" in doc and "legal-allowed-signers" in doc
    g = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())
    assert "consistency" in g["slot_texts_schema"] and "authenticity" in g["slot_texts_schema"]
    assert "detached signature" in g["purpose"] or "detached signature" in g["slot_texts_schema"]
    assert "hash-matched" not in doc


def test_slot_decisions_cannot_be_demoted_to_non_blocking(tmp_path):
    root = make_root(tmp_path)
    doc = read_gate(root)
    next(d for d in doc["decisions"] if d["id"] == "pins-03")["blocks_distribution"] = False
    slots, errs, _ = gate.resolve_about_slots(doc["decisions"], root, False)
    assert any("pins-03" in e and "blocks_distribution" in e for e in errs)


# ---------------------------------------------------------------------------------------------------- RC3-02
def test_symlinked_slot_text_is_refused_in_every_mode(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    f = root / "docs/legal/approved/legal-no-warranty.en.txt"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(f.read_bytes())
    f.unlink()
    os.symlink(outside, f)                                          # same bytes, same sha256: only the symlink is wrong
    for dist in (False, True):
        assert run_generate(root, notices, distribution=dist, monkeypatch=monkeypatch) == 2
    r = gate_cli(root, "--about-slots")
    assert r.returncode == 2 and "symbolic link" in r.stderr


def test_symlinked_decision_record_is_refused(tmp_path):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10"])
    rec = root / "docs/legal/decisions/D-LIC-10.signed.txt"
    outside = tmp_path / "outside-record.txt"
    outside.write_bytes(rec.read_bytes())
    rec.unlink()
    os.symlink(outside, rec)
    errs = gate.validate(read_gate(root)["decisions"], root)
    assert any("symbolic link" in e for e in errs)
    assert gate_cli(root).returncode == 2


@pytest.mark.parametrize("which", ["approved", "legal", "docs"])
def test_symlinked_directory_components_are_refused(tmp_path, which):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10"])
    target = {"approved": root / "docs/legal/approved", "legal": root / "docs/legal", "docs": root / "docs"}[which]
    moved = tmp_path / f"moved-{which}"
    shutil.move(str(target), str(moved))
    os.symlink(moved, target)
    doc = read_gate(root)["decisions"]
    slots, errs, _ = gate.resolve_about_slots(doc, root, False)
    assert errs and any("symbolic link" in e or "not a regular file" in e for e in errs), (which, errs)


def test_symlinked_signature_file_is_refused(tmp_path, signer):
    root = make_root(tmp_path)
    decide(root, ["D-LIC-10"])
    txt = "docs/legal/approved/legal-no-warranty.en.txt"
    sig = signer.sign(root / txt)
    real = tmp_path / "elsewhere.sig"
    real.write_bytes(sig.read_bytes())
    sig.unlink()
    os.symlink(real, sig)
    assert "symbolic link" in gate.verify_sshsig(root, txt, signer.allowed, [signer.fingerprint])


def test_unsafe_repo_file_helper(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.txt").write_text("x")
    os.symlink(tmp_path / "a" / "f.txt", tmp_path / "a" / "link.txt")
    os.symlink(tmp_path / "a", tmp_path / "dirlink")
    assert gate.unsafe_repo_file(tmp_path, "a/f.txt") == ""
    assert "symbolic link" in gate.unsafe_repo_file(tmp_path, "a/link.txt")
    assert "symbolic link" in gate.unsafe_repo_file(tmp_path, "dirlink/f.txt")
    assert gate.unsafe_repo_file(tmp_path, "a") == "not a regular file"
    assert gate.unsafe_repo_file(tmp_path, "a/missing.txt")
    assert gate.unsafe_repo_file(tmp_path, "a/f.txt/x")


# ---------------------------------------------------------------------------------------------------- exit code 2 for a malformed gate file
@pytest.mark.parametrize("content", ["", "not json", "[]", '{"schema": 2, "decisions": [1]}', '{"schema": 1}', '{"schema": 1, "decisions": []}'])
def test_a_malformed_gate_file_exits_2_everywhere(tmp_path, content):
    g = tmp_path / "bad.json"
    g.write_text(content)
    r = subprocess.run([sys.executable, "-I", str(GATE_PY), "--gate", str(g)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2 and "GATE FILE PROBLEM" in r.stderr, (content, r.returncode, r.stderr)
    r = subprocess.run([sys.executable, "-I", str(GATE_PY), "--gate", str(tmp_path / "missing.json")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2


def test_generator_exits_2_on_a_malformed_gate_file(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    (root / "packaging" / "distribution-gate.json").write_text("{")
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 2


def test_check_about_distribution_also_verifies_the_signatures(tmp_path, signer):
    from .about_fixture import chk_about
    root = make_root(tmp_path)
    decide(root, all_ids(root))
    trust(root, signer)
    lic = licenses_for(root, tmp_path / "work", distribution=True)
    assert chk_about.check(lic, root=root, distribution=True) == []
    # a signature disappears after the record was generated
    (root / "docs/legal/approved/legal-no-warranty.en.txt.sig").unlink()
    errs = chk_about.check(lic, root=root, distribution=True)
    assert any("distribution:" in e and "LEGAL.NO_WARRANTY text" in e for e in errs)
