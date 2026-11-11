"""The consolidated owner approval end to end: render, verify, one signature through a real ssh-agent and a real pseudo-terminal, gate acceptance.
THROWAWAY key and agent only (generated in a temp dir, killed and deleted by name); no real key, no real repository is changed."""
from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .about_fixture import ROOT

FILES = ["scripts/release/approval.py", "scripts/release/authorize_release.sh", "packaging/check-distribution-gate.py", "packaging/distribution-gate.json",
         "packaging/legal-allowed-signers", "packaging/release/decision-manifest-0.6.0.json", "scripts/release/release_orchestrate.py", "LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md"]
GENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def run(args, cwd, env=None, **kw):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env or GENV, timeout=180, **kw)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    for rel in FILES:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, r / rel)
    (r / "scripts/release/authorize_release.sh").chmod(0o755)
    for a in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-qm", "F"]):
        run(["git", *a], r)
    return r


@pytest.fixture
def key(tmp_path):
    d = tmp_path / "k"
    d.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "throwaway-legal", "-f", str(d / "legal")], check=True, env={"PATH": "/usr/bin:/bin"})
    out = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout
    sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", out).group(1)
    pid = re.search(r"SSH_AGENT_PID=(\d+);", out).group(1)
    subprocess.run(["ssh-add", str(d / "legal")], check=True, env={"PATH": "/usr/bin:/bin", "SSH_AUTH_SOCK": sock}, capture_output=True)
    fpr = subprocess.run(["ssh-keygen", "-l", "-f", str(d / "legal.pub")], capture_output=True, text=True).stdout.split()[1]
    # the PUBLIC key goes to a separate directory, as the owner would hand it over
    pub = tmp_path / "pub"
    pub.mkdir()
    shutil.copyfile(d / "legal.pub", pub / "legal.pub")
    yield {"pub": pub / "legal.pub", "sock": sock, "fpr": fpr}
    subprocess.run(["kill", pid], check=False)
    for p in (d / "legal", d / "legal.pub"):
        p.unlink(missing_ok=True)


def render(repo, tmp_path):
    out = tmp_path / "approval.md"
    r = run([sys.executable, "-I", "scripts/release/approval.py", "render", "--commit", "HEAD", "--out", str(out)], repo)
    assert r.returncode == 0, r.stderr
    return out


def drive(repo, doc, key, answers, extra=()):
    """Run authorize_release.sh with a controlling pseudo-terminal (it reads /dev/tty) and the throwaway agent; answers are typed at the prompts."""
    env = dict(GENV, SSH_AUTH_SOCK=key["sock"])
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(repo)
        os.execve("/bin/bash", ["bash", "scripts/release/authorize_release.sh", "--doc", str(doc), "--pubkey", str(key["pub"]), *extra], env)
    out, i, deadline = b"", 0, time.time() + 120
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if out.rstrip().endswith((b":", b"]:")) and i < len(answers):
                os.write(fd, (answers[i] + "\n").encode())
                i += 1
                out += b"\n"
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def gate_state(repo):
    return {d["id"]: d for d in json.loads((repo / "packaging/distribution-gate.json").read_text())["decisions"]}


def test_rendered_document_is_deterministic_draft_and_names_only_owner_only_decisions(repo, tmp_path):
    a = render(repo, tmp_path)
    b = tmp_path / "again.md"
    run([sys.executable, "-I", "scripts/release/approval.py", "render", "--commit", "HEAD", "--out", str(b)], repo)
    assert a.read_bytes() == b.read_bytes()
    text = a.read_text()
    head = text.split("\n\n", 1)[0].splitlines()
    assert head[0] == "redevops-legal-decision: 2"
    ids = [ln.split()[1] for ln in head if ln.startswith("decision: ")]
    owner = sorted(d for d, e in gate_state(repo).items() if e["decides"] == "owner")
    assert sorted(ids) == sorted(set(owner) - {"D-LIC-8", "D-LIC-11"})      # struck by the owner 2026-11-11, recorded in the manifest
    assert "D-LIC-8" not in ids and "D-LIC-11" not in ids
    assert "NOT in this signature" in text and "D-LIC-8" in text.split("NOT in this signature", 1)[1][:700]
    assert "The hero image that ships in the Projects UI" not in text and "are first-party artwork of the owner or the company" not in text
    for c in ("D-LIC-3", "D-LIC-1", "pins-03", "D-LIC-18", "pins-08"):
        assert c not in ids
    assert "DRAFT - NOT AUTHORIZED" in text and "Counsel reference" in text
    assert re.search(r"Date: `_+`", text)                                  # never prefilled
    assert "BEGIN" not in text and "ssh-ed25519" not in text and "AZURE_" not in text


