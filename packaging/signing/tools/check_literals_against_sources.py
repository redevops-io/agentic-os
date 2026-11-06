#!/usr/bin/env python3
"""Verify every entry of packaging/signing/LITERALS-PROVENANCE.json against its SOURCE (round 5, R5-05).

usage: check_literals_against_sources.py SOURCES_ROOT [--provenance FILE] [--repo DIR]

Unlike the unit test, which only checks the repository for self-consistency when no source root is given, this tool FAILS (exit 1) when any entry
cannot be verified: a source file that is missing counts as a failure, not as a skip. Exit 0 only when every entry was verified by at least one
check. Exit 2: bad usage or no source root. Standard library plus /usr/bin/ssh-keygen; no network.
A `check` is an object or a list of objects: repo_file_sha256 (file in the repository), ssh_keygen_fingerprint (a public key file in the
repository, fingerprint printed by ssh-keygen -lf), file_sha256 / text_in_files (files under SOURCES_ROOT).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def verify(entries: list, root: Path, repo: Path) -> list[str]:
    problems: list[str] = []
    for e in entries:
        v = e["value"]
        checks = e["check"] if isinstance(e["check"], list) else [e["check"]]
        ok = 0
        for c in checks:
            t = c["type"]
            if t == "repo_file_sha256":
                f = repo / c["file"]
                if not f.is_file():
                    problems.append(f"{v[:16]}: repository file {c['file']} is missing")
                elif sha(f) != v:
                    problems.append(f"{v[:16]}: sha256 of {c['file']} is {sha(f)}")
                else:
                    ok += 1
            elif t == "ssh_keygen_fingerprint":
                f = repo / c["file"]
                if not f.is_file():
                    problems.append(f"{v[:16]}: public key file {c['file']} is missing")
                    continue
                r = subprocess.run(["/usr/bin/ssh-keygen", "-lf", str(f)], capture_output=True, text=True)
                fp = [x for x in r.stdout.split() if x.startswith("SHA256:")]
                if r.returncode != 0 or not fp:
                    problems.append(f"{v[:16]}: ssh-keygen could not read {c['file']}")
                elif fp[0] != v:
                    problems.append(f"{v[:16]}: ssh-keygen -lf {c['file']} prints {fp[0]}")
                else:
                    ok += 1
            elif t == "file_sha256":
                f = root / c["file"]
                if not f.is_file():
                    problems.append(f"{v[:16]}: source file {c['file']} not found under the source root")
                elif sha(f) != v:
                    problems.append(f"{v[:16]}: sha256 of {c['file']} is {sha(f)}")
                else:
                    ok += 1
            elif t == "text_in_files":
                for rel in c["files"]:
                    f = root / rel
                    if not f.is_file():
                        problems.append(f"{v[:16]}: source file {rel} not found under the source root")
                        continue
                    txt = f.read_text(errors="replace")
                    if (v.lower() in txt.lower()) if c.get("case_insensitive") else (v in txt):
                        ok += 1
                    else:
                        problems.append(f"{v[:16]}: not found in {rel}")
            else:
                problems.append(f"{v[:16]}: unknown check type {t!r}")
        if ok == 0 and not any(p.startswith(v[:16]) for p in problems):
            problems.append(f"{v[:16]}: no check verified it")
    return problems


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    repo = here.parent.parent.parent
    prov = here.parent / "LITERALS-PROVENANCE.json"
    args = argv[1:]
    root = None
    while args:
        a = args.pop(0)
        if a == "--provenance":
            prov = Path(args.pop(0))
        elif a == "--repo":
            repo = Path(args.pop(0))
        elif root is None:
            root = Path(a)
        else:
            print("usage: check_literals_against_sources.py SOURCES_ROOT [--provenance FILE] [--repo DIR]", file=sys.stderr)
            return 2
    if root is None or not root.is_dir():
        print("refusing: a SOURCES_ROOT directory is required (the frozen scripts and the evidence set the literals come from)", file=sys.stderr)
        return 2
    entries = json.loads(prov.read_text())["entries"]
    problems = verify(entries, root, repo)
    for e in entries:
        mark = "FAIL" if any(p.startswith(e["value"][:16]) for p in problems) else "ok"
        print(f"{mark:4} {e['kind']:15} {e['value'][:24]}...  {e['meaning'][:70]}")
    for p in problems:
        print("PROBLEM:", p, file=sys.stderr)
    print(f"{len(entries)} entries, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
