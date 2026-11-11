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
         "packaging/legal-allowed-signers", "packaging/release/decision-manifest-0.6.0.json", "LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md"]
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
    assert sorted(ids) == owner
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
    doc2.write_text(doc2.read_text().replace("decision: D-LIC-8 a", "decision: D-LIC-8 b"))
    assert run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc2)], repo).returncode == 1
    doc3 = render(repo, tmp_path)
    doc3.write_text(doc3.read_text().replace("decision: D-LIC-8 a", "decision: D-LIC-8 a\ndecision: D-LIC-3 d", 1))
    r = run([sys.executable, "-I", "scripts/release/approval.py", "verify", "--doc", str(doc3)], repo)
    assert r.returncode == 1 and "not an owner-only gate decision" in r.stderr


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
    code, out = drive(repo, doc, key, [digest, key["fpr"], ""])
    assert code == 0, out
    r = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert r != f and run(["git", "rev-parse", "HEAD^"], repo).stdout.strip() == f
    changed = sorted(run(["git", "diff", "--name-only", f, r], repo).stdout.split())
    assert changed == ["docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md", "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md.sig",
                       "packaging/distribution-gate.json", "packaging/legal-allowed-signers"]
    g = gate_state(repo)
    for i, e in g.items():
        if e["decides"] == "owner":
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
