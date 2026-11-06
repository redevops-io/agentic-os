"""The licence text's contribution-history clause describes the merging account by role, not by a personal login.

Every copy of the licence (LICENSE.md and the vendored integration copies) keeps the clause's meaning: the remaining
commits are GitHub merge commits produced by the maintainer account that merges on `redevops-io/*` branches, which
introduce no third-party authorship. Nothing else in the licence text is touched by this wording.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN = "ary" + "bach"


def _licence_files():
    raw = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True).stdout
    return [p for p in raw.decode().split("\0") if p.rsplit("/", 1)[-1] == "LICENSE.md"]


def test_there_are_licence_copies_to_check():
    assert "LICENSE.md" in _licence_files()


def test_no_licence_copy_names_a_personal_login():
    for rel in _licence_files():
        assert LOGIN not in (ROOT / rel).read_text(encoding="utf-8").lower(), rel


def test_the_clause_describes_the_merging_account_by_role_and_keeps_its_meaning():
    for rel in _licence_files():
        text = (ROOT / rel).read_text(encoding="utf-8")
        flat = re.sub(r"\s+", " ", text)
        assert "GitHub merge commits produced by the maintainer account that merges on `redevops-io/*` branches, which introduce no third-party authorship." in flat, rel
        assert "On that basis RedevOps holds the copyright needed to relicense." in flat, rel
        assert text.count("## Contribution history") == 1, rel
