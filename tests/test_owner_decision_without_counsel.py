"""The owner's decision of the counsel-named decisions WITHOUT counsel (record header version 3): an explicit waiver, never a counsel approval.
THROWAWAY keys and agents only (generated in a temp dir, killed and deleted by name); no real key and no real repository is changed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

from .about_fixture import ROOT
from .test_release_authorization import FILES, GENV, drive, gate_state, key, render, run  # noqa: F401  (key is a fixture)

GATE_PY = ROOT / "packaging" / "check-distribution-gate.py"
_spec = importlib.util.spec_from_file_location("_gate_under_test_v3", GATE_PY)
GATE = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = GATE
_spec.loader.exec_module(GATE)

SLOT_FILES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs/legal/approved").glob("*.txt"))
EXTRA = ["scripts/release/waiver_approval.py", "scripts/release/arrange_native.py", "packaging/signing/release-signing-key.pub", *SLOT_FILES]
C, T, M = "a" * 40, "b" * 40, "c" * 64
PY = [sys.executable, "-I"]


def v3(decisions: dict, slots: dict | None = None, **over) -> str:
    h = {"bundle": "T", "base-commit": C, "base-tree": T, "manifest-sha256": M, "counsel-consulted": "no", "waiver": "owner-decision-without-counsel"}
    h.update(over)
    lines = ["redevops-legal-decision: 3"] + [f"{k}: {v}" for k, v in h.items() if v is not None]
    lines += [f"decision: {i} {o}" for i, o in decisions.items()]
    lines += [f"slot: {s} {v}" for s, v in (slots or {}).items()]
    return "\n".join(lines) + "\n\nbody\n"


# ------------------------------------------------------------------------------------------------ the record format
def test_a_version_3_record_parses_and_binds_slots_to_decisions():
    sha = "d" * 64
    hdr, probs = GATE.parse_bundle_record(v3({"D-LIC-1": "o", "D-LIC-12": "o"}, {"LEGAL.COPYRIGHT_LINE": f"sha256 {sha}", "LEGAL.SOURCE_OFFER": f"sha256 {sha}"}).encode())
    assert probs == [] and hdr["version"] == "3" and hdr["slots"]["LEGAL.COPYRIGHT_LINE"] == sha


@pytest.mark.parametrize("record,needle", [
    (v3({"D-LIC-2": "a"}), "only counsel-named decisions"),                                                  # an owner-only decision is not for this record
    (v3({"D-LIC-1": "o"}, **{"counsel-consulted": None}), "counsel-consulted: no"),
    (v3({"D-LIC-1": "o"}, **{"waiver": None}), "waiver: owner-decision-without-counsel"),
    (v3({"D-LIC-1": "o"}, **{"counsel-consulted": "yes"}), "counsel-consulted: no"),
    (v3({"D-LIC-1": "o"}, {"LEGAL.SOURCE_OFFER": "sha256 " + "d" * 64}), "belongs to D-LIC-12"),             # a slot whose primary decision is not listed
    (v3({"D-LIC-1": "o"}, {"LEGAL.NOPE": "sha256 " + "d" * 64}), "unknown slot"),
    (v3({"D-LIC-1": "o"}, {"LEGAL.COPYRIGHT_LINE": "not-applicable"}), "cannot be not-applicable"),
    (v3({"D-LIC-1": "o"}, {"LEGAL.COPYRIGHT_LINE": "sha256 " + "d" * 64, "LEGAL.COPYRIGHT_LINE ": "sha256 " + "e" * 64}), ""),
])
def test_malformed_version_3_records_are_refused(record, needle):
    _, probs = GATE.parse_bundle_record(record.encode())
    assert probs or needle == "", record
    if needle:
        assert any(needle in p for p in probs), probs


def test_a_version_2_record_still_refuses_slots_and_counsel_ids():
    rec = "redevops-legal-decision: 2\nbundle: T\nbase-commit: " + C + "\nbase-tree: " + T + "\nmanifest-sha256: " + M + "\ndecision: D-LIC-2 a\nslot: LEGAL.COPYRIGHT_LINE sha256 " + "d" * 64 + "\n"
    _, probs = GATE.parse_bundle_record(rec.encode())
    assert any("cannot authorize an About-slot" in p for p in probs)
    d = {"id": "D-LIC-3", "decides": "counsel", "chosen_option": "o", "slot_texts": None}
    hdr, _ = GATE.parse_bundle_record(("redevops-legal-decision: 2\nbundle: T\nbase-commit: " + C + "\nbase-tree: " + T + "\nmanifest-sha256: " + M + "\ndecision: D-LIC-3 o\n").encode())
    assert any("never one that names counsel" in p for p in GATE.bundle_binding_problems(d, hdr))


def test_the_waiver_reference_must_be_exactly_the_hash_of_the_signed_record():
    sha = "f" * 64
    hdr, probs = GATE.parse_bundle_record(v3({"D-LIC-3": "o"}).encode())
    assert probs == []
    ok = {"id": "D-LIC-3", "decides": "counsel", "chosen_option": "o", "counsel_reference": "owner-waiver:" + sha}
    assert GATE.waiver_binding_problems(ok, hdr, sha) == []
    for ref in ("", "owner-waiver:" + "0" * 64, "Counsel Opinion 2026-11-12", "owner-waiver:" + sha + " "):
        assert any("counsel_reference must be exactly" in p for p in GATE.waiver_binding_problems(dict(ok, counsel_reference=ref), hdr, sha)), ref
    assert any("names option" in p for p in GATE.waiver_binding_problems(dict(ok, chosen_option="a"), hdr, sha))
    assert any("does not list" in p for p in GATE.waiver_binding_problems(dict(ok, id="D-LIC-4"), hdr, sha))
    # slots in the record but not in the entry, and the other way round
    hdr2, _ = GATE.parse_bundle_record(v3({"D-LIC-1": "o"}, {"LEGAL.COPYRIGHT_LINE": "sha256 " + "d" * 64}).encode())
    e = {"id": "D-LIC-1", "decides": "owner+counsel", "chosen_option": "o", "counsel_reference": "owner-waiver:" + sha}
    assert any("authorizes different slots" in p for p in GATE.waiver_binding_problems(e, hdr2, sha))
    e["slot_texts"] = {"LEGAL.COPYRIGHT_LINE": {"file": "docs/legal/approved/x.txt", "sha256": "e" * 64}}
    assert any("text hash differs" in p for p in GATE.waiver_binding_problems(e, hdr2, sha))
    e["slot_texts"]["LEGAL.COPYRIGHT_LINE"]["sha256"] = "d" * 64
    assert GATE.waiver_binding_problems(e, hdr2, sha) == []


# ------------------------------------------------------------------------------------------------ end to end in a throwaway repository
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    for rel in [*FILES, *EXTRA]:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, r / rel)
    (r / "scripts/release/authorize_release.sh").chmod(0o755)
    for a in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-qm", "F"]):
        run(["git", *a], r)
    return r


def first_approval(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    digest = run([*PY, "scripts/release/approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, out = drive(repo, doc, key, [digest, key["fpr"], "Alex Rybachek"])
    assert code == 0, out


def waiver_doc(repo, tmp_path, name="waiver.md"):
    out = tmp_path / name
    r = run([*PY, "scripts/release/waiver_approval.py", "render", "--commit", "HEAD", "--out", str(out)], repo)
    assert r.returncode == 0, r.stderr
    return out


def sign_waiver(repo, tmp_path, key, doc=None):
    doc = doc or waiver_doc(repo, tmp_path)
    digest = run([*PY, "scripts/release/waiver_approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, out = drive(repo, doc, key, [digest, key["fpr"], "Alex Rybachek"], extra=("--waiver",))
    assert code == 0, out
    return digest


def test_the_waiver_document_is_a_deterministic_draft_that_says_no_counsel_was_consulted(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    a, b = waiver_doc(repo, tmp_path, "a.md"), waiver_doc(repo, tmp_path, "b.md")
    assert a.read_bytes() == b.read_bytes()
    text = a.read_text()
    head = text.split("\n\n", 1)[0].splitlines()
    assert head[0] == "redevops-legal-decision: 3" and "counsel-consulted: no" in head and "waiver: owner-decision-without-counsel" in head
    ids = sorted(ln.split()[1] for ln in head if ln.startswith("decision: "))
    assert ids == sorted(GATE.COUNSEL_NAMED)
    assert len([ln for ln in head if ln.startswith("slot: ")]) == 5
    assert "DRAFT - NOT AUTHORIZED" in text and "No counsel was consulted" in text and "not a legal opinion" in text
    assert re.search(r"Date: `_+`", text) and "BEGIN" not in text
    for slot_file in SLOT_FILES:
        assert (ROOT / slot_file).read_text().rstrip("\n") in text
    assert run([*PY, "scripts/release/waiver_approval.py", "verify", "--doc", str(a)], repo).returncode == 0


def test_verify_refuses_a_hand_edited_document(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    doc = waiver_doc(repo, tmp_path)
    edits = [("decision: D-LIC-3 o", "decision: D-LIC-3 a"), ("counsel-consulted: no", "counsel-consulted: yes"),
             ("No counsel was consulted", "Counsel was consulted"), ("decision: D-LIC-3 o", "decision: D-LIC-3 o\ndecision: D-LIC-2 a")]
    for old, new in edits:
        bad = tmp_path / "bad.md"
        bad.write_text(doc.read_text().replace(old, new, 1))
        r = run([*PY, "scripts/release/waiver_approval.py", "verify", "--doc", str(bad)], repo)
        assert r.returncode == 1, (old, new)


def test_render_refuses_when_a_decision_is_already_recorded(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    sign_waiver(repo, tmp_path, key)
    r = run([*PY, "scripts/release/waiver_approval.py", "render", "--commit", "HEAD", "--out", str(tmp_path / "again.md")], repo)
    assert r.returncode != 0 and "recorded once" in (r.stderr + r.stdout)


def test_the_chain_two_signatures_two_commits_and_the_gate_allows_a_distribution_build(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    r1 = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    # before the waiver the gate still refuses
    g0 = run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo)
    assert g0.returncode == 1 and "D-LIC-3" in g0.stderr
    digest = sign_waiver(repo, tmp_path, key)
    r2 = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert r2 != r1 and run(["git", "rev-parse", "HEAD^"], repo).stdout.strip() == r1
    changed = sorted(run(["git", "diff", "--name-only", r1, r2], repo).stdout.split())
    assert changed == ["docs/legal/approved/OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md", "docs/legal/approved/OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md.sig",
                       "packaging/distribution-gate.json"]
    g = gate_state(repo)
    for i in GATE.COUNSEL_NAMED:
        e = g[i]
        assert e["status"] == "DECIDED" and e["chosen_option"] == "o" and e["decision_sha256"] == digest, i
        assert e["counsel_reference"] == "owner-waiver:" + digest, i                  # the reference says what it is; no counsel reference is invented
        assert e["decided_on"] == time.strftime("%Y-%m-%d", time.gmtime()), i
    assert set(g["D-LIC-1"]["slot_texts"]) == {"LEGAL.COPYRIGHT_LINE", "LEGAL.LICENCE_STATEMENT"}
    assert set(g["D-LIC-12"]["slot_texts"]) == {"LEGAL.SOURCE_OFFER"} and set(g["D-LIC-10"]["slot_texts"]) == {"LEGAL.NO_WARRANTY"} and set(g["D-LIC-5"]["slot_texts"]) == {"LEGAL.SECTION13_NOTICE"}
    assert "slot_texts" not in g["D-LIC-3"]
    assert run([*PY, "scripts/release/waiver_approval.py", "check"], repo).returncode == 0
    assert run([*PY, "scripts/release/approval.py", "check"], repo).returncode == 0
    for platform in ("macos", "windows", "linux"):
        gr = run([*PY, "packaging/check-distribution-gate.py", "--platform", platform, "--distribution", "--about-slots"], repo)
        assert gr.returncode == 0, (platform, gr.stderr)
    # the slot texts are shown as approved texts, without a separate signature on each file
    mod = GATE
    gate = json.loads((repo / "packaging/distribution-gate.json").read_text())
    slots, errs, refusals = mod.resolve_about_slots(gate["decisions"], repo, True, allowed_signers=repo / "packaging/legal-allowed-signers", signers=gate["legal_signer_fingerprints"])
    assert errs == [] and refusals == [] and {s["state"] for s in slots.values()} == {"approved-verified"}
    assert not list((repo / "docs/legal/approved").glob("*.txt.sig"))


def test_tampering_after_signing_is_noticed_everywhere(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    sign_waiver(repo, tmp_path, key)
    gate_path = repo / "packaging/distribution-gate.json"
    snapshot = gate_path.read_text()
    # 1. a slot text edited after signing: the text no longer matches its hash, the gate refuses
    t = repo / "docs/legal/approved/source-offer.en.txt"
    original = t.read_text()
    t.write_text(original + "Free software for everyone.\n")
    gr = run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo)
    assert gr.returncode != 0
    t.write_text(original)
    assert run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo).returncode == 0
    # 2. the entry's slot hash edited together with the text: not what the owner signed
    g = json.loads(snapshot)
    new_text = original + "Edited.\n"
    t.write_text(new_text)
    for d in g["decisions"]:
        if d["id"] == "D-LIC-12":
            d["slot_texts"]["LEGAL.SOURCE_OFFER"]["sha256"] = hashlib.sha256(new_text.encode()).hexdigest()
    gate_path.write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
    assert run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo).returncode != 0
    t.write_text(original)
    # 3. a counsel_reference that claims a counsel or points at another record
    for ref in ("Counsel opinion 2026-11-12", "owner-waiver:" + "0" * 64):
        g = json.loads(snapshot)
        for d in g["decisions"]:
            if d["id"] == "D-LIC-3":
                d["counsel_reference"] = ref
        gate_path.write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
        r = run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo)
        assert r.returncode != 0, ref
        assert run([*PY, "scripts/release/waiver_approval.py", "check"], repo).returncode == 1, ref
    # 4. another option than the signed one
    g = json.loads(snapshot)
    for d in g["decisions"]:
        if d["id"] == "D-LIC-5":
            d["chosen_option"] = "a"
    gate_path.write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
    assert run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo).returncode != 0
    # 5. the signed document edited
    gate_path.write_text(snapshot)
    doc = repo / "docs/legal/approved/OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md"
    doc.write_text(doc.read_text() + "late edit\n")
    assert run([*PY, "scripts/release/waiver_approval.py", "check"], repo).returncode == 1


def test_an_unsigned_waiver_never_opens_the_gate(repo, tmp_path, key):
    """The same gate entries written by hand, without a signature: a private build only warns, a distribution build refuses."""
    first_approval(repo, tmp_path, key)
    doc = waiver_doc(repo, tmp_path)
    target = repo / "docs/legal/approved/OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md"
    target.write_bytes(doc.read_bytes())
    sha = hashlib.sha256(doc.read_bytes()).hexdigest()
    gate_path = repo / "packaging/distribution-gate.json"
    g = json.loads(gate_path.read_text())
    for d in g["decisions"]:
        if d["id"] in GATE.COUNSEL_NAMED:
            d.update(status="DECIDED", chosen_option="o", decision_reference="docs/legal/approved/OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md",
                     decision_sha256=sha, decided_by="forger", decided_on="2026-11-12", counsel_reference="owner-waiver:" + sha)
    for d in g["decisions"]:
        if d["id"] == "D-LIC-1":
            d["slot_texts"] = {"LEGAL.COPYRIGHT_LINE": {"file": "docs/legal/approved/copyright-line.en.txt", "sha256": hashlib.sha256((repo / "docs/legal/approved/copyright-line.en.txt").read_bytes()).hexdigest()}}
    gate_path.write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
    r = run([*PY, "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution", "--about-slots"], repo)
    assert r.returncode != 0
    assert run([*PY, "scripts/release/waiver_approval.py", "check"], repo).returncode == 1


def test_the_orchestrator_accepts_the_chain_and_names_the_owner_decisions(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    sign_waiver(repo, tmp_path, key)
    r = run([*PY, "scripts/release/release_orchestrate.py", "--run", "--stages", "S0", "--expect-signer-fingerprint", key["fpr"], "--workspace", str(repo),
             "--evidence-dir", str(repo.parent / "ev")], repo)
    out = r.stdout
    rows = [ln for ln in out.splitlines() if ln.startswith("* `")]
    fails = [ln for ln in rows if ln.startswith("* `FAIL`") and "protected" not in ln]
    assert not fails, "\n".join(fails)                                      # only the synthetic repository's missing protected refs may fail
    for needle in ("R2 differs from F2=HEAD^ in exactly the waiver approval paths", "the first release commit R differs from its parent F in exactly the four approval paths",
                   "the waiver document is byte-identical", "verification accepts the owner's signature for every decision and slot",
                   "owner decisions WITHOUT counsel"):
        assert needle in out, needle


def test_the_orchestrator_refuses_a_waiver_commit_with_extra_paths(repo, tmp_path, key):
    first_approval(repo, tmp_path, key)
    sign_waiver(repo, tmp_path, key)
    (repo / "scripts/release/extra.py").write_text("print('x')\n")
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit"], repo)
    r = run([*PY, "scripts/release/release_orchestrate.py", "--run", "--stages", "S0", "--expect-signer-fingerprint", key["fpr"], "--workspace", str(repo),
             "--evidence-dir", str(repo.parent / "ev")], repo)
    assert "`FAIL` R2 differs from F2=HEAD^" in r.stdout


def test_the_waiver_scripts_never_read_a_private_key_or_ssh_directory():
    for rel in ("scripts/release/waiver_approval.py", "scripts/release/authorize_release.sh"):
        src = (ROOT / rel).read_text()
        for bad in ("expanduser", "Path.home", "$HOME/.ssh", ".ssh/id_", "BEGIN OPENSSH PRIVATE KEY", "cat \"$HOME"):
            assert bad not in src, (rel, bad)


def test_every_counsel_named_decision_has_the_owner_option_and_a_statement():
    gate = json.loads((ROOT / "packaging/distribution-gate.json").read_text())
    man = json.loads((ROOT / "packaging/release/decision-manifest-0.6.0.json").read_text())
    by = {d["id"]: d for d in gate["decisions"]}
    assert set(man["counsel_waiver"]["decisions"]) == set(GATE.COUNSEL_NAMED)
    for i, m in man["counsel_waiver"]["decisions"].items():
        opts = {o["key"]: o["text"] for o in by[i]["options"]}
        assert m["option"] in opts and opts[m["option"]].startswith("Owner decision without counsel"), i
        assert len(m["owner_statement"]) > 40, i
    for slot, spec in man["counsel_waiver"]["slots"].items():
        raw = (ROOT / spec["file"]).read_bytes()
        assert raw.isascii() and b"\r" not in raw and raw.strip() and GATE.SLOT_TEXT_RE.match(spec["file"]), slot
