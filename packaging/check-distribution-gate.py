#!/usr/bin/env python3
"""Distribution gate: refuse a DISTRIBUTION build while a distribution-blocking decision is OPEN.

  python3 -I packaging/check-distribution-gate.py [--platform macos|windows|linux|qa|all] [--distribution] [--gate FILE]
                                                  [--about-slots]

A distribution build is one with --distribution or ANY value of the environment variable REDEVOPS_DISTRIBUTION_BUILD other than
exactly `0` or empty (fail closed: `1`, `true`, `yes`, `on`, `y`, `false`, `no` ... all mean distribution). A PRIVATE candidate build (neither set) prints the open decisions and exits 0: private candidates stay
on the owner's machine and are not distribution.

Exit codes: 0 = allowed (private build, or every applicable blocker is DECIDED and complete); 1 = distribution build
refused (a blocker is OPEN or a DECIDED entry is incomplete); 2 = the gate file itself is malformed. (packaging/build-all.sh
and the candidate script turn any non-zero exit of this gate into exit 3.)

About / legal slots (RC3): the About page of the application carries five legal slots (ABOUT_SLOTS below). A slot is FILLED
only when EVERY decision it lists is DECIDED here (with its hash-matched written record) AND the primary decision (the first one
listed) carries a `slot_texts` entry naming an approved text file of the repository and the sha256 of that file, and the file
matches. `slot_texts` is an object on a DECIDED entry:

    "slot_texts": {"LEGAL.NO_WARRANTY": {"file": "docs/legal/approved/no-warranty.en.txt", "sha256": "<64 hex>"}}

(file: a relative path of the form docs/legal/approved/<name>.txt; LEGAL.SECTION13_NOTICE alone may instead be
{"not_applicable": true} when the recorded decision is "no statement"). An OPEN entry must not carry slot_texts, and an OPEN
slot never has its text file read. With --about-slots this script also resolves the slots: a DECIDED slot whose text is missing,
unreadable or does not match its hash is an error in EVERY build mode (exit 2); in a DISTRIBUTION build an unfilled slot refuses
the build (exit 1). packaging/notices/generate_about.py applies the same rules (exit 3 there) when it writes the About record.

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

# The legal slots of the About page: slot id -> decisions that must ALL be DECIDED before the slot may carry text. The first
# decision is the primary one: its entry holds the `slot_texts` for the slot. `omittable`: the recorded decision may be "no
# statement", in which case the slot is not shown.
ABOUT_SLOTS = {
    "LEGAL.COPYRIGHT_LINE": {"decisions": ("D-LIC-1",), "omittable": False},
    "LEGAL.LICENCE_STATEMENT": {"decisions": ("D-LIC-1", "D-LIC-15"), "omittable": False},
    "LEGAL.SOURCE_OFFER": {"decisions": ("D-LIC-12", "pins-03", "pins-04"), "omittable": False},
    "LEGAL.NO_WARRANTY": {"decisions": ("D-LIC-10",), "omittable": False},
    "LEGAL.SECTION13_NOTICE": {"decisions": ("D-LIC-5",), "omittable": True},
}
SLOT_TEXT_DIR = "docs/legal/approved"
SLOT_TEXT_RE = re.compile(r"^docs/legal/approved/[A-Za-z0-9._-]+\.txt$")
SLOT_TEXT_MAX_BYTES = 16384
PLACEHOLDER = ("Legal text pending decision {ids}. This is a private candidate; nothing here is a legal statement "
               "or clearance.")


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
            stray = [k for k in ("chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on",
                                 "slot_texts") if d.get(k)]
            if stray:
                errs.append(f"{i}: OPEN but carries decision fields {stray}")
        if "slot_texts" in d and d.get("slot_texts") is not None:
            errs += _slot_texts_problems(d)
    return errs


def _slot_texts_problems(d: dict) -> list[str]:
    """Structure of a `slot_texts` object (no file is read here)."""
    i, st, errs = d.get("id", "?"), d.get("slot_texts"), []
    if not isinstance(st, dict):
        return [f"{i}: slot_texts must be an object"]
    for slot, v in st.items():
        spec = ABOUT_SLOTS.get(slot)
        if spec is None:
            errs.append(f"{i}: slot_texts names an unknown slot {slot!r}")
            continue
        if spec["decisions"][0] != i:
            errs.append(f"{i}: slot_texts for {slot} belongs on the primary decision {spec['decisions'][0]}")
        if not isinstance(v, dict):
            errs.append(f"{i}: slot_texts[{slot}] must be an object")
        elif set(v) == {"not_applicable"}:
            if v["not_applicable"] is not True or not spec["omittable"]:
                errs.append(f"{i}: slot_texts[{slot}] not_applicable is only valid (as true) for an omittable slot")
        elif set(v) != {"file", "sha256"}:
            errs.append(f"{i}: slot_texts[{slot}] must have exactly the keys file and sha256")
        else:
            if not isinstance(v["file"], str) or not SLOT_TEXT_RE.match(v["file"]):
                errs.append(f"{i}: slot_texts[{slot}].file must match {SLOT_TEXT_DIR}/<name>.txt")
            if not isinstance(v["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", v["sha256"]):
                errs.append(f"{i}: slot_texts[{slot}].sha256 must be 64 lowercase hex characters")
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


def resolve_about_slots(decisions: list[dict], root: Path = ROOT, distribution: bool = False,
                        read=None) -> tuple[dict, list[str], list[str]]:
    """Resolve the About slots. Returns (slots, errors, refusals).

    slots: slot id -> {"state": "placeholder"|"filled"|"not-applicable", "decisions": [...], "pending": [...], "text": str,
    "text_sha256": hex (filled only)}. errors apply in EVERY build mode (a decision without its text, a text that does not match
    its hash, a decision missing from the gate file, a DECIDED entry that is incomplete). refusals apply to a DISTRIBUTION build
    only (a slot that is still a placeholder). The text file of a slot is read ONLY when every decision it lists is DECIDED."""
    read = read or (lambda p: Path(p).read_bytes())
    by_id = {d.get("id"): d for d in decisions}
    slots: dict = {}
    errors: list[str] = []
    refusals: list[str] = []
    for slot, spec in ABOUT_SLOTS.items():
        ids = list(spec["decisions"])
        pending, broken = [], False
        for i in ids:
            d = by_id.get(i)
            if d is None:
                errors.append(f"{slot}: decision {i} is not in the gate file")
                broken = True
            elif d.get("status") != "DECIDED":
                pending.append(i)
            else:
                problems = _decided_problems(d, [o.get("key") for o in d.get("options", [])], root)
                if problems:
                    errors += [f"{slot}: {p}" for p in problems]
                    broken = True
        if broken:
            continue
        if pending:                                   # OPEN: placeholder; no text file is ever opened
            slots[slot] = {"state": "placeholder", "decisions": ids, "pending": pending,
                           "text": PLACEHOLDER.format(ids=", ".join(pending))}
            if distribution:
                refusals.append(f"{slot}: decision(s) {', '.join(pending)} OPEN")
            continue
        entry = (by_id[ids[0]].get("slot_texts") or {}).get(slot)
        if entry is None:
            errors.append(f"{slot}: {', '.join(ids)} DECIDED but {ids[0]} has no slot_texts entry for it")
            continue
        if entry.get("not_applicable") is True and spec["omittable"]:
            slots[slot] = {"state": "not-applicable", "decisions": ids, "pending": [], "text": ""}
            continue
        rel, want = entry.get("file", ""), entry.get("sha256", "")
        if not isinstance(rel, str) or not SLOT_TEXT_RE.match(rel) or not re.fullmatch(r"[0-9a-f]{64}", str(want)):
            errors.append(f"{slot}: slot_texts entry is malformed")
            continue
        try:
            raw = read(root / rel)
        except OSError as e:
            errors.append(f"{slot}: approved text file {rel} unreadable: {e.__class__.__name__}")
            continue
        if hashlib.sha256(raw).hexdigest() != want:
            errors.append(f"{slot}: approved text file {rel} does not match its recorded sha256")
            continue
        if len(raw) > SLOT_TEXT_MAX_BYTES or not raw.strip():
            errors.append(f"{slot}: approved text file {rel} is empty or larger than {SLOT_TEXT_MAX_BYTES} bytes")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{slot}: approved text file {rel} is not UTF-8")
            continue
        if any(ord(c) < 32 and c not in "\n\t" for c in text) or "\r" in text:
            errors.append(f"{slot}: approved text file {rel} holds control characters (LF line ends only)")
            continue
        slots[slot] = {"state": "filled", "decisions": ids, "pending": [], "text": text, "text_sha256": want}
    return slots, errors, refusals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", default="all", choices=PLATFORMS)
    ap.add_argument("--distribution", action="store_true", help="treat this as a distribution build")
    ap.add_argument("--gate", type=Path, default=DEFAULT_GATE,
                    help="gate file (default: the repository copy; a different file is for tests only and is never valid for signing)")
    ap.add_argument("--root", type=Path, default=ROOT,
                    help="directory against which decision_reference paths are resolved (default: the repository; tests only)")
    ap.add_argument("--about-slots", action="store_true",
                    help="also resolve the About legal slots (see the module docstring): errors exit 2, an unfilled slot "
                         "refuses a distribution build")
    a = ap.parse_args(argv)
    if a.gate.resolve() != DEFAULT_GATE.resolve() or a.root.resolve() != ROOT.resolve():
        print("NOTE: non-default --gate/--root: this result is NOT valid for signing or release decisions", file=sys.stderr)
    decisions = load(a.gate)
    errs = validate(decisions, a.root)
    if errs:
        for e in errs:
            print("GATE FILE PROBLEM:", e, file=sys.stderr)
        return 2
    dist = is_distribution(a.distribution)
    refusals: list[str] = []
    if a.about_slots:
        slots, slot_errs, refusals = resolve_about_slots(decisions, a.root, dist)
        if slot_errs:
            for e in slot_errs:
                print("ABOUT SLOT PROBLEM:", e, file=sys.stderr)
            return 2
        print(f"about slots: {sum(1 for s in slots.values() if s['state'] == 'placeholder')} of {len(slots)} are placeholders")
    blockers = open_blockers(decisions, a.platform, a.root)
    total = len(applicable(decisions, a.platform))
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
    if refusals:
        print(f"distribution gate (platform {a.platform}): DISTRIBUTION BUILD REFUSED; {len(refusals)} About legal slot(s) "
              "are not filled with an approved text:", file=sys.stderr)
        for r in refusals:
            print(f"  - {r}", file=sys.stderr)
        return 1
    print(f"distribution gate (platform {a.platform}): all {total} distribution-blocking decisions are DECIDED with a written record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
