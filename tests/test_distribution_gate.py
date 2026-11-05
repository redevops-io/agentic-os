"""The distribution gate (packaging/check-distribution-gate.py, packaging/distribution-gate.json).

A DISTRIBUTION build (REDEVOPS_DISTRIBUTION_BUILD=1 or --distribution) must fail while any applicable distribution-blocking
decision is OPEN; a PRIVATE candidate build must pass. No decision is DECIDED by the agent that wrote the file: only the owner
can mark one (a DECIDED entry must carry the written record). Offline; no build.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import Signer, signer  # noqa: F401  (signer is a pytest fixture)

ROOT = Path(__file__).resolve().parents[1]
GATE_PY = ROOT / "packaging" / "check-distribution-gate.py"
GATE_JSON = ROOT / "packaging" / "distribution-gate.json"
REGISTER = ROOT / "packaging" / "DECISIONS-REQUIRED.md"

_spec = importlib.util.spec_from_file_location("_gate", GATE_PY)
gate = importlib.util.module_from_spec(_spec)
sys.modules["_gate"] = gate
_spec.loader.exec_module(gate)


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("APPROVAL_SECRET", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
                                                            "REDEVOPS_DISTRIBUTION_BUILD")}
    env.update(extra)
    return env


def _run(*args, gate_file=None, allowed=None, **env):
    cmd = [sys.executable, "-I", str(GATE_PY), *args]
    if allowed:
        cmd += ["--allowed-signers", str(allowed)]
    if gate_file:
        cmd += ["--gate", str(gate_file), "--root", str(Path(gate_file).parent)]   # records of a test gate live beside it
    return subprocess.run(cmd, capture_output=True, text=True, env=_env(**env), timeout=60)


def _decisions():
    return json.loads(GATE_JSON.read_text(encoding="utf-8"))["decisions"]


def _decided(d, tmp_path, signer=None):
    """A DECIDED copy of `d` with an invented record in tmp_path. With `signer` (a THROWAWAY test key) the record is also signed."""
    d = copy.deepcopy(d)
    record = tmp_path / f"{d['id']}.signed.txt"
    record.write_text(f"redevops-legal-decision: 1\ndecision: {d['id']}\noption: {d['options'][0]['key']}\n\nsigned by the owner\n")
    if signer is not None:
        signer.sign(record)
    d.update(status="DECIDED", chosen_option=d["options"][0]["key"], decision_reference=record.name,
             decision_sha256=hashlib.sha256(record.read_bytes()).hexdigest(), decided_by="owner",
             decided_on="2026-12-01")
    if "counsel" in d["decides"]:
        d["counsel_reference"] = "counsel letter 2026-11-30"
    return d


def _write(tmp_path, decisions, signer=None):
    p = tmp_path / "gate.json"
    doc = {"schema": 1, "decisions": decisions}
    if signer is not None:
        doc["legal_signer_fingerprints"] = [signer.fingerprint]       # the gate file pins the key of the allowed-signers file
    p.write_text(json.dumps(doc))
    return p


# ------------------------------------------------------------------------------------ the committed gate file

def test_committed_gate_file_is_well_formed():
    assert gate.validate(_decisions()) == []


def test_every_register_decision_is_in_the_gate_file():
    """Every distribution-relevant id of the register and the form is present; none is invented."""
    ids = {d["id"] for d in _decisions()}
    assert {f"D-LIC-{n}" for n in range(1, 19)} <= ids
    assert {"pins-03", "pins-04", "pins-08"} <= ids
    register = REGISTER.read_text(encoding="utf-8")
    for i in ids:
        assert i in register, f"{i} is not in packaging/DECISIONS-REQUIRED.md"


def test_the_blocking_set_is_the_documented_one():
    blocking = {d["id"] for d in _decisions() if d["blocks_distribution"]}
    assert {"D-LIC-1", "D-LIC-2", "D-LIC-3", "D-LIC-4", "D-LIC-5", "D-LIC-6", "D-LIC-12", "D-LIC-13", "D-LIC-18",
            "pins-03", "pins-04", "pins-08"} <= blocking                      # the signing request's list plus the pins items
    assert not {"D-LIC-7", "D-LIC-14", "D-LIC-17"} & blocking


def test_the_agent_marked_nothing_decided_in_the_committed_file():
    """Only the owner can decide. If the owner later records decisions, each must be complete (validate covers that);
    this test only guards the state in which the file was written: every entry carries no decision fields while OPEN."""
    for d in _decisions():
        if d["status"] == "OPEN":
            for k in ("chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on", "counsel_reference"):
                assert not d.get(k), (d["id"], k)


# ------------------------------------------------------------------------------------ behaviour

def test_private_candidate_build_passes_and_lists_the_open_decisions():
    r = _run("--platform", "macos")
    assert r.returncode == 0, r.stderr
    assert "PRIVATE CANDIDATE" in r.stdout and "OPEN" in r.stdout


@pytest.mark.skipif(not any(d["blocks_distribution"] and d["status"] == "OPEN" for d in _decisions()),
                    reason="every blocker has been decided by the owner")
@pytest.mark.parametrize("how", ["flag", "env", "env-true"])
def test_distribution_build_fails_while_a_blocker_is_open(how):
    kw = {"flag": dict(args=("--distribution",), env={}), "env": dict(args=(), env={"REDEVOPS_DISTRIBUTION_BUILD": "1"}),
          "env-true": dict(args=(), env={"REDEVOPS_DISTRIBUTION_BUILD": "true"})}[how]
    r = _run("--platform", "macos", *kw["args"], **kw["env"])
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "DISTRIBUTION BUILD REFUSED" in r.stderr and "D-LIC-1" in r.stderr
    assert "OWNER_DECISION_FORM.md" in r.stderr


def test_only_empty_or_zero_means_a_private_build_everything_else_is_distribution():
    """Fail closed (round 4, R4-09): any other value, including 'on', 'enabled', 'y', 'false' and 'no', is a distribution build."""
    for v in ("", "0"):
        assert _run("--platform", "macos", REDEVOPS_DISTRIBUTION_BUILD=v).returncode == 0
    # RC3-03: no trimming. Whitespace-padded values are NOT private (the nit accepted in round 5 is closed)
    for v in (" 0 ", "0 ", " 0", " ", "\t", "0\n", "00", "\u00a0"):
        assert gate.is_distribution(False, {"REDEVOPS_DISTRIBUTION_BUILD": v}) is True, repr(v)
    assert gate.is_distribution(False, {}) is False and gate.is_distribution(False, {"REDEVOPS_DISTRIBUTION_BUILD": ""}) is False
    assert gate.is_distribution(False, {"REDEVOPS_DISTRIBUTION_BUILD": "0"}) is False
    assert gate.is_distribution(True, {"REDEVOPS_DISTRIBUTION_BUILD": "0"}) is True        # the flag wins
    for v in ("1", "true", "yes", "on", "enabled", "y", "false", "no", "TRUE", "2", "off", " ", "0 "):
        assert gate.is_distribution(False, {"REDEVOPS_DISTRIBUTION_BUILD": v}) is True, v
        r = _run("--platform", "macos", REDEVOPS_DISTRIBUTION_BUILD=v)
        assert r.returncode == 1 and "DISTRIBUTION BUILD REFUSED" in r.stderr, v


def test_a_forged_decision_record_is_not_accepted(tmp_path):
    """R4-09: a URL, an absolute path, a '..' path, a missing file or a wrong hash never makes a decision complete."""
    for bad in ("https://example.invalid/fake", "/etc/hosts", "../x", "missing.txt"):
        ds = [_decided(d, tmp_path) if d["blocks_distribution"] else d for d in _decisions()]
        ds[0].update(decision_reference=bad, decision_sha256="0" * 64)
        assert gate.validate(ds, tmp_path), bad
        assert _run("--platform", "all", "--distribution", gate_file=_write(tmp_path, ds)).returncode == 2, bad


def test_a_non_default_gate_file_says_it_is_not_valid_for_signing(tmp_path, signer):
    ds = [_decided(d, tmp_path, signer) if d["blocks_distribution"] else d for d in _decisions()]
    r = _run("--platform", "all", "--distribution", gate_file=_write(tmp_path, ds, signer), allowed=signer.allowed)
    assert r.returncode == 0 and "NOT valid for signing" in r.stderr


def test_all_blockers_decided_with_a_signed_written_record_passes(tmp_path, signer):
    ds = [_decided(d, tmp_path, signer) if d["blocks_distribution"] else d for d in _decisions()]
    p = _write(tmp_path, ds, signer)
    r = _run("--platform", "all", "--distribution", gate_file=p, allowed=signer.allowed)
    assert r.returncode == 0, r.stderr
    assert "DECIDED with a written record" in r.stdout and "valid detached signature" in r.stdout


def test_all_blockers_decided_without_signatures_is_refused_in_a_distribution_build(tmp_path, signer):
    """RC3-01: the hash match alone proves consistency, not authorship: unsigned records never pass a distribution build."""
    ds = [_decided(d, tmp_path) if d["blocks_distribution"] else d for d in _decisions()]
    p = _write(tmp_path, ds)
    r = _run("--platform", "all", "--distribution", gate_file=p, allowed=signer.allowed)
    assert r.returncode == 1 and "no valid detached signature" in r.stderr
    assert _run("--platform", "all", gate_file=p, allowed=signer.allowed).returncode == 0          # a private build only warns


def test_one_open_blocker_is_enough_to_refuse(tmp_path):
    ds = [_decided(d, tmp_path) if d["blocks_distribution"] else d for d in _decisions()]
    ds[0]["status"] = "OPEN"
    for k in ("chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on", "counsel_reference"):
        ds[0][k] = None
    r = _run("--platform", "macos", "--distribution", gate_file=_write(tmp_path, ds))
    assert r.returncode == 1 and ds[0]["id"] in r.stderr


def test_platform_scope_windows_only_blockers_do_not_stop_a_macos_build(tmp_path, signer):
    ds = [_decided(d, tmp_path, signer) if d["blocks_distribution"] and "windows" not in d["scope"] and d["scope"] != ["qa"] else d
          for d in _decisions()]
    p = _write(tmp_path, ds, signer)
    assert _run("--platform", "macos", "--distribution", gate_file=p, allowed=signer.allowed).returncode == 0
    r = _run("--platform", "windows", "--distribution", gate_file=p)
    assert r.returncode == 1 and "pins-08" in r.stderr and "D-LIC-9" in r.stderr
    r = _run("--platform", "qa", "--distribution", gate_file=p)
    assert r.returncode == 1 and "D-LIC-16" in r.stderr


@pytest.mark.parametrize("breakage", [
    lambda d: d.update(chosen_option=None),
    lambda d: d.update(decision_reference=""),
    lambda d: d.update(decision_sha256="abc"),
    lambda d: d.update(decided_by=""),
    lambda d: d.update(decided_on="yesterday"),
    lambda d: d.update(chosen_option="zz"),
])
def test_a_decided_entry_without_the_written_record_is_not_accepted(tmp_path, breakage):
    ds = [_decided(d, tmp_path) if d["blocks_distribution"] else d for d in _decisions()]
    breakage(ds[0])
    p = _write(tmp_path, ds)
    assert _run("--platform", "macos", gate_file=p).returncode == 2           # a malformed gate file is an error even for a private build
    assert _run("--platform", "macos", "--distribution", gate_file=p).returncode == 2


def test_counsel_decisions_need_a_counsel_reference(tmp_path):
    ds = [_decided(d, tmp_path) if d["blocks_distribution"] else d for d in _decisions()]
    counsel = next(d for d in ds if "counsel" in d["decides"])
    counsel["counsel_reference"] = ""
    assert _run("--platform", "all", gate_file=_write(tmp_path, ds)).returncode == 2


def test_the_hash_of_a_record_inside_the_repository_is_verified(tmp_path):
    record = ROOT / "docs" / "legal" / "OWNER_DECISION_FORM.md"
    ds = [_decided(d, tmp_path) if d["blocks_distribution"] else d for d in _decisions()]
    ds[0].update(decision_reference="docs/legal/OWNER_DECISION_FORM.md", decision_sha256="0" * 64)
    # the other decided entries keep their records in tmp_path: resolve them there, the first one against the repository
    assert any("does not match" in e for e in gate.validate(ds[:1], ROOT))
    ds[0]["decision_sha256"] = hashlib.sha256(record.read_bytes()).hexdigest()
    assert gate.validate(ds[:1], ROOT) == []


def test_an_open_entry_with_decision_fields_is_rejected(tmp_path):
    ds = _decisions()
    ds[0]["decided_by"] = "someone"
    assert any("OPEN but carries decision fields" in e for e in gate.validate(ds))


def test_malformed_gate_file_exits_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{}")
    assert _run(gate_file=p).returncode != 0


# ------------------------------------------------------------------------------------ the build scripts honour it

def test_candidate_script_runs_the_gate_first_and_passes_the_flag_through():
    src = (ROOT / "packaging" / "candidate" / "build-local-macos-candidate.sh").read_text(encoding="utf-8")
    gate_call = src.index("check-distribution-gate.py")
    for later in ('mkdir -p "$OUT"', "exec env -i", "git -C \"$ROOT\" status"):
        assert gate_call < src.index(later), later
    assert "|| exit 3" in src[gate_call:gate_call + 120]
    assert "REDEVOPS_DISTRIBUTION_BUILD" in src.split("exec env -i")[0].rsplit("for v in", 1)[1]    # survives the env -i re-exec


def test_build_all_runs_the_gate_before_any_build_step():
    src = (ROOT / "packaging" / "build-all.sh").read_text(encoding="utf-8")
    assert src.index("check-distribution-gate.py") < src.index("build-wheelhouse.sh\"")
    assert "|| exit 3" in src


@pytest.mark.skipif(not any(d["blocks_distribution"] and d["status"] == "OPEN" for d in _decisions()),
                    reason="every blocker has been decided by the owner")
def test_candidate_script_refuses_a_distribution_build_before_touching_anything(tmp_path):
    out = tmp_path / "candidate-out"
    r = subprocess.run(["bash", str(ROOT / "packaging" / "candidate" / "build-local-macos-candidate.sh"), str(out)],
                       capture_output=True, text=True, env=_env(REDEVOPS_DISTRIBUTION_BUILD="1"), timeout=60, cwd=tmp_path)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "DISTRIBUTION BUILD REFUSED" in r.stderr
    assert not out.exists()                       # nothing was created: the refusal precedes every step
