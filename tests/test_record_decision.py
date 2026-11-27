"""The standalone record step of the release signing, end to end with a THROWAWAY key in a copy of the repository: it
writes exactly the fields the record names, the release gate accepts it, and anything altered is refused."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
import record_decision as R  # noqa: E402

ID = "D-LIC-11"
KEYGEN = shutil.which("ssh-keygen")


@pytest.fixture
def copy(tmp_path):
    """The committed tree, with the owner's signer replaced by a throwaway key made here. The real key is never used."""
    tree = subprocess.run(["git", "-C", str(ROOT), "archive", "HEAD", "packaging", "docs", "scripts", "brand", "LICENSE", "LICENSE.md",
                           "LICENSE-COMMONS-CLAUSE", "LICENSING.md"], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tree)) as tar:
        tar.extractall(tmp_path / "repo")
    repo = tmp_path / "repo"
    for rel in ("scripts/release/record_decision.py",):
        shutil.copy(ROOT / rel, repo / rel)                   # the code under test, as it is now
    if (repo / (R.RECORDS[ID]["doc"] + ".sig")).exists():
        pytest.skip("the owner has already signed this record: the committed gate checks it")
    return repo


def sign(repo: Path, key: Path, doc: str) -> None:
    subprocess.run([KEYGEN, "-Y", "sign", "-q", "-f", str(key), "-n", "redevops-legal-decision@v1", str(repo / doc)], check=True, capture_output=True)


def throwaway(tmp_path: Path, repo: Path, *, listed: bool = True) -> Path:
    key = tmp_path / ("test-key" if listed else "other-key")
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "throwaway-test-only", "-f", str(key)], check=True)
    if listed:
        pub = " ".join((tmp_path / (key.name + ".pub")).read_text().split()[:2])
        (repo / R.ALLOWED).write_text(f'redevops-legal-decision namespaces="redevops-legal-decision@v1" {pub}\n')
        fp = subprocess.run([KEYGEN, "-lf", str(key) + ".pub"], capture_output=True, text=True, check=True).stdout.split()[1]
        gate = json.loads((repo / R.GATE).read_text())
        gate["legal_signer_fingerprints"] = [fp]
        (repo / R.GATE).write_text(json.dumps(gate, indent=1, ensure_ascii=False) + "\n")
    return key


pytestmark = pytest.mark.skipif(KEYGEN is None, reason="ssh-keygen is not on this machine")


def test_the_record_says_what_the_step_writes_and_nothing_is_signed_yet(copy):
    assert R.verify(ID, copy) == []
    assert R.entry_of(ID, copy)["decision_reference"] != R.RECORDS[ID]["doc"]


def test_signed_and_applied_it_changes_exactly_five_fields_and_the_gate_accepts_it(copy, tmp_path):
    key = throwaway(tmp_path, copy)
    before = json.loads((copy / R.GATE).read_text())
    sign(copy, key, R.RECORDS[ID]["doc"])
    changed = R.apply(ID, "2026-11-28", copy)
    after = json.loads((copy / R.GATE).read_text())
    digest = hashlib.sha256((copy / R.RECORDS[ID]["doc"]).read_bytes()).hexdigest()
    assert set(changed) == {"decision_reference", "decision_sha256", "decided_by", "decided_on", "ships_meanwhile"}
    assert changed["decision_sha256"] == digest and changed["decided_by"] == f"Alex Rybachek (owner), record {digest}"
    differing = [(a["id"], k) for a, b in zip(before["decisions"], after["decisions"]) for k in set(a) | set(b) if a.get(k) != b.get(k)]
    assert {i for i, _ in differing} == {ID} and {k for _, k in differing} <= set(changed) and {k: v for k, v in before.items() if k != "decisions"} == {
        k: v for k, v in after.items() if k != "decisions"}
    assert next(d for d in after["decisions"] if d["id"] == ID)["chosen_option"] == "b"
    assert R.check(ID, copy) == []


@pytest.mark.parametrize("break_it", ["unsigned", "another key", "record altered after signing", "wrong option"])
def test_anything_altered_is_refused(copy, tmp_path, break_it):
    key = throwaway(tmp_path, copy)
    doc = copy / R.RECORDS[ID]["doc"]
    if break_it == "wrong option":
        doc.write_bytes(doc.read_bytes().replace(b"option: b", b"option: a", 1))
        assert R.verify(ID, copy) and pytest.raises(SystemExit, R.apply, ID, "2026-11-28", copy)
        return
    if break_it == "another key":
        sign(copy, throwaway(tmp_path, copy, listed=False), R.RECORDS[ID]["doc"])
    elif break_it != "unsigned":
        sign(copy, key, R.RECORDS[ID]["doc"])
    R.apply(ID, "2026-11-28", copy)
    if break_it == "record altered after signing":
        doc.write_bytes(doc.read_bytes() + b"\nOne more line.\n")
    assert R.check(ID, copy), break_it
