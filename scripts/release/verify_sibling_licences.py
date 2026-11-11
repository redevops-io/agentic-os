#!/usr/bin/env python3
"""Verify the licence declarations, licence files and package metadata of the two sibling packages at their NEW tags, against the core's standard texts and each package's previous tag.

  verify_sibling_licences.py --connectors DIR --knowledge-frontier DIR     (git clones that hold the tags v0.1.0, v0.1.1 and v0.2.0, v0.2.1)

Checks (read-only, git plumbing only): the tag is annotated and points at a commit whose parent is the previous tag's commit; the diff to the previous tag is exactly the licence files, the licence metadata,
the README line and the version; LICENSE equals the core's AGPL text byte for byte; LICENSE-COMMONS-CLAUSE equals the core's apart from the `Software:` line, which names the package; LICENSE.md names the package, both
standard texts and the SPDX LicenseRef; pyproject.toml declares `AGPL-3.0-or-later WITH Commons-Clause` and the new version; no source file changed. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
LICENCE_FILES = {"LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "README.md", "pyproject.toml"}
EXPECT = {"redevops-connectors": ("v0.1.0", "v0.1.1", "0.1.1"), "knowledge-frontier": ("v0.2.0", "v0.2.1", "0.2.1")}


def git(repo: Path, *a: str, raw: bool = False):
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, env=ENV)
    if p.returncode:
        raise RuntimeError(f"git {' '.join(a[:2])}: {p.stderr.decode(errors='replace').strip()[:150]}")
    return p.stdout if raw else p.stdout.decode().strip()


def check(name: str, repo: Path) -> list[tuple[str, str]]:
    old, new, ver = EXPECT[name]
    rows: list[tuple[str, str]] = []
    add = lambda ok, t: rows.append(("PASS" if ok else "FAIL", f"{name}: {t}"))
    try:
        add(git(repo, "cat-file", "-t", new) == "tag", f"{new} is an annotated tag object")
        c_new, c_old = git(repo, "rev-parse", new + "^{commit}"), git(repo, "rev-parse", old + "^{commit}")
        add(git(repo, "rev-parse", c_new + "^") == c_old and git(repo, "rev-list", "--count", f"{c_old}..{c_new}") == "1", f"{new} is exactly one commit on top of {old} ({c_old[:8]})")
        changed = set(git(repo, "diff", "--name-only", old, new).splitlines())
        add(changed == LICENCE_FILES or changed <= LICENCE_FILES | set(), f"the diff to {old} touches only {sorted(changed)}")
        add(changed <= LICENCE_FILES, "no source, test or other file changed")
        core = (ROOT / "LICENSE").read_bytes()
        add(git(repo, "show", f"{new}:LICENSE", raw=True) == core, "LICENSE equals the core's AGPL-3.0 text byte for byte")
        clause = git(repo, "show", f"{new}:LICENSE-COMMONS-CLAUSE", raw=True).decode()
        core_clause = (ROOT / "LICENSE-COMMONS-CLAUSE").read_text()
        norm = lambda s: re.sub(r"(?m)^Software:.*$", "Software: X", s)
        add(norm(clause) == norm(core_clause), "LICENSE-COMMONS-CLAUSE equals the core's apart from the Software: line")
        m = re.search(r"(?m)^Software:\s+(\S+)", clause)
        add(bool(m) and m.group(1) == name, f"its Software: line names {name}")
        md = git(repo, "show", f"{new}:LICENSE.md").replace("\n", " ")
        add(name in md and "Commons Clause" in md and "LicenseRef-AGPL-3.0-or-later-with-Commons-Clause" in md and "LICENSE-COMMONS-CLAUSE" in md, "LICENSE.md names the package, the Commons Clause, the LicenseRef and both texts")
        py = git(repo, "show", f"{new}:pyproject.toml")
        add(re.search(r'(?m)^license\s*=\s*(\{\s*text\s*=\s*)?"AGPL-3\.0-or-later WITH Commons-Clause"', py) is not None, "pyproject.toml declares AGPL-3.0-or-later WITH Commons-Clause")
        add(re.search(rf'(?m)^version\s*=\s*"{re.escape(ver)}"', py) is not None, f"pyproject.toml version is {ver}")
        rd = git(repo, "show", f"{new}:README.md")
        add("Commons Clause" in rd and not re.search(r"Licensed AGPL-3\.0-or-later\.", rd), "README states the family licence and no longer says plain AGPL-3.0-or-later")
        add("AGPL-3.0-or-later" in py and "Commons-Clause" in py, "metadata and files agree (no plain AGPL declaration left in pyproject.toml)")
    except RuntimeError as e:
        rows.append(("FAIL", f"{name}: {e}"))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--connectors", type=Path, required=True)
    ap.add_argument("--knowledge-frontier", type=Path, required=True)
    a = ap.parse_args(argv)
    rows = check("redevops-connectors", a.connectors) + check("knowledge-frontier", a.knowledge_frontier)
    for s, t in rows:
        print(f"{s:5} {t}")
    bad = sum(1 for s, _ in rows if s == "FAIL")
    print(f"SUMMARY: PASS={len(rows) - bad} FAIL={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
