"""F-2 of the final-tip review: the release-key separation check treats any allowed-signers line it cannot parse as a plain key line, and any
`cert-authority` line, as a problem in EVERY mode (private and distribution), so a `cert-authority` line that carries the release public key
is refused (exit 2) instead of slipping through. Throwaway/scratch roots only; the production files are never written."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from .about_fixture import chk_about, gate, make_notices, make_root, run_generate, stage_folder
from .test_scopefix import RELEASE_FPR, SIGN, gate_cli

ROOT = Path(__file__).resolve().parents[1]
NS = 'namespaces="redevops-legal-decision@v1"'
NOT_PLAIN = "is not a plain 'principal [options] key-type base64-key' line"


def _pub() -> str:
    return (SIGN / "release-signing-key.pub").read_text().strip()


def _other_pub() -> str:
    """A syntactically valid ed25519 key blob that is NOT the release key (made up bytes; no private key exists)."""
    import base64
    blob = bytes.fromhex("0000000b7373682d65643235353139") + bytes.fromhex("00000020") + hashlib.sha256(b"finalfix-F2-not-a-real-key").digest()
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


REJECTED = {
    "cert-authority-release-key": f"owner cert-authority {NS} {_pub()}\n",
    "cert-authority-first-option": f"owner cert-authority,{NS} {_pub()}\n",
    "cert-authority-other-key": f"owner cert-authority {NS} {_other_pub()}\n",
    "unbalanced-quote": f'owner {NS} "ssh-ed25519 AAAA\n',
    "no-key-type": f"owner {NS} AAAAC3NzaC1lZDI1NTE5AAAAIHkr\n",
    "invalid-base64-key": f"owner {NS} ssh-ed25519 not*base64!\n",
    "key-type-without-key": f"owner {NS} ssh-ed25519\n",
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_an_unparseable_or_cert_authority_line_is_refused_with_exit_2_in_every_mode(tmp_path, name):
    root = make_root(tmp_path)
    (root / "packaging" / "legal-allowed-signers").write_text(REJECTED[name])
    for args in ((), ("--platform", "macos"), ("--distribution",), ("--about-slots",), ("--distribution", "--about-slots")):
        r = gate_cli(root, *args)
        assert r.returncode == 2, (name, args, r.returncode, r.stdout, r.stderr)
        assert "GATE FILE PROBLEM" in r.stderr and "legal-allowed-signers line 1" in r.stderr, (name, args, r.stderr)


def test_the_cert_authority_line_with_the_release_key_is_the_reviewers_reproducer(tmp_path):
    root = make_root(tmp_path)
    (root / "packaging" / "legal-allowed-signers").write_text(REJECTED["cert-authority-release-key"])
    r = gate_cli(root, "--platform", "macos")
    assert r.returncode == 2 and NOT_PLAIN in r.stderr, (r.returncode, r.stderr)


def test_separation_problems_reports_those_lines_and_a_later_bad_line_after_good_ones(tmp_path):
    p = tmp_path / "allowed"
    p.write_text(f"# comment\n\nowner {NS} {_other_pub()}\nowner2 cert-authority {NS} {_other_pub()}\n")
    probs = gate.separation_problems(p)
    assert len(probs) == 1 and "line 4" in probs[0] and NOT_PLAIN in probs[0], probs
    p.write_text(REJECTED["unbalanced-quote"])
    assert gate.separation_problems(p) == [ "legal-allowed-signers line 1 cannot be parsed" ]


def test_the_plain_release_key_line_still_gives_the_separation_message_and_only_that(tmp_path):
    p = tmp_path / "allowed"
    p.write_text(f"owner-release {NS} {_pub()}\n")
    probs = gate.separation_problems(p)
    assert len(probs) == 1 and RELEASE_FPR in probs[0] and "separate legal-decision key" in probs[0], probs


def test_valid_lines_comments_blank_lines_and_a_missing_file_are_not_problems(tmp_path):
    p = tmp_path / "allowed"
    p.write_text(f"# header\n\nowner {NS} {_other_pub()}\n*@example.org valid-before=\"20991231\" {NS} {_other_pub()}\n")
    assert gate.separation_problems(p) == []
    assert gate.separation_problems(tmp_path / "does-not-exist") == []        # unchanged: a missing file is not a line problem


def test_the_empty_production_allowed_signers_file_and_the_production_gate_still_pass():
    assert gate.allowed_signer_fingerprints(ROOT / "packaging" / "legal-allowed-signers") == ([], [])
    assert gate.separation_problems(ROOT / "packaging" / "legal-allowed-signers") == []
    r = gate_cli(ROOT)
    assert r.returncode == 0 and "PRIVATE CANDIDATE" in r.stdout, (r.returncode, r.stdout, r.stderr)
    r = gate_cli(ROOT, "--about-slots")
    assert r.returncode == 0, (r.returncode, r.stderr)


@pytest.mark.parametrize("name", ["cert-authority-release-key", "unbalanced-quote"])
def test_the_about_generator_and_checker_refuse_these_lines_too(tmp_path, monkeypatch, name):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    allowed = root / "packaging" / "legal-allowed-signers"
    allowed.write_text("# empty\n")
    lic = tmp_path / "Licenses"
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    stage_folder(root, notices, lic)
    allowed.write_text(REJECTED[name])
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 2
    assert run_generate(root, notices, distribution=True, monkeypatch=monkeypatch) == 2
    assert any(e.startswith("signers:") for e in chk_about.check(lic, root=root))
