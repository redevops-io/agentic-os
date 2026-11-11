"""Release signing packages: derived from the release commit, verified read-only, never pre-authorizing anything.

Everything is SYNTHETIC: a throw-away git repository that holds copies of the real signing scripts and a STUB distribution gate (exit code chosen per
commit), fake artifacts, throwaway ssh keys and, on a Mac, ad-hoc signed fixtures. No Developer ID, no Azure, no notary, no network, no real key.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .about_fixture import ROOT

sys.path.insert(0, str(ROOT / "scripts" / "release"))
import signing_package as sp  # noqa: E402
import verify_signed as vs  # noqa: E402

GATE_STUB = "import sys\nsys.exit(int(open(__file__.replace('check-distribution-gate.py', 'gate-rc.txt')).read().strip()))\n"
DARWIN = sys.platform == "darwin"


def g(repo, *a):
    p = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def commit(repo, msg, rc: int | None = None) -> str:
    if rc is not None:
        (repo / "packaging/gate-rc.txt").write_text(f"{rc}\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    g(r, "init", "-q")
    for rel in sp.PIN_FILES:
        if rel in (sp.APPROVAL, sp.APPROVAL + ".sig", "packaging/check-distribution-gate.py"):
            continue
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, r / rel)
    (r / "scripts/release/signing_package.py").write_text((ROOT / "scripts/release/signing_package.py").read_text())
    (r / sp.APPROVAL).parent.mkdir(parents=True, exist_ok=True)
    (r / sp.APPROVAL).write_text("synthetic approval document\n")
    (r / (sp.APPROVAL + ".sig")).write_text("synthetic signature\n")
    (r / "packaging/check-distribution-gate.py").write_text(GATE_STUB)
    return r


def art(tmp_path, commit_id, dmg=b"unsigned dmg bytes", about_mode="distribution", slots="approved-verified") -> Path:
    a = tmp_path / "artifacts"
    for p, names in (("macos", {"App_0.6.0_aarch64.dmg": dmg}), ("windows", {"App_0.6.0_x64.msi": b"msi", "App_0.6.0_x64-setup.exe": b"nsis"}),
                     ("linux", {"app_0.6.0_amd64.deb": b"deb", "App_0.6.0_amd64.AppImage": b"appimage"})):
        d = a / p
        d.mkdir(parents=True, exist_ok=True)
        for n, b in names.items():
            (d / n).write_bytes(b)
        (d / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(b).hexdigest()}  ./{n}\n" for n, b in names.items()) + f"{hashlib.sha256(b'about').hexdigest()}  ./notices/note.txt\n")
        (d / "notices").mkdir(exist_ok=True)
        (d / "notices" / "note.txt").write_bytes(b"about")
        (d / "about.json").write_text(json.dumps({"build": {"mode": about_mode, "source": {"commit": commit_id, "dirty_files": 0}}, "legal": {"slots": {"licence": {"state": slots}}}}))
        # about.json is not in SHA256SUMS on purpose (it is checked, not signed)
    return a


def evid(repo, c, a, tmp_path) -> Path:
    tree = g(repo, "rev-parse", c + "^{tree}")
    f = tmp_path / "evidence-manifest.json"
    f.write_bytes(sp.evidence_manifest(a, c, tree, hashlib.sha256((repo / sp.APPROVAL).read_bytes()).hexdigest()))
    return f


@pytest.fixture()
def built(repo, tmp_path):
    c = commit(repo, "release commit", rc=0)
    a = art(tmp_path, c)
    e = evid(repo, c, a, tmp_path)
    out = tmp_path / "pkg-macos"
    sp.build("macos", repo, c, out, artifacts=a, evid=e)
    return repo, c, a, e, out


# ---------------------------------------------------------------------------------------------------------------- evidence manifest
def test_evidence_manifest_is_deterministic_and_has_no_timestamp(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    one = evid(repo, c, a, tmp_path)
    two = tmp_path / "again.json"
    two.write_bytes(sp.evidence_manifest(a, c, g(repo, "rev-parse", c + "^{tree}"), hashlib.sha256((repo / sp.APPROVAL).read_bytes()).hexdigest()))
    assert one.read_bytes() == two.read_bytes()
    doc = json.loads(one.read_text())
    assert set(doc["platforms"]) == {"macos", "windows", "linux"} and doc["release_commit"] == c
    assert not any(k in doc for k in ("generated", "timestamp", "date"))


def test_evidence_manifest_lists_only_deliverables_but_binds_the_whole_build_folder(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    doc = json.loads(evid(repo, c, a, tmp_path).read_text())
    assert [f["name"] for f in doc["platforms"]["macos"]] == ["App_0.6.0_aarch64.dmg"]            # the nested notes file is bound, not listed
    assert doc["build_folder_sha256sums_sha256"]["macos"] == hashlib.sha256((a / "macos/SHA256SUMS").read_bytes()).hexdigest()
    (a / "macos/notices/note.txt").write_bytes(b"changed")
    with pytest.raises(sp.Refused, match="does not match SHA256SUMS"):
        evid(repo, c, a, tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    (a / "macos/notices/note.txt").write_bytes(b"about")
    (a / "macos/SHA256SUMS").write_text((a / "macos/SHA256SUMS").read_text() + f"{'0' * 64}  ../escape\n")
    with pytest.raises(sp.Refused, match="refused"):
        evid(repo, c, a, tmp_path / "x")


def test_evidence_manifest_refuses_a_platform_without_a_deliverable(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    (a / "linux/SHA256SUMS").write_text(f"{hashlib.sha256(b'about').hexdigest()}  ./notices/note.txt\n")
    with pytest.raises(sp.Refused, match="lists no deliverable"):
        evid(repo, c, a, tmp_path)


def test_evidence_manifest_refuses_a_file_that_differs_from_sha256sums(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    (a / "linux/app_0.6.0_amd64.deb").write_bytes(b"changed")
    with pytest.raises(sp.Refused, match="does not match SHA256SUMS"):
        evid(repo, c, a, tmp_path)


# ---------------------------------------------------------------------------------------------------------------- macOS package
def test_macos_package_verifies_and_its_scope_is_a_distribution_scope(built):
    repo, c, a, e, out = built
    rows = sp.verify_package(out, repo, artifacts=a)
    assert [t for s, t in rows if s == "FAIL"] == [], rows
    scope = sp.parse_scope((out / "scope/scope-macos.DRAFT.txt").read_text())
    assert len(scope) == 23 and scope["scope_version"] == "3"
    assert scope["distribution"] == "yes" and scope["notarize"] == "yes" and scope["upload_to"] == "apple-notary"
    assert scope["build_commit"] == scope["procedure_commit"] == c
    assert scope["not_before"] == sp.WINDOW_PH and scope["not_after"] == sp.WINDOW_PH         # the window is the owner's
    assert scope["identity_sha256"] == hashlib.sha256(b"Developer ID Application: ReDevOps.io LLC (Q8A92DM4T7)").hexdigest()
    assert scope["notary_profile_sha256"] == hashlib.sha256(b"redevops").hexdigest()
    assert scope["team_id"] == "Q8A92DM4T7"
    text = (out / "OPERATOR_RUNBOOK.md").read_text() + (out / "scope/scope-macos.DRAFT.txt").read_text()
    for forbidden in ("BEGIN OPENSSH", "BEGIN PRIVATE", "AZURE_", "access_token"):
        assert forbidden not in text


def test_the_package_is_deterministic_and_never_overwrites(built, tmp_path):
    repo, c, a, e, out = built
    out2 = tmp_path / "pkg-macos-2"
    sp.build("macos", repo, c, out2, artifacts=a, evid=e)
    assert (out / "CHECKSUMS.sha256").read_text() == (out2 / "CHECKSUMS.sha256").read_text()
    with pytest.raises(sp.Refused, match="exists"):
        sp.build("macos", repo, c, out, artifacts=a, evid=e)


def test_verify_reports_the_gate_refusing_as_a_failure_not_as_a_pass(repo, tmp_path):
    c = commit(repo, "release commit with a refusing gate", rc=1)
    a = art(tmp_path, c)
    out = tmp_path / "pkg"
    sp.build("macos", repo, c, out, artifacts=a, evid=evid(repo, c, a, tmp_path))
    rows = sp.verify_package(out, repo, artifacts=a)
    assert any(s == "FAIL" and "REFUSES macos" in t for s, t in rows)
    assert not any(s == "FAIL" for s, t in sp.verify_package(out, repo, artifacts=a, gate=False))


def test_verify_catches_a_hand_edited_scope_and_a_changed_package_file(built):
    repo, c, a, e, out = built
    f = out / "scope/scope-macos.DRAFT.txt"
    f.write_text(f.read_text().replace("distribution=yes", "distribution=no"))
    rows = sp.verify_package(out, repo, artifacts=a)
    assert any(s == "FAIL" and "CHECKSUMS" in t for s, t in rows) and any(s == "FAIL" and "differs from the derivation" in t for s, t in rows)


def test_verify_catches_a_wrong_dmg_and_a_wrong_release_commit(built, tmp_path):
    repo, c, a, e, out = built
    (a / "macos/App_0.6.0_aarch64.dmg").write_bytes(b"another dmg")
    rows = sp.verify_package(out, repo, artifacts=a)
    assert any(s == "FAIL" and "dmg in the artifacts folder" in t for s, t in rows)
    (repo / "x.txt").write_text("x")
    c2 = commit(repo, "a later commit", rc=0)
    info = json.loads((out / "PACKAGE.json").read_text())
    assert info["release_commit"] == c != c2
    a2 = art(tmp_path / "second", c2)
    with pytest.raises(sp.Refused, match="different release commit"):
        sp.build("macos", repo, c2, tmp_path / "pkg2", artifacts=a2, evid=e)


def test_macos_build_refuses_a_dmg_the_evidence_manifest_does_not_list(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    e = evid(repo, c, a, tmp_path)
    (a / "macos/App_0.6.0_aarch64.dmg").write_bytes(b"swapped after the manifest")
    with pytest.raises(sp.Refused, match="not listed with its sha256"):
        sp.build("macos", repo, c, tmp_path / "pkg", artifacts=a, evid=e)


def test_verify_checks_the_extracted_folder_the_scripts_will_run_from(built, tmp_path):
    repo, c, a, e, out = built
    src = tmp_path / "src"
    sp.extract(repo, c, src)
    assert not any(s == "FAIL" for s, t in sp.verify_package(out, repo, src_dir=src, artifacts=a))
    (src / "packaging/signing/signing-common.sh").write_text("# tampered\n")
    rows = sp.verify_package(out, repo, src_dir=src, artifacts=a)
    assert any(s == "FAIL" and "extracted folder differs" in t for s, t in rows)


@pytest.mark.skipif(not DARWIN, reason="tools/fill_scope_window.sh uses the BSD date of macOS")
def test_a_filled_macos_scope_is_accepted_by_the_real_parser(built, tmp_path):
    repo, c, a, e, out = built
    src = tmp_path / "src"
    sp.extract(repo, c, src)
    filled = tmp_path / "scope-macos.txt"
    q = subprocess.run(["/bin/bash", str(src / "packaging/signing/release/fill_scope_window.sh"), str(out / "scope/scope-macos.DRAFT.txt"), str(filled)], capture_output=True, text=True)
    assert q.returncode == 0, q.stderr
    scope_sha = q.stdout.strip()
    assert scope_sha == hashlib.sha256(filled.read_bytes()).hexdigest()
    p = subprocess.run(["/bin/bash", str(src / "packaging/signing/release/check_scope_parse.sh"), "--src", str(src), "--kind", "macos", str(filled)], capture_output=True, text=True)
    assert p.returncode == 0 and "PARSE OK: 23 keys" in p.stdout, p.stdout + p.stderr
    assert "distribution=yes" in p.stdout


def test_the_runbook_names_exact_commands_and_stops_on_a_refusing_gate(built):
    repo, c, a, e, out = built
    rb = (out / "OPERATOR_RUNBOOK.md").read_text()
    for needle in ("git -C \"$REPO\" archive", "signing_package.py verify", "sign_and_notarize_r0prime_macos_v3.sh", "--real", "--distribution", "--ledger",
                   "verify_signed.py macos", "ALLOW (exit 0)", "does not publish, push or tag"):
        assert needle in rb, needle
    assert "R0P_NOTARY_PROFILE='redevops'" in rb


# ---------------------------------------------------------------------------------------------------------------- Windows packages
def _sub(p, n):
    (p / n).mkdir(exist_ok=True)
    return p / n


def win_inputs(tmp_path):
    d = tmp_path / "inner"
    d.mkdir()
    out = []
    for role, b in (("launcher", b"L"), ("sidecar", b"S"), ("uninstaller", b"U")):
        h = hashlib.sha256(b).hexdigest()
        (d / f"{role}-{h}.exe").write_bytes(b)
        out.append(d / f"{role}-{h}.exe")
    return out


def test_windows_inner_package_names_roles_and_leaves_host_pins_to_the_signing_host(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    e = evid(repo, c, a, tmp_path)
    out = tmp_path / "pkg-win-inner"
    sp.build("windows", repo, c, out, stage="inner", inputs=win_inputs(tmp_path), evid=e)
    sc = sp.parse_scope((out / "scope/scope-windows-inner.DRAFT.txt").read_text())
    assert len(sc) == 30 and sc["stage"] == "inner" and sc["distribution"] == "yes" and sc["container_listing_sha256"] == "none"
    assert sc["az_endpoint"] == "https://eus.codesigning.azure.net/" and sc["az_profile"] == "redevops-public" and sc["expected_cn"] == "CN=ReDevOps.io LLC"
    assert all(sc[k] == sp.HOST_PH for k in sp.HOST_KEYS)
    assert sc["inputs_sha256"].split(",") == sorted(sc["inputs_sha256"].split(","))
    rows = sp.verify_package(out, repo, artifacts=a)
    assert [t for s, t in rows if s == "FAIL"] == [], rows


def test_windows_inner_refuses_a_file_that_does_not_carry_its_own_hash(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    e = evid(repo, c, art(tmp_path, c), tmp_path)
    bad = tmp_path / ("sidecar-" + "0" * 64 + ".exe")
    bad.write_bytes(b"x")
    with pytest.raises(sp.Refused, match="does not carry its own sha256"):
        sp.build("windows", repo, c, tmp_path / "p", stage="inner", inputs=[bad], evid=e)
    odd = tmp_path / "setup.exe"
    odd.write_bytes(b"x")
    with pytest.raises(sp.Refused, match="not named"):
        sp.build("windows", repo, c, tmp_path / "p2", stage="inner", inputs=[odd], evid=e)


def test_windows_container_needs_a_listing_and_inner_refuses_one(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    a = art(tmp_path, c)
    e = evid(repo, c, a, tmp_path)
    inst = [a / "windows/App_0.6.0_x64.msi", a / "windows/App_0.6.0_x64-setup.exe"]
    with pytest.raises(sp.Refused, match="needs the container listing"):
        sp.build("windows", repo, c, tmp_path / "p", stage="container", inputs=inst, evid=e)
    lst = tmp_path / "listing.txt"
    lst.write_text("MSI: 12 files\n")
    out = sp.build("windows", repo, c, tmp_path / "p2", stage="container", inputs=inst, evid=e, listing=lst)
    sc = sp.parse_scope((out / "scope/scope-windows-container.DRAFT.txt").read_text())
    assert sc["stage"] == "container" and sc["container_listing_sha256"] == hashlib.sha256(lst.read_bytes()).hexdigest()
    with pytest.raises(sp.Refused, match="belongs to stage container"):
        sp.build("windows", repo, c, tmp_path / "p3", stage="inner", inputs=win_inputs(_sub(tmp_path, "w")), evid=e, listing=lst)


def test_windows_host_pins_fill_only_the_host_placeholders(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    e = evid(repo, c, art(tmp_path, c), tmp_path)
    out = sp.build("windows", repo, c, tmp_path / "p", stage="inner", inputs=win_inputs(tmp_path), evid=e)
    vals = {k: "ab" * 32 for k in sp.HOST_KEYS}
    vals["tool_osslsigncode_sha256"] = "none"
    filled = tmp_path / "filled.txt"
    sp.fill_windows_tools(out / "scope/scope-windows-inner.DRAFT.txt", filled, vals)
    sc = sp.parse_scope(filled.read_text())
    assert sc["tool_az_sha256"] == "ab" * 32 and sc["tool_osslsigncode_sha256"] == "none" and sc["not_before"] == sp.WINDOW_PH
    with pytest.raises(sp.Refused, match="exists"):
        sp.fill_windows_tools(out / "scope/scope-windows-inner.DRAFT.txt", filled, vals)
    with pytest.raises(sp.Refused, match="not a host placeholder"):
        sp.fill_windows_tools(filled, tmp_path / "again.txt", vals)


@pytest.mark.skipif(not DARWIN, reason="fill_scope_window.sh uses the BSD date of macOS")
def test_a_filled_windows_scope_is_accepted_by_the_real_parser(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    e = evid(repo, c, art(tmp_path, c), tmp_path)
    out = sp.build("windows", repo, c, tmp_path / "p", stage="inner", inputs=win_inputs(tmp_path), evid=e)
    src = tmp_path / "src"
    sp.extract(repo, c, src)
    vals = {k: "cd" * 32 for k in sp.HOST_KEYS}
    host = tmp_path / "host.txt"
    sp.fill_windows_tools(out / "scope/scope-windows-inner.DRAFT.txt", host, vals)
    win = tmp_path / "win.txt"
    q = subprocess.run(["/bin/bash", str(src / "packaging/signing/release/fill_scope_window.sh"), str(host), str(win)], capture_output=True, text=True)
    assert q.returncode == 0, q.stderr
    p = subprocess.run(["/bin/bash", str(src / "packaging/signing/release/check_scope_parse.sh"), "--src", str(src), "--kind", "windows", str(win)], capture_output=True, text=True)
    assert p.returncode == 0 and "PARSE OK (windows): 30 keys" in p.stdout, p.stdout + p.stderr


def test_windows_tool_pins_use_the_files_and_the_python_the_owner_names(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "az").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "az").chmod(0o755)
    (bindir / "java").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "java").chmod(0o755)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(sys.executable, venv / "bin" / "python")
    (venv / "lib" / "python3" / "site-packages").mkdir(parents=True)
    (venv / "lib" / "python3" / "site-packages" / "x.pth").write_text("# nothing\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    v = sp.windows_pin_values(tools, venv / "bin" / "python", env_path=str(bindir) + ":/usr/bin:/bin")
    assert v["tool_az_sha256"] == hashlib.sha256((bindir / "az").read_bytes()).hexdigest()
    assert v["tool_java_sha256"] == hashlib.sha256((bindir / "java").read_bytes()).hexdigest()
    assert v["tool_osslsigncode_sha256"] == "none"
    pth = f"lib/python3/site-packages/x.pth {hashlib.sha256(b'# nothing' + chr(10).encode()).hexdigest()}"
    assert v["tool_python_pth_sha256"] == hashlib.sha256((pth + "\n").encode()).hexdigest()
    assert all(len(v[k]) == 64 for k in sp.HOST_KEYS if k != "tool_osslsigncode_sha256")
    with pytest.raises(sp.Refused, match="az"):
        sp.windows_pin_values(tools, venv / "bin" / "python", env_path="/nonexistent")


# ---------------------------------------------------------------------------------------------------------------- SSHSIG package and verification
def throwaway(tmp_path):
    k = tmp_path / "k"
    subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "signing-test-throwaway", "-f", str(k)], check=True, env={"PATH": "/usr/bin:/bin"})
    return k


def rel_manifest(c, tmp_path, linux=None) -> Path:
    m = tmp_path / "RELEASE_MANIFEST.json"
    linux = linux if linux is not None else [{"name": "app_0.6.0_amd64.deb", "size": 3, "sha256": hashlib.sha256(b"deb").hexdigest()}]
    m.write_text(json.dumps({"release_commit": c, "platforms": {"linux": linux}}))
    return m


def test_sshsig_package_binds_the_published_release_key_and_the_manifest(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    m = rel_manifest(c, tmp_path)
    out = sp.build("sshsig", repo, c, tmp_path / "p", release_manifest=m)
    sc = sp.parse_scope((out / "scope/scope-sshsig.DRAFT.txt").read_text())
    assert len(sc) == 11 and sc["namespace"] == "redevops-qa-release@v1" and sc["key_fingerprint"] == sp.RELEASE_KEY_FPR
    assert sc["manifest_sha256"] == hashlib.sha256(m.read_bytes()).hexdigest()
    assert not any(s == "FAIL" for s, t in sp.verify_package(out, repo))
    m2 = rel_manifest("0" * 40, tmp_path / "other") if (tmp_path / "other").mkdir() is None else None
    with pytest.raises(sp.Refused, match="different release commit"):
        sp.build("sshsig", repo, c, tmp_path / "p2", release_manifest=m2)


def test_sshsig_verification_accepts_the_right_signature_and_refuses_tampering(tmp_path):
    k = throwaway(tmp_path)
    fpr = sp.fingerprint(Path(str(k) + ".pub"))
    c = "a" * 40
    m = rel_manifest(c, tmp_path)
    subprocess.run(["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(k), "-n", sp.SSHSIG_NS, str(m)], check=True, env={"PATH": "/usr/bin:/bin"}, capture_output=True)
    sig = Path(str(m) + ".sig")
    lin = tmp_path / "lin"
    lin.mkdir()
    (lin / "app_0.6.0_amd64.deb").write_bytes(b"deb")
    ok = vs.sshsig(m, sig, Path(str(k) + ".pub"), c, lin, expect_fpr=fpr)
    assert ok.failed == 0, ok.rows
    # the real release key is the default expectation: a throwaway key is refused
    assert vs.sshsig(m, sig, Path(str(k) + ".pub"), c, lin).failed >= 1
    (lin / "app_0.6.0_amd64.deb").write_bytes(b"swapped")
    assert any("differ from the signed manifest" in t for s, t in vs.sshsig(m, sig, Path(str(k) + ".pub"), c, lin, expect_fpr=fpr).rows if s == "FAIL")
    m.write_text(m.read_text() + " ")
    assert any("SSHSIG verification failed" in t for s, t in vs.sshsig(m, sig, Path(str(k) + ".pub"), c, lin, expect_fpr=fpr).rows if s == "FAIL")
    assert vs.sshsig(m, sig, Path(str(k) + ".pub"), "b" * 40, lin, expect_fpr=fpr).failed >= 1


# ---------------------------------------------------------------------------------------------------------------- Windows output verification
def win_out(tmp_path, c, *, mode="real", private=False, stage="inner", cn="CN=ReDevOps.io LLC", extra=""):
    d = tmp_path / "winout"
    d.mkdir()
    (d / "a-signed.exe").write_bytes(b"signed")
    (d / "SHA256SUMS").write_text(f"{hashlib.sha256(b'signed').hexdigest()}  a-signed.exe\n")
    (d / "EVIDENCE.txt").write_text("\n".join([f"mode:            {mode}", f"stage:           {stage} (x)", f"scope binds:     build commit {c}; procedure commit {c}",
                                               "distribution:    PRIVATE-CANDIDATE signing: signing does NOT imply permission to distribute" if private else "distribution:    gate enforced (--distribution) and passed",
                                               f"subject check:   signer certificate must carry exactly one CN, equal to '{cn}' (exact match)", extra]) + "\n")
    return d


def test_windows_output_verification_checks_the_run_records(tmp_path):
    c = "c" * 40
    d = win_out(tmp_path, c)
    r = vs.windows(d, "inner", c, "CN=ReDevOps.io LLC", None, None, None)
    assert r.failed == 0, r.rows
    assert any("only verification" in t for s, t in r.rows if s == "INFO")        # honest about what it did not verify independently
    for kw, why in (({"mode": "throwaway-test"}, "mode real"), ({"private": True}, "distribution gate"), ({"stage": "container"}, "stage inner")):
        d2 = tmp_path / why.replace(" ", "_")
        d2.mkdir()
        r2 = vs.windows(win_out(d2, c, **kw), "inner", c, "CN=ReDevOps.io LLC", None, None, None)
        assert r2.failed >= 1, kw
    d3 = tmp_path / "other"
    d3.mkdir()
    assert vs.windows(win_out(d3, c), "inner", "d" * 40, None, None, None, None).failed >= 1
    d4 = tmp_path / "cn"
    d4.mkdir()
    assert vs.windows(win_out(d4, c), "inner", c, "CN=Someone Else", None, None, None).failed >= 1


def test_windows_output_must_differ_from_the_inputs_and_match_the_package(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    e = evid(repo, c, art(tmp_path, c), tmp_path)
    ins = win_inputs(tmp_path)
    pkg = sp.build("windows", repo, c, tmp_path / "p", stage="inner", inputs=ins, evid=e)
    d = tmp_path / "o"
    d.mkdir()
    for f in ins:
        (d / f.name).write_bytes(f.read_bytes())                                   # NOT signed: identical to the input
    (d / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in ins))
    (d / "EVIDENCE.txt").write_text(f"mode:            real\nstage:           inner (x)\nscope binds:     build commit {c}\ndistribution:    gate enforced (--distribution) and passed\n")
    r = vs.windows(d, "inner", c, None, pkg, None, None)
    assert any("unchanged from the input" in t for s, t in r.rows if s == "FAIL")


# ---------------------------------------------------------------------------------------------------------------- release manifest
def test_release_manifest_lists_the_signed_files_and_the_linux_packages(repo, tmp_path):
    c = commit(repo, "r", rc=0)
    ev = tmp_path / "evidence"
    (ev / "signed/macos").mkdir(parents=True)
    (ev / "signed/macos/App.dmg").write_bytes(b"signed dmg")
    wc = ev / "signed/windows-container"
    wc.mkdir(parents=True)
    (wc / "App.msi").write_bytes(b"signed msi")
    (wc / "SHA256SUMS").write_text(f"{hashlib.sha256(b'signed msi').hexdigest()}  App.msi\n")
    a = art(tmp_path, c)
    shutil.copytree(a, ev / "artifacts")
    doc = json.loads(vs.release_manifest(ev, c, repo))
    assert doc["release_commit"] == c and [f["name"] for f in doc["platforms"]["macos"]] == ["App.dmg"]
    assert {f["name"] for f in doc["platforms"]["linux"]} == {"app_0.6.0_amd64.deb", "App_0.6.0_amd64.AppImage"}
    assert "P-LIN a" in doc["linux_signing_policy"]
    (wc / "App.msi").write_bytes(b"altered")
    with pytest.raises(sp.Refused, match="SHA256SUMS"):
        vs.release_manifest(ev, c, repo)


# ---------------------------------------------------------------------------------------------------------------- macOS output verification (ad-hoc fixtures, a Mac only)
def make_app_dmg(tmp_path, name, sidecar: str = "/usr/bin/false", *, adhoc_flags="runtime"):
    root = tmp_path / f"root-{name}"
    app = root / "Agentic Test.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    (app / "Resources" / "Licenses").mkdir(parents=True)
    shutil.copy("/usr/bin/true", app / "MacOS" / "agentic-test")
    shutil.copy(sidecar, app / "MacOS" / "redevops-sidecar")
    (app / "Info.plist").write_text('<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleExecutable</key><string>agentic-test</string><key>CFBundleIdentifier</key><string>io.example.test</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>\n')
    (app / "Resources" / "Licenses" / "about.json").write_text(json.dumps({"build": {"mode": "distribution", "source": {"commit": "c" * 40, "dirty_files": 0}}, "legal": {"slots": {"x": {"state": "approved-verified"}}}}))
    os.symlink("/Applications", root / "Applications")
    for b in ("redevops-sidecar", "agentic-test"):
        subprocess.run(["/usr/bin/codesign", "--force", "-s", "-", "-o", adhoc_flags, str(app / "MacOS" / b)], check=True, capture_output=True)
    subprocess.run(["/usr/bin/codesign", "--force", "-s", "-", "-o", adhoc_flags, str(root / "Agentic Test.app")], check=True, capture_output=True)
    dmg = tmp_path / f"{name}.dmg"
    q = subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-srcfolder", str(root), "-format", "UDZO", "-ov", str(dmg)], capture_output=True, text=True)
    if q.returncode:
        pytest.skip("hdiutil create is not available here: " + q.stderr[:80])
    return dmg


def adhoc_out(tmp_path, dmg):
    o = tmp_path / "out"
    o.mkdir()
    shutil.copy(dmg, o / "signed.dmg")
    (o / "EVIDENCE.txt").write_text("fixture\n")
    (o / "TRANSCRIPT.txt").write_text("fixture\n")
    (o / "TRANSCRIPT.sha256").write_text(f"{hashlib.sha256(b'fixture' + chr(10).encode()).hexdigest()}  TRANSCRIPT.txt\n")
    (o / "OUTPUT.sha256").write_text(f"{hashlib.sha256(dmg.read_bytes()).hexdigest()}  signed.dmg\n")
    return o


@pytest.mark.skipif(not DARWIN, reason="needs codesign, hdiutil and plutil of macOS")
def test_macos_verifier_on_an_adhoc_fixture_counts_only_the_non_developer_id_rows(tmp_path):
    inp = make_app_dmg(tmp_path, "input")
    signed = make_app_dmg(tmp_path / "s", "signed") if (tmp_path / "s").mkdir() is None else None
    out = adhoc_out(tmp_path, signed)
    r = vs.macos(out, inp, "ABCDE12345", "c" * 40, tmp_path / "work", src=ROOT, offline=True, adhoc=True)
    assert r.failed == 0, [x for x in r.rows if x[0] == "FAIL"]
    assert any(s == "XFAIL" and "Developer ID" in t for s, t in r.rows)                       # the Developer ID rows are not met by an ad-hoc fixture
    assert any(s == "PASS" and "byte-identical" in t and "X-2 corrected" in t for s, t in r.rows)
    # the same fixture is REFUSED as a real run: no Developer ID chain, no team, no notary records
    r2 = vs.macos(out, inp, "ABCDE12345", "c" * 40, tmp_path / "work2", src=ROOT, offline=True, adhoc=False)
    assert r2.failed >= 5
    assert any("TeamIdentifier" in t for s, t in r2.rows if s == "FAIL")


@pytest.mark.skipif(not DARWIN, reason="needs codesign, hdiutil and plutil of macOS")
def test_macos_verifier_refuses_changed_code_and_an_unsigned_copy_of_the_input(tmp_path):
    inp = make_app_dmg(tmp_path, "input")
    (tmp_path / "s").mkdir()
    changed = make_app_dmg(tmp_path / "s", "changed", "/usr/bin/yes")                     # different sidecar code
    r = vs.macos(adhoc_out(tmp_path, changed), inp, "ABCDE12345", "c" * 40, tmp_path / "w1", src=ROOT, offline=True, adhoc=True)
    assert any(s == "FAIL" and "code differs from the unsigned input" in t for s, t in r.rows)
    d2 = tmp_path / "t2"
    d2.mkdir()
    same = adhoc_out(d2, inp)                                                              # the input itself: nothing was signed
    r2 = vs.macos(same, inp, "ABCDE12345", "c" * 40, tmp_path / "w2", src=ROOT, offline=True, adhoc=True)
    assert any(s == "FAIL" and "byte-identical to the unsigned input" in t for s, t in r2.rows)


def test_macos_verifier_refuses_a_bad_team_id_and_a_non_empty_work_folder(tmp_path):
    if not DARWIN:
        assert "needs a Mac" in vs.macos(tmp_path, tmp_path / "x", "ABCDE12345", "c" * 40, tmp_path / "w").rows[0][1]
        return
    assert vs.macos(tmp_path, tmp_path / "x", "short", "c" * 40, tmp_path / "w").failed == 1
    w = tmp_path / "busy"
    w.mkdir()
    (w / "f").write_text("x")
    assert any("new or empty" in t for s, t in vs.macos(tmp_path, tmp_path / "x", "ABCDE12345", "c" * 40, w).rows)


# ---------------------------------------------------------------------------------------------------------------- no secrets, no network, standard library only
def test_the_new_modules_use_only_the_standard_library_and_read_no_credentials():
    for name in ("signing_package.py", "verify_signed.py"):
        text = (ROOT / "scripts/release" / name).read_text()
        imports = {ln.split()[1].split(".")[0] for ln in text.splitlines() if ln.startswith(("import ", "from ")) and "__future__" not in ln}
        assert imports <= {"sys", "argparse", "hashlib", "json", "os", "re", "shutil", "subprocess", "tempfile", "pathlib", "signing_package"}, imports
        for pat in (r"\.ssh", r"\.azure", r"find-identity", r"security ", r"urllib", r"requests", r"socket", r"curl ", r"keychain-profile"):
            assert not __import__("re").search(pat, "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(("#", '"', "'")) and "w(" not in ln and "print(" not in ln)), (name, pat)