def test_verify_refuses_a_tampered_document_and_a_wrong_commit(repo, tmp_path):
    doc = render(repo, tmp_path)
    ok = run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc)], repo)
    assert ok.returncode == 0, ok.stderr
    doc.write_text(doc.read_text().replace("Counsel reference", "Counsel reference (approved)"))
    bad = run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc)], repo)
    assert bad.returncode == 1 and "byte-identical" in bad.stderr
    doc2 = render(repo, tmp_path)
    doc2.write_text(doc2.read_text().replace("decision: D-LIC-13 a", "decision: D-LIC-13 b"))
    assert run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc2)], repo).returncode == 1
    doc3 = render(repo, tmp_path)
    doc3.write_text(doc3.read_text().replace("decision: D-LIC-13 a", "decision: D-LIC-13 a\ndecision: D-LIC-3 d", 1))
    r = run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc3)], repo)
    assert r.returncode == 1 and "not an owner-only decision of the manifest" in r.stderr


def test_dry_run_changes_nothing(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    before = run(["git", "status", "--porcelain"], repo).stdout
    r = run(["bash", "scripts/release/authorize_release.sh", "--doc", str(doc), "--pubkey", str(key["pub"]), "--dry-run"], repo)
    assert r.returncode == 0 and "DRY RUN" in r.stdout and key["fpr"] in r.stdout
    assert run(["git", "status", "--porcelain"], repo).stdout == before == ""
    assert not (repo / "docs/legal/approved").exists()


def test_one_signature_one_commit_and_the_gate_accepts_exactly_the_owner_only_decisions(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    f = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    digest = run([sys.executable, "-I", "scripts/release/approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, out = drive(repo, doc, key, [digest, key["fpr"], "Alex Rybachek"])
    assert code == 0, out
    r = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert r != f and run(["git", "rev-parse", "HEAD^"], repo).stdout.strip() == f
    changed = sorted(run(["git", "diff", "--name-only", f, r], repo).stdout.split())
    assert changed == ["docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md", "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig",
                       "packaging/distribution-gate.json", "packaging/legal-allowed-signers"]
    g = gate_state(repo)
    for i, e in g.items():
        if e["decides"] == "owner" and i in ("D-LIC-8", "D-LIC-11"):
            assert e["status"] == "OPEN" and e["decision_sha256"] is None, i      # struck by the owner: never recorded
        elif e["decides"] == "owner":
            assert e["status"] == "DECIDED" and e["decision_sha256"] == digest and e["decided_on"] == time.strftime("%Y-%m-%d", time.gmtime()), i
        else:
            assert e["status"] == "OPEN", i
    assert key["fpr"] in json.loads((repo / "packaging/distribution-gate.json").read_text())["legal_signer_fingerprints"]
    assert run([sys.executable, "-I", "scripts/release/approval.py", "check"], repo).returncode == 0
    # the gate still refuses a distribution build: the counsel decisions are open
    gr = run([sys.executable, "-I", "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution"], repo)
    assert gr.returncode == 1 and "D-LIC-3" in gr.stderr
    # tamper with the signed document afterwards: the gate notices
    p = repo / "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md"
    p.write_text(p.read_text() + "\nlate edit\n")
    assert run([sys.executable, "-I", "scripts/release/approval.py", "check"], repo).returncode == 1


def test_a_wrong_typed_digest_stops_before_anything_is_written(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    code, out = drive(repo, doc, key, ["0" * 64])
    assert code != 0 and "typed digest differs" in out
    assert run(["git", "status", "--porcelain"], repo).stdout == ""


def test_the_release_key_and_a_dirty_tree_are_refused(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    rel = tmp_path / "rel.pub"
    shutil.copyfile(ROOT / "packaging/signing/release-signing-key.pub", rel)
    r = run(["bash", "scripts/release/authorize_release.sh", "--doc", str(doc), "--pubkey", str(rel), "--dry-run"], repo)
    assert r.returncode == 1 and "release key" in r.stderr
    (repo / "stray.txt").write_text("x")
    r = run(["bash", "scripts/release/authorize_release.sh", "--doc", str(doc), "--pubkey", str(key["pub"]), "--dry-run"], repo)
    assert r.returncode == 1 and "not clean" in r.stderr


def test_the_scripts_never_read_a_private_key_or_ssh_directory():
    for rel in ("scripts/release/approval.py", "scripts/release/authorize_release.sh"):
        src = (ROOT / rel).read_text()
        for bad in ("expanduser", "Path.home", "$HOME/.ssh", ".ssh/id_", "BEGIN OPENSSH PRIVATE KEY", "cat \"$HOME"):
            assert bad not in src, (rel, bad)


# ---------------------------------------------------------------------------------------------------- review findings (independent review of the release tooling)
def sign_and_commit_with(repo, tmp_path, key, doc):
    """Make a release commit exactly as authorize_release.sh does, but with whatever key and document the caller chooses (an 'agent forgery')."""
    sk = tmp_path / "attacker"
    sk.mkdir(exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(sk / "k")], check=True, env={"PATH": "/usr/bin:/bin"})
    (repo / "docs/legal/approved").mkdir(parents=True, exist_ok=True)
    r = run([sys.executable, "-I", "scripts/release/approval.py", "apply", "--doc", str(doc), "--pubkey", str(sk / "k.pub"), "--decided-by", "x", "--date", "2026-12-01"], repo)
    assert r.returncode == 0, r.stderr
    subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(sk / "k"), "-n", "redevops-legal-decision@v1", str(doc)], check=True, env={"PATH": "/usr/bin:/bin"})
    shutil.copyfile(str(doc) + ".sig", repo / "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig")
    fp = subprocess.run(["ssh-keygen", "-l", "-f", str(sk / "k.pub")], capture_output=True, text=True).stdout.split()[1]
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", f"forged\n\nSigner-key-fingerprint: {fp}\n"], repo)
    return fp


def s0_rows(repo, expect):
    r = run([sys.executable, "-I", "scripts/release/release_orchestrate.py", "--run", "--stages", "S0", "--expect-signer-fingerprint", expect, "--workspace", str(repo), "--evidence-dir", str(repo.parent / "ev")], repo)
    return r.stdout


def test_s0_rejects_a_release_commit_signed_by_a_key_the_owner_did_not_name(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    forged_fp = sign_and_commit_with(repo, tmp_path, key, doc)
    out = s0_rows(repo, key["fpr"])                                   # the owner's real fingerprint
    assert "`FAIL` signer lists" in out
    out2 = s0_rows(repo, forged_fp)                                   # only the fingerprint the commit itself chose would pass that row
    assert "`PASS` both signer lists name exactly one key, the one you named" in out2
    no_expect = run([sys.executable, "-I", "scripts/release/release_orchestrate.py", "--run", "--stages", "S0", "--workspace", str(repo), "--evidence-dir", str(repo.parent / "ev")], repo).stdout
    assert "no --expect-signer-fingerprint" in no_expect


def test_s0_notices_a_deleted_last_decision_and_extra_files(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    sign_and_commit_with(repo, tmp_path, key, doc)
    g = json.loads((repo / "packaging/distribution-gate.json").read_text())
    g["decisions"].pop()                                              # pins-08 is last
    (repo / "packaging/distribution-gate.json").write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
    (repo / "packaging/distribution-gate.json packaging_legal-allowed-signers").write_text("x")
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit"], repo)
    out = s0_rows(repo, "SHA256:" + "A" * 43)
    assert "`FAIL` gate file: unexpected changes" in out and "decision list" in out
    assert "`FAIL` R differs from F=HEAD^ in exactly the four approval paths" in out


def test_verify_refuses_a_struck_document_with_rewritten_prose_but_accepts_the_rendered_strike(repo, tmp_path):
    base = [sys.executable, "-I", "scripts/release/approval.py"]
    struck = tmp_path / "struck.md"
    assert run([*base, "render", "--commit", "HEAD", "--out", str(struck), "--strike", "D-LIC-2"], repo).returncode == 0
    assert "decision: D-LIC-2" not in struck.read_text().split("\n\n")[0] and "Struck by the owner" in struck.read_text()
    assert run([*base, "verify", "--doc", str(struck)], repo).returncode == 0
    struck.write_text(struck.read_text().replace("You are NOT authorizing", "You are authorizing anything"))
    r = run([*base, "verify", "--doc", str(struck)], repo)
    assert r.returncode == 1 and "byte-identical" in r.stderr


@pytest.mark.parametrize("edit", [
    lambda h: "\n".join(l for l in h.split("\n") if not l.startswith("decision: ")),                          # zero decisions
    lambda h: h.replace("\n\n", "\nslot: LEGAL.NO_WARRANTY sha256 " + "d" * 64 + "\n\n", 1),                # slot line
    lambda h: h.replace("\n\n", "\nsurprise: 1\n\n", 1),                                                   # unknown key
])
def test_verify_uses_the_gates_strict_header_parser(repo, tmp_path, edit):
    doc = render(repo, tmp_path)
    doc.write_text(edit(doc.read_text()))
    r = run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc)], repo)
    assert r.returncode == 1


def test_check_fails_without_a_signature_and_for_non_blocking_or_unknown_ids(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    run([sys.executable, "-I", "scripts/release/approval.py", "apply", "--doc", str(doc), "--pubkey", str(key["pub"]), "--decided-by", "x", "--date", "2026-12-01"], repo)
    r = run([sys.executable, "-I", "scripts/release/approval.py", "check"], repo)
    assert r.returncode == 1 and "signature" in r.stderr                       # applied, never signed
    # a header listing only a non-blocking and an unknown decision
    text = doc.read_text().split("\n\n", 1)
    head = [l for l in text[0].split("\n") if not l.startswith("decision: ")] + ["decision: D-LIC-7 b", "decision: D-LIC-99 a"]
    (repo / "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md").write_text("\n".join(head) + "\n\n" + text[1])
    r = run([sys.executable, "-I", "scripts/release/approval.py", "check"], repo)
    assert r.returncode == 1 and "D-LIC-99" in r.stderr


def test_a_gate_file_that_flips_a_counsel_decision_to_owner_is_refused(repo, tmp_path, key):
    g = json.loads((repo / "packaging/distribution-gate.json").read_text())
    for d in g["decisions"]:
        if d["id"] in ("D-LIC-3", "pins-03"):
            d["decides"] = "owner"
    (repo / "packaging/distribution-gate.json").write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "flip"], repo)
    r = run([sys.executable, "-I", "scripts/release/approval.py", "render", "--commit", "HEAD", "--out", str(tmp_path / "x.md")], repo)
    assert r.returncode != 0 and "counsel-named" in r.stderr


def test_the_frozen_counsel_set_equals_the_gate_files_counsel_decisions():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_g", ROOT / "packaging/check-distribution-gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    named = {d["id"] for d in json.loads((ROOT / "packaging/distribution-gate.json").read_text())["decisions"] if "counsel" in d["decides"]}
    assert named == set(mod.COUNSEL_NAMED)


def test_the_name_typed_must_equal_the_name_in_the_document(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    digest = run([sys.executable, "-I", "scripts/release/approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, out = drive(repo, doc, key, [digest, key["fpr"], "Someone Else"])
    assert code != 0 and "typed name differs" in out
    assert run(["git", "status", "--porcelain"], repo).stdout == ""


def test_authorize_with_a_struck_document_records_only_the_remaining_decisions(repo, tmp_path, key):
    doc = tmp_path / "struck.md"
    assert run([sys.executable, "-I", "scripts/release/approval.py", "render", "--commit", "HEAD", "--out", str(doc), "--strike", "D-LIC-2"], repo).returncode == 0
    digest = run([sys.executable, "-I", "scripts/release/approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, out = drive(repo, doc, key, [digest, key["fpr"], "Alex Rybachek"])
    assert code == 0, out
    g = gate_state(repo)
    assert g["D-LIC-2"]["status"] == "OPEN" and g["D-LIC-13"]["status"] == "DECIDED" and g["D-LIC-8"]["status"] == "OPEN"
    assert run([sys.executable, "-I", "scripts/release/approval.py", "check"], repo).returncode == 0


def test_the_gate_file_formatting_is_preserved(repo, tmp_path, key):
    before = (repo / "packaging/distribution-gate.json").read_text()
    doc = render(repo, tmp_path)
    digest = run([sys.executable, "-I", "scripts/release/approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, _ = drive(repo, doc, key, [digest, key["fpr"], "Alex Rybachek"])
    assert code == 0
    after = (repo / "packaging/distribution-gate.json").read_text()
    import difflib
    changed = sum(1 for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0) if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
    assert changed < 120, changed                       # the status fields of nine entries and one fingerprint, not a reformat of the file


def test_s0_rejects_a_signature_by_a_pre_listed_attacker_key_even_if_the_owner_key_is_added(repo, tmp_path, key):
    """Finding N1: F already lists the attacker's key; the document is signed by the attacker; the owner's PUBLIC key is added by apply."""
    atk = tmp_path / "atk"
    atk.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(atk / "k")], check=True, env={"PATH": "/usr/bin:/bin"})
    afp = subprocess.run(["ssh-keygen", "-l", "-f", str(atk / "k.pub")], capture_output=True, text=True).stdout.split()[1]
    pub = (atk / "k.pub").read_text().split()
    (repo / "packaging/legal-allowed-signers").write_text(f'atk namespaces="redevops-legal-decision@v1" {pub[0]} {pub[1]}\n')
    g = json.loads((repo / "packaging/distribution-gate.json").read_text())
    g["legal_signer_fingerprints"] = [afp]
    (repo / "packaging/distribution-gate.json").write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n")
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "F2 lists a signer"], repo)
    doc = render(repo, tmp_path)
    run([sys.executable, "-I", "scripts/release/approval.py", "apply", "--doc", str(doc), "--pubkey", str(key["pub"]), "--decided-by", "x", "--date", "2026-12-01"], repo)
    subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(atk / "k"), "-n", "redevops-legal-decision@v1", str(doc)], check=True, env={"PATH": "/usr/bin:/bin"})
    shutil.copyfile(str(doc) + ".sig", repo / "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig")
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", f"r\n\nSigner-key-fingerprint: {key['fpr']}\n"], repo)
    out = s0_rows(repo, key["fpr"])
    assert "`FAIL` signer lists" in out or "`FAIL` signature by the named key" in out
    assert "`PASS` the signature verifies against a list that holds only the key you named" not in out


def test_a_crafted_signer_line_is_read_the_way_the_gate_reads_it(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    sign_and_commit_with(repo, tmp_path, key, doc)
    own = key["pub"].read_text().split()
    line = (repo / "packaging/legal-allowed-signers").read_text().strip().splitlines()[-1]
    atk_blob = line.split()[-1]
    crafted = f'p namespaces="redevops-legal-decision@v1\\" ssh-ed25519 {own[1]} \\"" ssh-ed25519 {atk_blob}\n'
    (repo / "packaging/legal-allowed-signers").write_text(crafted)
    run(["git", "add", "-A"], repo)
    run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "--amend", "--no-edit"], repo)
    out = s0_rows(repo, key["fpr"])
    assert "`PASS` both signer lists name exactly one key" not in out


def test_s8_refuses_unless_every_earlier_stage_passed_in_the_same_run(repo, tmp_path):
    bare = tmp_path / "remote.git"
    run(["git", "init", "-q", "--bare", str(bare)], tmp_path)
    run(["git", "remote", "add", "origin", str(bare)], repo)
    r = run([sys.executable, "-I", "scripts/release/release_orchestrate.py", "--run", "--stages", "S8", "--push", "--remote", "origin", "--workspace", str(repo), "--evidence-dir", str(tmp_path / "ev")], repo)
    assert "refused: stages that did not PASS in this same run" in r.stdout
    assert run(["git", "ls-remote", str(bare)], tmp_path).stdout.strip() == ""
    r = run([sys.executable, "-I", "scripts/release/release_orchestrate.py", "--run", "--stages", "S8", "--push", "--remote", "--upload-pack=x", "--workspace", str(repo)], repo)
    assert r.returncode != 0 or "plain remote name" in r.stdout


def test_a_document_that_covers_a_decision_the_owner_struck_is_refused(repo, tmp_path):
    doc = render(repo, tmp_path)
    doc.write_text(doc.read_text().replace("decision: D-LIC-13 a", "decision: D-LIC-13 a\ndecision: D-LIC-8 a", 1))
    r = run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc)], repo)
    assert r.returncode == 1 and "struck by the owner" in r.stderr


