"""The DEVELOPMENT-ONLY disposable-signer harness (packaging/notices/tools/dev_signer_e2e.py, docs/ABOUT_LEGAL_DEV_SIGNER.md).

These tests run the harness (real gate / generator / checker / sidecar code on a fixture repository root, a throwaway key in a temporary
directory) and check that it proves the mechanism, that it fails when it should, and that it leaves the production files untouched and
no key behind. Offline; skipped when ssh-keygen is missing. No production signer, no real decision, no real key.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "packaging" / "notices" / "tools" / "dev_signer_e2e.py"
DOC = ROOT / "docs" / "ABOUT_LEGAL_DEV_SIGNER.md"

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin") is None,
                                reason="ssh-keygen is not available")

spec = importlib.util.spec_from_file_location("_t_dev_signer_e2e", HARNESS)
dev = importlib.util.module_from_spec(spec)
sys.modules["_t_dev_signer_e2e"] = dev
spec.loader.exec_module(dev)

EXPECTED = {            # scenario -> (gate private, gate distribution, About private, About distribution)
    "all-signed": (0, 0, 0, 0),
    "tamper-record": (2, 2, 2, 2),
    "tamper-text": (2, 2, 2, 2),
    "tamper-record+hash": (0, 1, 0, 3),
    "tamper-text+hash": (0, 1, 0, 3),
    "second-key": (0, 1, 0, 3),
    "record-reused": (0, 1, 0, 3),
    "option-changed": (0, 1, 0, 3),
    "wrong-namespace": (0, 1, 0, 3),
    "missing-record-sig": (0, 1, 0, 3),
    "missing-text-sig": (0, 1, 0, 3),
    "fingerprint-mismatch": (0, 1, 0, 3),
    "section13-na": (0, 0, 0, 0),
    "decision-open": (0, 1, 0, 3),
}


def production_snapshot():
    return dev.production_state(ROOT)


@pytest.fixture(scope="module")
def run_once(tmp_path_factory):
    """One full harness run (module scope): returns (exit code, stdout, stderr, results json, tmp base, state before, state after)."""
    import contextlib
    import io
    base = tmp_path_factory.mktemp("harness-base")
    out_json = base / "result.json"
    mp = pytest.MonkeyPatch()
    mp.setenv("SSH_AUTH_SOCK", "/nonexistent-agent-socket-for-the-test")      # must be dropped, never reach a child
    mp.setenv("SSH_AGENT_PID", "1")
    mp.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)
    before = production_snapshot()
    dev._LOG.clear()
    so, se = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = dev.main(["--tmp-base", str(base), "--json", str(out_json)])
    finally:
        env_after = {k: os.environ.get(k) for k in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}
        mp.undo()
    after = production_snapshot()
    res = json.loads(out_json.read_text()) if out_json.exists() else {}
    return {"rc": rc, "out": so.getvalue(), "err": se.getvalue(), "res": res, "base": base, "before": before, "after": after,
            "env_after": env_after, "log": [list(x) for x in dev._LOG]}


# ------------------------------------------------------------------------------------------------------------ the proof
def test_the_harness_passes_end_to_end(run_once):
    assert run_once["rc"] == 0, run_once["out"][-3000:] + run_once["err"]
    assert "RESULT: PASS" in run_once["out"]
    assert [r["scenario"] for r in run_once["res"]["rows"]] == list(EXPECTED)
    assert all(r["ok"] and not r["problems"] for r in run_once["res"]["rows"])


@pytest.mark.parametrize("name", list(EXPECTED))
def test_outcome_of_each_scenario(run_once, name):
    row = next(r for r in run_once["res"]["rows"] if r["scenario"] == name)
    g = row["got"]
    assert (g["gp"], g["gd"], g["np"], g["nd"]) == EXPECTED[name]


def test_all_signed_is_approved_verified_and_passes_a_distribution_gate(run_once):
    row = run_once["res"]["rows"][0]
    assert row["scenario"] == "all-signed" and set(row["states"].values()) == {"approved-verified"} and len(row["states"]) == 5
    assert row["check_about_distribution"] == "pass"


def test_every_refused_distribution_row_shows_a_draft_or_a_placeholder_never_an_approved_slot(run_once):
    for r in run_once["res"]["rows"]:
        if EXPECTED[r["scenario"]][3] == 3:                                  # the About generator refused a distribution build
            st = set(r["states"].values())
            assert st & {"unverified-draft", "pending"} and r["scenario"] != "all-signed", r["scenario"]
    by = {r["scenario"]: r["states"] for r in run_once["res"]["rows"]}
    assert by["decision-open"]["LEGAL.NO_WARRANTY"] == "pending"
    assert set(by["fingerprint-mismatch"].values()) == {"unverified-draft"}
    assert by["section13-na"]["LEGAL.SECTION13_NOTICE"] == "not-applicable"


def test_sidecar_serves_the_slot_state_tags(run_once):
    sc = run_once["res"]["sidecar"]
    assert set(sc) == {"all-signed", "second-key", "missing-record-sig", "decision-open"}
    text = "\n".join("\n".join(v["checks"]) for v in sc.values())
    assert "validate_record (launcher view contract): accepted" in text
    assert "viewer endpoint GET /about/files/licence -> 200" in text
    assert "approved-verified / tag 'Approved text, signature verified'" in text
    assert "unverified-draft / tag 'Unverified draft, not an approved text' / text only after the label" in text
    assert "pending / tag 'Pending a decision'" in text
    assert "REJECTED" not in text


# ------------------------------------------------------------------------------------------------------------ nothing left behind
def test_production_state_is_untouched_and_consistent(run_once):
    assert run_once["before"] == run_once["after"]
    st = run_once["after"]
    assert st["allowed_signers_active_lines"] == len(st["fingerprints"])             # the file lists exactly the keys the gate file pins
    assert st["decisions_total"] == 21 and st["decisions_open"] + st["decisions_decided"] == 21
    assert st["private_keys_in_repo"] == []
    assert "PRODUCTION STATE at end: UNCHANGED" in run_once["out"]


def test_the_throwaway_keys_and_the_workspace_are_gone(run_once):
    assert "CLEANUP: the throwaway keys (dev-signer-key, dev-signer-key.pub, dev-other-key, dev-other-key.pub) were deleted" in run_once["out"]
    assert "workspace directory removed: yes" in run_once["out"]
    assert sorted(p.name for p in run_once["base"].iterdir()) == ["result.json"]           # no dev-signer-e2e-* directory remains


def test_no_agent_no_ssh_directory_no_ssh_add(run_once):
    assert run_once["env_after"] == {"SSH_AUTH_SOCK": None, "SSH_AGENT_PID": None}            # dropped by the harness itself
    log = run_once["log"]
    assert log and not [c for c in log if os.path.basename(c[0]) in ("ssh-add", "ssh-agent")]
    home_ssh = os.path.join(os.path.expanduser("~"), ".ssh")
    assert not [c for c in log if any(home_ssh in a or "/.ssh/" in a for a in c)]
    signs = [c for c in log if os.path.basename(c[0]) == "ssh-keygen" and "-Y" in c and "sign" in c]
    assert len(signs) >= 26 and all("dev-signer-e2e-" in c[c.index("-f") + 1] for c in signs)
    gens = [c for c in log if os.path.basename(c[0]) == "ssh-keygen" and "-t" in c]
    assert len(gens) == 2 and all("-N" in c and c[c.index("-N") + 1] == "" and "dev-signer-e2e-" in c[c.index("-f") + 1] for c in gens)
    assert all("DEV-ONLY" in c[c.index("-C") + 1] for c in gens)
    assert "note: SSH_AUTH_SOCK was set in the caller's environment" in run_once["out"]


def test_fixture_text_is_obviously_synthetic_and_wording_is_clean():
    src = HARNESS.read_text(encoding="utf-8") + "\n" + DOC.read_text(encoding="utf-8")
    assert "DEV FIXTURE TEXT - NOT LEGAL TEXT" in src
    low = src.lower()
    for phrase in ("open source", "open-source", "cleared", "compliant", "approved by counsel", "tamper-proof", "certified"):
        assert phrase not in low, phrase
    src.encode("ascii")


# ------------------------------------------------------------------------------------------------------------ the harness fails when it must
def test_a_key_location_inside_the_repository_is_refused(tmp_path):
    before = sorted(p.name for p in (ROOT / "packaging").iterdir())
    for inside in (ROOT / "packaging", ROOT / "docs", ROOT):
        assert dev.main(["--tmp-base", str(inside), "--no-sidecar"]) == dev.EXIT_REFUSED
    assert sorted(p.name for p in (ROOT / "packaging").iterdir()) == before
    assert not list(ROOT.glob("dev-signer-e2e-*")) and not list((ROOT / "docs").glob("dev-signer-e2e-*"))
    with pytest.raises(dev.Refused):
        dev.check_tmp_base(ROOT / "packaging", ROOT)


def test_an_ssh_or_gpg_directory_is_refused_as_key_location(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (".ssh", ".gnupg"):
        d = tmp_path / name
        d.mkdir()
        with pytest.raises(dev.Refused):
            dev.check_tmp_base(d, ROOT)
        (d / "sub").mkdir()
        assert dev.main(["--tmp-base", str(d / "sub"), "--no-sidecar"]) == dev.EXIT_REFUSED
        assert list((d / "sub").iterdir()) == []                                 # no workspace, no key was created
    with pytest.raises(dev.Refused):
        dev.check_tmp_base(tmp_path / "does-not-exist", ROOT)


def fake_repo(tmp_path: Path) -> Path:
    r = tmp_path / "fake-repo"
    for rel in ("packaging/distribution-gate.json", "packaging/legal-allowed-signers"):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, r / rel)
    return r


def test_the_unmodified_fake_repo_passes_the_start_assertions(tmp_path):
    st = dev.production_state(fake_repo(tmp_path))
    assert dev.production_violations(st) == []


@pytest.mark.parametrize("what", ["signer-line", "fingerprint", "private-key-file"])
def test_the_harness_refuses_to_run_when_production_is_not_consistent(tmp_path, what, capsys):
    r = fake_repo(tmp_path)
    if what == "signer-line":
        with open(r / "packaging" / "legal-allowed-signers", "a") as fh:
            fh.write('someone namespaces="redevops-legal-decision@v1" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPLACEHOLDERPLACEHOLDERPLACEHOLDER0 x\n')
    elif what == "fingerprint":
        doc = json.loads((r / "packaging" / "distribution-gate.json").read_text())
        doc["legal_signer_fingerprints"] = [*doc["legal_signer_fingerprints"], "SHA256:" + "A" * 43]
        (r / "packaging" / "distribution-gate.json").write_text(json.dumps(doc))
    elif what == "private-key-file":
        (r / "stray").mkdir()
        (r / "stray" / "key-file").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nTEST PLACEHOLDER, NOT A KEY\n")
    base = tmp_path / "base"
    base.mkdir()
    rc = dev.main(["--repo-root", str(r), "--tmp-base", str(base), "--no-sidecar"])
    err = capsys.readouterr().err
    assert rc == dev.EXIT_REFUSED and "REFUSED: production state is not consistent" in err
    assert list(base.iterdir()) == []                                              # refused before any key or directory was created


def test_the_harness_is_not_vacuous_a_wrong_expectation_fails_it(tmp_path, monkeypatch):
    real = dev.scenarios

    def wrong(slots_spec, key2):
        rows = real(slots_spec, key2)
        key, title, mutate, expected = rows[5]                                      # second-key: claim that it is still verified
        assert key == "second-key"
        expected = dict(expected, states={k: "approved-verified" for k in slots_spec}, gd=0, nd=0)
        rows[5] = (key, title, mutate, expected)
        return rows
    monkeypatch.setattr(dev, "scenarios", wrong)
    base = tmp_path / "b"
    base.mkdir()
    assert dev.main(["--tmp-base", str(base), "--no-sidecar"]) == 1
    assert list(base.iterdir()) == []                                               # the keys and the workspace are removed on failure too


def test_remove_tree_by_name_deletes_files_individually_and_never_follows_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    top = tmp_path / "top"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "b" / "f").write_text("x")
    (top / "link").symlink_to(outside)
    assert dev.remove_tree_by_name(top) == 2
    assert not top.exists() and (outside / "keep.txt").read_text() == "keep"


def test_the_shipped_production_files_are_consistent_and_hold_no_private_key():
    """Independent of the harness: the production signer list lists exactly the keys the gate file pins, all 21 decisions exist, and every
    DECIDED one names a record that is a file of the repository (the owner's signed approvals)."""
    text = (ROOT / "packaging" / "legal-allowed-signers").read_text()
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    doc = json.loads((ROOT / "packaging" / "distribution-gate.json").read_text())
    assert len(lines) == len(doc["legal_signer_fingerprints"]) and len(doc["decisions"]) == 21
    assert "PRIVATE KEY" not in text
    for d in doc["decisions"]:
        if d["status"] == "DECIDED":
            assert (ROOT / d["decision_reference"]).is_file(), d["id"]
