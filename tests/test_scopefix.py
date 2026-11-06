"""RC3 scope-v3 review fixes: S3-05 (the release key is never a legal-decision signer) and S3-04 (documents agree with the code: scope version
and key lists). The ledger fix (S3-02) is tested in tests/test_signing_procedure_r0prime.py (test_s3_02_*). Throwaway keys only."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import chk_about, decide, gate, make_notices, make_root, read_gate, run_generate, signer, trust, write_gate

ROOT = Path(__file__).resolve().parents[1]
SIGN = ROOT / "packaging" / "signing"
RELEASE_FPR = "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"


def gate_cli(root, *args, allowed=None):
    cmd = [sys.executable, "-I", str(root / "packaging" / "check-distribution-gate.py"), "--gate", str(root / "packaging" / "distribution-gate.json"),
           "--root", str(root), "--allowed-signers", str(allowed or root / "packaging" / "legal-allowed-signers"), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=120)


def release_allowed_line() -> str:
    pub = (SIGN / "release-signing-key.pub").read_text().strip()
    return f'owner-release namespaces="redevops-legal-decision@v1" {pub}\n'


# ----------------------------------------------------------------------------------------------- S3-05
def test_the_gate_constant_is_the_published_release_fingerprint_everywhere():
    assert gate.RELEASE_KEY_FPR == RELEASE_FPR
    assert f'RELEASE_KEY_FPR="{RELEASE_FPR}"' in (SIGN / "sign_manifest_sshsig.sh").read_text()
    # and it is the fingerprint of the committed public release key
    tmp = ROOT / "packaging" / "signing" / "release-signing-key.pub"
    fps, probs = gate.allowed_signer_fingerprints(_write_tmp(release_allowed_line()))
    assert probs == [] and fps == [RELEASE_FPR], (fps, tmp)


_TMP = []


def _write_tmp(text: str) -> Path:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="sep-"))
    p = d / "allowed"
    p.write_text(text)
    _TMP.append(p)
    return p


def test_the_release_fingerprint_in_the_gate_file_is_an_error_in_every_mode(tmp_path):
    root = make_root(tmp_path)
    doc = read_gate(root)
    doc["legal_signer_fingerprints"] = [RELEASE_FPR]
    write_gate(root, doc)
    for args in ((), ("--distribution",), ("--distribution", "--about-slots"), ("--platform", "macos")):
        r = gate_cli(root, *args)
        assert r.returncode == 2 and "RELEASE key" in r.stderr and "separate legal-decision key" in r.stderr, (args, r.stderr)


def test_the_release_key_in_the_allowed_signers_file_is_an_error_in_every_mode(tmp_path, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    (root / "packaging" / "legal-allowed-signers").write_text(release_allowed_line())
    for args in ((), ("--distribution",), ("--about-slots",)):
        r = gate_cli(root, *args)
        assert r.returncode == 2 and RELEASE_FPR in r.stderr and "separate legal-decision key" in r.stderr, (args, r.stderr)
    # the About generator and the checker refuse it too
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 2
    assert run_generate(root, notices, distribution=True, monkeypatch=monkeypatch) == 2
    (root / "packaging" / "legal-allowed-signers").write_text("# empty\n")
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    lic = tmp_path / "Licenses"
    from .about_fixture import stage_folder
    stage_folder(root, notices, lic)
    (root / "packaging" / "legal-allowed-signers").write_text(release_allowed_line())
    assert any(e.startswith("signers:") and "RELEASE" in e or "release key" in e for e in chk_about.check(lic, root=root))


def test_a_throwaway_signer_still_works_and_separation_problems_are_empty(tmp_path, signer, monkeypatch):
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    decide(root, ["D-LIC-10"])
    trust(root, signer)
    assert gate.separation_problems(root / "packaging" / "legal-allowed-signers") == []
    assert run_generate(root, notices, monkeypatch=monkeypatch) == 0
    assert signer.fingerprint != RELEASE_FPR


def test_the_shipped_policy_is_empty_and_the_separation_is_documented():
    g = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())
    assert g["legal_signer_fingerprints"] == [] and RELEASE_FPR not in json.dumps(g["legal_signer_fingerprints"])
    t = " ".join((ROOT / "docs" / "ABOUT_LEGAL_UI.md").read_text().split())
    assert "separate legal-decision key" in t and "release key" in t and "any namespace" in t
    assert "separate" in " ".join(g["signers_policy"].split()) and "release key" in g["signers_policy"]


# ----------------------------------------------------------------------------------------------- S3-04: documents vs code
def _keys_of_script(path: Path) -> set:
    text = path.read_text().replace("\\\n", " ")
    m = re.search(r'r0p_scope_load "\$SCOPE_FILE" "\$AUTH_SHA" ([a-z0-9_ ]+?)(?:\n|$)', text)
    assert m, path
    return set(m.group(1).split())


def _version_of_script(path: Path) -> str:
    m = re.search(r'\[ "\$S_scope_version" = (\d+) \]', path.read_text())
    assert m, path
    return m.group(1)


def _template_block(name: str) -> str:
    t = (SIGN / "AUTHORIZATION-TEMPLATE.md").read_text()
    i = t.index(f"Scope file `{name}`:")
    j = t.index("```", t.index("```", i) + 3)
    return t[t.index("```", i) + 3:j]


@pytest.mark.parametrize("script,block", [("sign_and_notarize_r0prime_macos_v3.sh", "scope-macos.txt"), ("sign_windows_r0prime_v3.sh", "scope-win-inner.txt"),
                                          ("sign_manifest_sshsig.sh", "scope-sshsig.txt")])
def test_template_scope_blocks_list_exactly_the_keys_and_version_the_scripts_require(script, block):
    keys = _keys_of_script(SIGN / script)
    body = _template_block(block)
    lines = [ln for ln in body.splitlines() if "=" in ln]
    doc_keys = {ln.split("=", 1)[0] for ln in lines}
    assert doc_keys == keys, (sorted(doc_keys ^ keys))
    assert dict(ln.split("=", 1) for ln in lines)["scope_version"] == _version_of_script(SIGN / script)


def test_windows_container_block_names_the_same_keys_and_the_licenses_dir_instruction():
    t = (SIGN / "AUTHORIZATION-TEMPLATE.md").read_text()
    sec = t[t.index("## 3. Windows stage `container`"):t.index("## 4. Evidence manifest SSHSIG")]
    assert "--licenses-dir" in sec and "about.json" in sec and "container_listing_sha256" in sec
    p = (SIGN / "SIGNING-PROCEDURE-R0PRIME.md").read_text()
    assert "--licenses-dir" in p


def test_procedure_document_states_the_real_scope_versions_and_what_is_checked():
    p = " ".join((SIGN / "SIGNING-PROCEDURE-R0PRIME.md").read_text().split())
    mac, win, ssh = (_version_of_script(SIGN / n) for n in ("sign_and_notarize_r0prime_macos_v3.sh", "sign_windows_r0prime_v3.sh", "sign_manifest_sshsig.sh"))
    assert (mac, win, ssh) == ("3", "3", "2")
    assert f"scope_version {mac} (the SSHSIG manifest script stays scope_version {ssh})" in p
    for k in ("check_about_sha256", "generate_about_sha256", "about_record_sha256", "legal_signers_sha256"):
        assert k in p, k
    for needle in ("procedure_commit", "evidence_manifest_sha256", "bind nothing at run time", "Owner-attested", "claims/<artifact key>",
                   "separate legal-decision key", "Accepted limits", "independent review round"):
        assert needle in p, needle
    t = " ".join((SIGN / "AUTHORIZATION-TEMPLATE.md").read_text().split())
    assert "Scope files are **version 2**" not in t and "not verified by the scripts" not in t
    assert "same input artifact" in t and "scope_version 3" in t and "bind nothing at run time" in t


def test_the_documented_build_commit_rule_matches_the_scripts():
    mac, win = (SIGN / "sign_and_notarize_r0prime_macos_v3.sh").read_text(), (SIGN / "sign_windows_r0prime_v3.sh").read_text()
    assert 'r0p_about_check "$RES/Licenses" "$DISTRIBUTION" "$S_build_commit"' in mac          # macOS dmg, --real
    assert 'r0p_about_check "$LICDIR" "$DISTRIBUTION" "$S_build_commit"' in win and 'STAGE" = container' in win     # Windows container stage only
    t = " ".join((SIGN / "AUTHORIZATION-TEMPLATE.md").read_text().split())
    assert "macOS dmg" in t and "Windows `container` stage" in t and "Windows `inner` stage" in t