def test_the_struck_artwork_decisions_stay_open_and_keep_blocking(repo, tmp_path, key):
    doc = render(repo, tmp_path)
    digest = run([sys.executable, "-I", "scripts/release/approval.py", "digest", "--doc", str(doc)], repo).stdout.strip()
    code, out = drive(repo, doc, key, [digest, key["fpr"], "Alex Rybachek"])
    assert code == 0, out
    g = gate_state(repo)
    for i in ("D-LIC-8", "D-LIC-11"):
        assert g[i]["status"] == "OPEN" and g[i]["blocks_distribution"] is True
    r = run([sys.executable, "-I", "packaging/check-distribution-gate.py", "--platform", "macos", "--distribution"], repo)
    assert r.returncode == 1 and "D-LIC-8" in r.stderr and "D-LIC-11" in r.stderr


def test_the_manifest_cannot_strike_a_counsel_decision_or_give_no_reason(repo, tmp_path):
    import json as _j
    p = repo / "packaging/release/decision-manifest-0.6.0.json"
    m = _j.loads(p.read_text())
    m["decisions"]["D-LIC-3"]["struck"] = "no"
    p.write_text(_j.dumps(m, indent=2) + "\n")
    run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "strike counsel"], repo)
    r = run([sys.executable, "-I", "scripts/release/approval.py", "render", "--commit", "HEAD", "--out", str(tmp_path / "x.md")], repo)
    assert r.returncode != 0 and "not an owner-only decision" in (r.stderr + r.stdout)
    m["decisions"]["D-LIC-3"].pop("struck")
    m["decisions"]["D-LIC-8"]["struck"] = "  "
    p.write_text(_j.dumps(m, indent=2) + "\n")
    run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "strike without reason"], repo)
    r = run([sys.executable, "-I", "scripts/release/approval.py", "render", "--commit", "HEAD", "--out", str(tmp_path / "y.md")], repo)
    assert r.returncode != 0 and "without a reason" in (r.stderr + r.stdout)
