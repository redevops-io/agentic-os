#!/usr/bin/env python3
"""Distribution gate: refuse a DISTRIBUTION build while a distribution-blocking decision is OPEN.

  python3 -I packaging/check-distribution-gate.py [--platform macos|windows|linux|qa|all] [--distribution] [--gate FILE]

A distribution build is one with --distribution or ANY value of the environment variable REDEVOPS_DISTRIBUTION_BUILD other than
exactly `0` or empty (fail closed: `1`, `true`, `yes`, `on`, `y`, `false`, `no` ... all mean distribution). A PRIVATE candidate build (neither set) prints the open decisions and exits 0: private candidates stay
on the owner's machine and are not distribution.

Exit codes: 0 = allowed (private build, or every applicable blocker is DECIDED and complete); 1 = distribution build
refused (a blocker is OPEN or a DECIDED entry is incomplete); 2 = the gate file itself is malformed.

The gate file is packaging/distribution-gate.json. Only a person can mark a decision DECIDED, by editing that file after
the owner has signed docs/legal/OWNER_DECISION_FORM.md: the entry must then carry chosen_option, decision_reference,
decision_sha256 (sha256 of the signed record; if decision_reference is a file of this repository the hash is verified),
decided_by and decided_on, and counsel_reference when 'decides' includes counsel. The gate does not judge licences: it
checks that the written decisions exist. Standard library only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_GATE = HERE / "distribution-gate.json"
PLATFORMS = ("macos", "windows", "linux", "qa", "all")
DECIDES = ("owner", "counsel", "owner+counsel")
REQUIRED_KEYS = ("id", "title", "question", "decides", "scope", "blocks_distribution", "status", "options",
                 "recommended_default", "ships_meanwhile")


def is_distribution(flag: bool, env=None) -> bool:
    env = os.environ if env is None else env
    # fail closed: only an empty value or exactly "0" means "private candidate"; every other value is a distribution build
    return flag or str(env.get("REDEVOPS_DISTRIBUTION_BUILD", "")).strip() not in ("", "0")


def load(path: Path) -> list[dict]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"gate file {path} unreadable: {e}")
    if doc.get("schema") != 1 or not isinstance(doc.get("decisions"), list) or not doc["decisions"]:
        raise SystemExit(f"gate file {path}: schema 1 with a non-empty 'decisions' list expected")
    return doc["decisions"]


def validate(decisions: list[dict], root: Path = ROOT) -> list[str]:
    """Structural problems of the gate file, and DECIDED entries that lack the written record."""
    errs: list[str] = []
    seen: set[str] = set()
    for d in decisions:
        i = d.get("id", "?")
        for k in REQUIRED_KEYS:
            if k not in d:
                errs.append(f"{i}: missing key {k!r}")
        if i in seen:
            errs.append(f"{i}: duplicate id")
        seen.add(i)
        if d.get("status") not in ("OPEN", "DECIDED"):
            errs.append(f"{i}: status must be OPEN or DECIDED, not {d.get('status')!r}")
        if d.get("decides") not in DECIDES:
            errs.append(f"{i}: decides must be one of {DECIDES}")
        if not d.get("scope") or any(s not in PLATFORMS for s in d.get("scope", [])):
            errs.append(f"{i}: scope must list platforms from {PLATFORMS}")
        keys = [o.get("key") for o in d.get("options", [])]
        if not keys or len(set(keys)) != len(keys):
            errs.append(f"{i}: options need unique keys")
        dflt = (d.get("recommended_default") or {}).get("option")
        if dflt not in keys:
            errs.append(f"{i}: recommended_default.option {dflt!r} is not one of the options {keys}")
        if d.get("status") == "DECIDED":
            errs += _decided_problems(d, keys, root)
        else:
            stray = [k for k in ("chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on")
                     if d.get(k)]
            if stray:
                errs.append(f"{i}: OPEN but carries decision fields {stray}")
    return errs


def _decided_problems(d: dict, keys: list, root: Path) -> list[str]:
    i, errs = d["id"], []
    if d.get("chosen_option") not in keys:
        errs.append(f"{i}: DECIDED without a chosen_option among {keys}")
    if not str(d.get("decision_reference") or "").strip():
        errs.append(f"{i}: DECIDED without decision_reference (the signed written decision)")
    h = str(d.get("decision_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        errs.append(f"{i}: DECIDED without a 64-hex decision_sha256 of the signed record")
    else:
        ref = str(d.get("decision_reference") or "")
        # the written decision must be a FILE OF THE REPOSITORY (relative path, no URL, no '..'): a URL or an absolute path
        # cannot be checked and is not accepted; the hash must match the file's bytes
        if not ref or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", ref) or Path(ref).is_absolute() or ".." in Path(ref).parts:
            errs.append(f"{i}: decision_reference must be a relative path of a file in the repository, not {ref!r}")
        else:
            p = root / ref
            if not p.is_file():
                errs.append(f"{i}: decision_reference {ref!r} is not a file of the repository")
            elif hashlib.sha256(p.read_bytes()).hexdigest() != h:
                errs.append(f"{i}: decision_sha256 does not match {ref}")
    if not str(d.get("decided_by") or "").strip():
        errs.append(f"{i}: DECIDED without decided_by")
    try:
        datetime.date.fromisoformat(str(d.get("decided_on") or ""))
    except ValueError:
        errs.append(f"{i}: decided_on must be an ISO date (YYYY-MM-DD)")
    if "counsel" in str(d.get("decides")) and not str(d.get("counsel_reference") or "").strip():
        errs.append(f"{i}: counsel decides but counsel_reference is empty")
    return errs


def applicable(decisions: list[dict], platform: str) -> list[dict]:
    return [d for d in decisions if d.get("blocks_distribution") and
            ("all" in d["scope"] or platform == "all" or platform in d["scope"])]


def open_blockers(decisions: list[dict], platform: str, root: Path = ROOT) -> list[dict]:
    """Applicable blockers that are OPEN, or DECIDED but incomplete (an incomplete record counts as open)."""
    out = []
    for d in applicable(decisions, platform):
        if d["status"] != "DECIDED" or _decided_problems(d, [o["key"] for o in d["options"]], root):
            out.append(d)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", default="all", choices=PLATFORMS)
    ap.add_argument("--distribution", action="store_true", help="treat this as a distribution build")
    ap.add_argument("--gate", type=Path, default=DEFAULT_GATE,
                    help="gate file (default: the repository copy; a different file is for tests only and is never valid for signing)")
    ap.add_argument("--root", type=Path, default=ROOT,
                    help="directory against which decision_reference paths are resolved (default: the repository; tests only)")
    a = ap.parse_args(argv)
    if a.gate.resolve() != DEFAULT_GATE.resolve() or a.root.resolve() != ROOT.resolve():
        print("NOTE: non-default --gate/--root: this result is NOT valid for signing or release decisions", file=sys.stderr)
    decisions = load(a.gate)
    errs = validate(decisions, a.root)
    if errs:
        for e in errs:
            print("GATE FILE PROBLEM:", e, file=sys.stderr)
        return 2
    blockers = open_blockers(decisions, a.platform, a.root)
    total = len(applicable(decisions, a.platform))
    dist = is_distribution(a.distribution)
    ids = ", ".join(d["id"] for d in blockers)
    if not dist:
        print(f"distribution gate (platform {a.platform}): PRIVATE CANDIDATE build, not a distribution; "
              f"{len(blockers)} of {total} distribution-blocking decisions are OPEN" + (f": {ids}" if blockers else ""))
        return 0
    if blockers:
        print(f"distribution gate (platform {a.platform}): DISTRIBUTION BUILD REFUSED; "
              f"{len(blockers)} of {total} distribution-blocking decisions are OPEN: {ids}", file=sys.stderr)
        for d in blockers:
            print(f"  - {d['id']} ({d['decides']}): {d['title']}", file=sys.stderr)
        print("  record each decision in docs/legal/OWNER_DECISION_FORM.md and update packaging/distribution-gate.json",
              file=sys.stderr)
        return 1
    print(f"distribution gate (platform {a.platform}): all {total} distribution-blocking decisions are DECIDED with a written record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
