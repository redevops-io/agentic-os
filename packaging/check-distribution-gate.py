#!/usr/bin/env python3
"""Distribution gate: refuse a DISTRIBUTION build while a distribution-blocking decision is OPEN.

  python3 -I packaging/check-distribution-gate.py [--platform macos|windows|linux|qa|all] [--distribution] [--gate FILE]
                                                  [--about-slots] [--allowed-signers FILE]

A distribution build is one with --distribution or ANY value of the environment variable REDEVOPS_DISTRIBUTION_BUILD other than
the empty string or exactly `0` (fail closed, no trimming: `1`, `true`, `yes`, `on`, `y`, `false`, `no`, `0 ` (with a space), ` `
all mean distribution). A PRIVATE candidate build (neither set) prints the open decisions and exits 0: private candidates stay
on the owner's machine and are not distribution.

Exit codes: 0 = allowed (private build, or every applicable blocker is DECIDED, complete and signed); 1 = distribution build
refused (a blocker is OPEN, a DECIDED entry is incomplete, or a signature is missing or invalid); 2 = the gate file itself is
malformed (or an About slot text is missing or does not match). (packaging/build-all.sh and the candidate script turn any non-zero
exit of this gate into exit 3.)

WHAT THE GATE PROVES, AND WHAT IT DOES NOT. A DECIDED entry is plain repository data. The hash check (decision_sha256, the sha256 of a
slot text) proves CONSISTENCY between the entry and the files it names: it is a tripwire against drift and mistakes (a file edited
after it was recorded, a path that points nowhere). It does NOT prove who decided or that anybody approved anything: whoever can
edit this repository can write a DECIDED entry, an invented "signed record" and a matching hash (OBSERVED in the RC3 security review:
all 21 decisions marked DECIDED with invented one-line files passed). Authenticity comes only from the detached signatures below,
which a DISTRIBUTION build additionally requires; the gate never judges licences or legal text.

SIGNATURES (distribution builds). For every DECIDED decision that blocks distribution on the platform (and every decision a
legal slot lists) the record named by decision_reference must have a detached OpenSSH signature `<record>.sig`, and every approved slot
text `<file>.sig`: an SSHSIG in the namespace `redevops-legal-decision@v1`, verified with `ssh-keygen -Y verify` against
packaging/legal-allowed-signers (public keys only, written by the owner; it ships EMPTY, so with no signer every DECIDED entry fails
a distribution build, on purpose, until the owner adds a key). A signature proves which key signed the exact bytes and nothing else.

CONSOLIDATED OWNER APPROVAL (header version 2). One detached signature over one approval document may cover several decisions, but ONLY
decisions whose `decides` is exactly "owner": the header lists `decision: <id> <option>` lines plus base-commit, base-tree and manifest-sha256,
all taken from the verified bytes (parse_bundle_record). A decision that names counsel (decides "counsel" or "owner+counsel") is never covered by
a bundle, and a bundle authorizes no About-slot text; those keep their own version-1 record, counsel_reference and slot texts. The signature,
namespace and signer list are exactly those of a version-1 record; nothing here relaxes them.

THE SIGNED RECORD BINDS MEANING (R2-01). A decision record starts with a machine-readable header, up to the first empty line:

    redevops-legal-decision: 1
    decision: <decision id>
    option: <chosen option key>
    slot: <slot id> sha256 <64 hex of the approved text>        (one line per slot the decision authorizes; or "slot: <id> not-applicable")

The gate parses these fields from the VERIFIED bytes and compares them with the gate entry (id, chosen_option, the slot set and the hash of each
slot text), so one signed record or text cannot be re-used for another decision, option or slot, and a text signed alone (without a record
line naming its slot and hash) is not enough.

THE SIGNER LIST IS PINNED, NOT AUTHENTICATED (R2-01). The gate file's `legal_signer_fingerprints` (SHA256 fingerprints, ships EMPTY) must name
exactly the keys of packaging/legal-allowed-signers, no more and no fewer, or no signature verifies. BOTH files are repository data: the
pairing makes a one-sided change (a key added to the signers file alone) fail, it does not stop a repository writer who edits both. Before any
distribution build the owner confirms the fingerprints against an independent channel (their published key page): a procedure step, not a code
guarantee. This script never creates, chooses or handles a key. A private build only prints a WARNING for a DECIDED entry whose signature or
binding does not verify. docs/legal/CODEOWNERS.proposed is a draft, inactive until the owner installs it.

About / legal slots (RC3): the About page of the application carries five legal slots (ABOUT_SLOTS below). A slot becomes
"approved-verified" (the only state that shows a text as an approved text) only when EVERY decision it lists is DECIDED here (with its written
record, whose hash matches) AND the primary decision (the first one listed) carries a `slot_texts` entry naming an approved text file of
the repository and the sha256 of that file, the file matches, AND the signatures and the signed record headers verify (above). A DECIDED slot
that does not verify is an "unverified-draft": its text is shown only with a label inside it ("Unverified draft, not an approved text.")
in EVERY build mode; an OPEN slot is "pending" (the neutral placeholder); an omitted slot is "not-applicable". `slot_texts` is an object on a DECIDED entry:

    "slot_texts": {"LEGAL.NO_WARRANTY": {"file": "docs/legal/approved/no-warranty.en.txt", "sha256": "<64 hex>"}}

(file: a relative path of the form docs/legal/approved/<name>.txt; LEGAL.SECTION13_NOTICE alone may instead be
{"not_applicable": true} when the recorded decision is "no statement"). An OPEN entry must not carry slot_texts, and an OPEN
slot never has its text file read. The record and the text files must be regular files of the repository: a symlink (the file or
any directory below the repository root) is refused. With --about-slots this script also resolves the slots: a DECIDED slot whose
text is missing, unreadable or does not match its hash is an error in EVERY build mode (exit 2); in a DISTRIBUTION build an
unfilled slot refuses the build (exit 1). packaging/notices/generate_about.py applies the same rules (exit 3 there) when it writes
the About record.

The gate file is packaging/distribution-gate.json. Only a person can mark a decision DECIDED, by editing that file after
the owner has signed docs/legal/OWNER_DECISION_FORM.md: the entry must then carry chosen_option, decision_reference,
decision_sha256 (sha256 of the written record, which must be a regular file of this repository), decided_by and decided_on, and
counsel_reference when 'decides' includes counsel. Standard library only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_GATE = HERE / "distribution-gate.json"
DEFAULT_ALLOWED_SIGNERS = HERE / "legal-allowed-signers"
SIG_NAMESPACE = "redevops-legal-decision@v1"
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
# States of a slot in the About record. approved-verified is the ONLY state that shows a text as an approved text; a text that is not
# backed by verified signatures is never shown as ordinary legal text (unverified-draft carries this label inside the text itself).
TAGS = {"pending": "Pending a decision", "approved-verified": "Approved text, signature verified",
        "unverified-draft": "Unverified draft, not an approved text", "not-applicable": ""}
DRAFT_PREFIX = ("Unverified draft, not an approved text. This is a private candidate; nothing here is a legal statement or "
                "clearance.\n\n")
RECORD_VERSION = "1"
FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
# The published fingerprint of the RELEASE key (the same constant as RELEASE_KEY_FPR in packaging/signing/sign_manifest_sshsig.sh). The owner's
# ssh-agent signs ANY namespace, so a key that is both the release signer and the legal-decision signer would make the legal gate exactly as
# trustworthy as access to the release key: the gate REFUSES the release key as a legal signer, in every build mode. Choose a separate key.
RELEASE_KEY_FPR = "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"


def is_distribution(flag: bool, env=None) -> bool:
    env = os.environ if env is None else env
    # fail closed, NO trimming: only the empty string or exactly "0" means "private candidate"; every other value (including
    # whitespace-only, "0 " and " 0") is a distribution build
    return flag or str(env.get("REDEVOPS_DISTRIBUTION_BUILD", "")) not in ("", "0")


SEPARATION_MSG = ("the published RELEASE key fingerprint is listed as a legal-decision signer. The owner's ssh-agent signs any namespace, so a "
                  "shared key makes the legal gate only as trustworthy as access to the release key: choose a separate legal-decision key")


def separation_problems(allowed_signers: Path) -> list[str]:
    """The release key must not be a legal signer: problems for the allowed-signers file (the fingerprint list is checked by load_doc).
    An error in EVERY build mode (a shared key defeats the separation whether or not a build is a distribution). A line this check cannot
    read as a plain key line (it cannot be parsed, holds no usable key, or carries `cert-authority`, which would let every certificate
    issued by the listed key sign) is a problem too, in every mode: the key it carries cannot be compared with the release key, so it must
    not be accepted silently (the same strings anchor_problems reports). A missing file is not a line problem."""
    fps, probs = allowed_signer_fingerprints(allowed_signers)
    out = [p for p in probs if p.startswith("legal-allowed-signers line ")]
    if RELEASE_KEY_FPR in fps:
        out.append(f"{allowed_signers.name} lists the release key {RELEASE_KEY_FPR}: {SEPARATION_MSG}")
    return out


def load_doc(path: Path) -> dict:
    """The gate file as a dict (schema 1, a non-empty decisions list, a valid legal_signer_fingerprints list). A malformed file prints
    `GATE FILE PROBLEM` and exits 2."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"GATE FILE PROBLEM: gate file {path} unreadable: {e}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("decisions"), list) or not doc["decisions"]:
        print(f"GATE FILE PROBLEM: gate file {path}: schema 1 with a non-empty 'decisions' list expected", file=sys.stderr)
        raise SystemExit(2)
    fps = doc.get("legal_signer_fingerprints", [])
    if not isinstance(fps, list) or any(not isinstance(f, str) or not FP_RE.match(f) for f in fps) or len(set(fps)) != len(fps):
        print("GATE FILE PROBLEM: legal_signer_fingerprints must be a list of distinct SHA256:<43 base64> fingerprints", file=sys.stderr)
        raise SystemExit(2)
    if RELEASE_KEY_FPR in fps:
        print(f"GATE FILE PROBLEM: {SEPARATION_MSG}", file=sys.stderr)
        raise SystemExit(2)
    return doc


def load(path: Path) -> list[dict]:
    return load_doc(path)["decisions"]


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


def unsafe_repo_file(root: Path, rel: str) -> str:
    """"" when `rel` is a regular file of the repository reached WITHOUT a symlink: every component below `root` is lstat-ed (no
    symlink, directories for all but the last, a regular file at the end). Otherwise the reason."""
    cur = Path(root)
    parts = Path(rel).parts
    if not parts:
        return "empty path"
    for n, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            return "not a file of the repository"
        if stat.S_ISLNK(st.st_mode):
            return f"{'/'.join(parts[:n + 1])} is a symbolic link"
        last = n == len(parts) - 1
        if last and not stat.S_ISREG(st.st_mode):
            return "not a regular file"
        if not last and not stat.S_ISDIR(st.st_mode):
            return f"{'/'.join(parts[:n + 1])} is not a directory"
    return ""


def read_repo_file(root: Path, rel: str, cap: int = 1 << 20) -> bytes:
    """Bytes of a regular file of the repository, opened WITHOUT following a symlink (O_NOFOLLOW) and checked with fstat on the open
    handle, after every path component was lstat-ed. Raises OSError otherwise (also above `cap` bytes)."""
    why = unsafe_repo_file(root, rel)
    if why:
        raise OSError(why)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(os.path.join(str(root), rel), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(cap + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > cap:
        raise OSError("file too large")
    return data


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
            try:
                raw_rec = read_repo_file(root, ref)
            except OSError as e:
                errs.append(f"{i}: decision_reference {ref!r} is not a regular file of the repository ({e})")
            else:
                if hashlib.sha256(raw_rec).hexdigest() != h:
                    errs.append(f"{i}: decision_sha256 does not match {ref} (the hash proves consistency with the file, not who decided)")
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


# ------------------------------------------------------------------------------------------------ signed record format
_HDR_LINE = re.compile(r"^([a-z-]+): (.+)$")
_SLOT_LINE = re.compile(r"^(LEGAL\.[A-Z0-9_]+) (?:sha256 ([0-9a-f]{64})|(not-applicable))$")


def parse_decision_record(raw: bytes) -> tuple[dict, list[str]]:
    """The machine-readable header of a decision record: the first lines up to the first empty line, exactly

        redevops-legal-decision: 1
        decision: <decision id>
        option: <chosen option key>
        slot: <slot id> sha256 <64 hex>        (zero or more; the slot ids the decision authorizes, with the sha256 of each text)
        slot: <slot id> not-applicable

    Free text may follow after an empty line. Returns ({"version","decision","option","slots": {slot: sha|"not-applicable"}}, problems)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}, ["the record is not UTF-8"]
    head = []
    for ln in text.split("\n"):
        if ln == "":
            break
        head.append(ln)
    out: dict = {"slots": {}}
    probs: list[str] = []
    if not head or head[0] != f"redevops-legal-decision: {RECORD_VERSION}":
        return {}, [f"the record does not start with the header line 'redevops-legal-decision: {RECORD_VERSION}'"]
    for ln in head[1:]:
        m = _HDR_LINE.match(ln)
        if not m:
            probs.append(f"header line is not 'key: value': {ln[:40]!r}")
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "slot":
            sm = _SLOT_LINE.match(v)
            if not sm:
                probs.append(f"malformed slot line: {v[:60]!r}")
            elif sm.group(1) in out["slots"]:
                probs.append(f"slot {sm.group(1)} listed twice")
            else:
                out["slots"][sm.group(1)] = sm.group(2) or "not-applicable"
        elif k in ("decision", "option"):
            if k in out:
                probs.append(f"{k} given twice")
            out[k] = v
        else:
            probs.append(f"unknown header key {k!r}")
    for k in ("decision", "option"):
        if k not in out:
            probs.append(f"header lacks '{k}:'")
    return out, probs


BUNDLE_VERSION = "2"
WAIVER_VERSION = "3"
WAIVER_STATEMENT = "owner-decision-without-counsel"
WAIVER_REF_PREFIX = "owner-waiver:"      # counsel_reference of an entry decided under a version 3 record: this prefix + the sha256 of the signed record
# Decisions that name counsel. Frozen here, NOT read from the gate file: `decides` is repository data (editable together with the entry), so a
# consolidated owner approval refuses these ids whatever the gate file says. A new counsel decision is added here deliberately
# (tests/test_release_approval_bundle.py fails if the gate file and this set disagree).
COUNSEL_NAMED = frozenset({"D-LIC-1", "D-LIC-3", "D-LIC-4", "D-LIC-5", "D-LIC-6", "D-LIC-9", "D-LIC-10", "D-LIC-12", "D-LIC-16", "D-LIC-18",
                           "pins-03", "pins-08"})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_HDR_LINE = re.compile(r"^([a-z0-9-]+): (.+)$")
_BUNDLE_DECISION = re.compile(r"^([A-Za-z][A-Za-z0-9-]*) ([a-z0-9]{1,4})$")


def parse_bundle_record(raw: bytes) -> tuple[dict, list[str]]:
    """The machine-readable header of a CONSOLIDATED OWNER APPROVAL (one signature for several decisions). Two versions exist:

    Version 2, the approval of OWNER-ONLY decisions:

        redevops-legal-decision: 2
        bundle: <name>
        base-commit: <40 hex>
        base-tree: <40 hex>
        manifest-sha256: <64 hex>
        decision: <decision id> <option key>          (one or more)

    Version 3, the owner's decision of COUNSEL-NAMED decisions WITHOUT counsel (an explicit waiver, never a counsel approval):

        redevops-legal-decision: 3
        bundle: <name>
        base-commit / base-tree / manifest-sha256       (as above)
        counsel-consulted: no
        waiver: owner-decision-without-counsel
        decision: <counsel-named decision id> <option key>        (one or more)
        slot: <slot id> sha256 <64 hex of the approved text>       (zero or more; or "slot: <id> not-applicable")

    Free text follows after an empty line. A version 2 bundle can authorize ONLY decisions whose `decides` is exactly "owner" and no About-slot
    text (a `slot:` line is refused); a version 3 bundle can authorize ONLY decisions of the frozen COUNSEL_NAMED set, and each slot line must
    belong to a decision the bundle lists (see bundle_binding_problems and waiver_binding_problems). The base-commit, base-tree and
    manifest-sha256 lines are bound by the signature; the gate does not resolve them against git (the release orchestrator does).
    Returns ({"version","bundle","base-commit","base-tree","manifest-sha256","decisions": {id: option}, "slots": {slot: sha|"not-applicable"}}, problems)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}, ["the record is not UTF-8"]
    head = []
    for ln in text.split("\n"):
        if ln == "":
            break
        head.append(ln)
    version = next((v for v in (BUNDLE_VERSION, WAIVER_VERSION) if head and head[0] == f"redevops-legal-decision: {v}"), None)
    if version is None:
        return {}, [f"the record does not start with the header line 'redevops-legal-decision: {BUNDLE_VERSION}' or '{WAIVER_VERSION}'"]
    out: dict = {"version": version, "decisions": {}, "slots": {}}
    probs: list[str] = []
    for ln in head[1:]:
        m = _BUNDLE_HDR_LINE.match(ln)
        if not m:
            probs.append(f"header line is not 'key: value': {ln[:40]!r}")
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "decision":
            dm = _BUNDLE_DECISION.match(v)
            if not dm:
                probs.append(f"malformed decision line: {v[:60]!r}")
            elif dm.group(1) in out["decisions"]:
                probs.append(f"decision {dm.group(1)} listed twice")
            else:
                out["decisions"][dm.group(1)] = dm.group(2)
        elif k in ("bundle", "base-commit", "base-tree", "manifest-sha256"):
            if k in out:
                probs.append(f"{k} given twice")
            out[k] = v
        elif k == "slot":
            if version != WAIVER_VERSION:
                probs.append("a bundle cannot authorize an About-slot text (slot line refused)")
                continue
            sm = _SLOT_LINE.match(v)
            if not sm:
                probs.append(f"malformed slot line: {v[:60]!r}")
            elif sm.group(1) in out["slots"]:
                probs.append(f"slot {sm.group(1)} listed twice")
            else:
                out["slots"][sm.group(1)] = sm.group(2) or "not-applicable"
        elif k in ("counsel-consulted", "waiver") and version == WAIVER_VERSION:
            if k in out:
                probs.append(f"{k} given twice")
            out[k] = v
        else:
            probs.append(f"unknown header key {k!r}")
    for k, rx in (("base-commit", _HEX40), ("base-tree", _HEX40), ("manifest-sha256", _HEX64)):
        if not rx.match(str(out.get(k, ""))):
            probs.append(f"header lacks a valid '{k}:'")
    if not str(out.get("bundle", "")).strip():
        probs.append("header lacks 'bundle:'")
    if not out["decisions"]:
        probs.append("header lists no decision")
    if version == WAIVER_VERSION:
        if out.get("counsel-consulted") != "no":
            probs.append("a version 3 record must say 'counsel-consulted: no'")
        if out.get("waiver") != WAIVER_STATEMENT:
            probs.append(f"a version 3 record must say 'waiver: {WAIVER_STATEMENT}'")
        outside = sorted(set(out["decisions"]) - COUNSEL_NAMED)
        if outside:
            probs.append(f"a version 3 record covers only counsel-named decisions, not {outside}")
        for slot in out["slots"]:
            spec = ABOUT_SLOTS.get(slot)
            if spec is None:
                probs.append(f"unknown slot {slot}")
            elif spec["decisions"][0] not in out["decisions"]:
                probs.append(f"slot {slot} belongs to {spec['decisions'][0]}, which the record does not list")
            elif out["slots"][slot] == "not-applicable" and not spec["omittable"]:
                probs.append(f"slot {slot} cannot be not-applicable")
    return out, probs


def bundle_binding_problems(d: dict, hdr: dict) -> list[str]:
    """What the SIGNED bundle says versus the gate entry. The entry must be listed in the bundle with the same option; its `decides` must be
    exactly "owner" (counsel and owner+counsel decisions are never covered by a bundle: they keep their own record and counsel_reference);
    and the entry must carry no slot_texts (a bundle authorizes none)."""
    i, errs = d.get("id"), []
    if i not in hdr.get("decisions", {}):
        errs.append(f"{i}: the signed bundle does not list this decision")
    elif hdr["decisions"][i] != d.get("chosen_option"):
        errs.append(f"{i}: the signed bundle names option {hdr['decisions'][i]!r}, the gate entry {d.get('chosen_option')!r}")
    if d.get("decides") != "owner" or i in COUNSEL_NAMED:
        errs.append(f"{i}: decides is {d.get('decides')!r}; a consolidated owner approval covers owner-only decisions, never one that names counsel")
    if d.get("slot_texts"):
        errs.append(f"{i}: a consolidated owner approval authorizes no About-slot text, but the gate entry names slot_texts")
    return errs


def waiver_binding_problems(d: dict, hdr: dict, record_sha256: str) -> list[str]:
    """What a SIGNED version 3 record (owner decision WITHOUT counsel) says versus the gate entry. The entry must be a counsel-named decision (the
    frozen set), listed in the record with the same option; its counsel_reference must be exactly WAIVER_REF_PREFIX + the sha256 of the signed
    record (so the entry cannot claim a counsel reference it does not have, and the reference cannot be copied to another record); and its
    slot_texts must equal the slot lines of the record that belong to it (slot id, text sha256 or not-applicable)."""
    i, errs = d.get("id"), []
    if hdr.get("version") != WAIVER_VERSION:
        return [f"{i}: not a version 3 (owner decision without counsel) record"]
    if i not in COUNSEL_NAMED:
        errs.append(f"{i}: a version 3 record covers only the counsel-named decisions")
    if i not in hdr.get("decisions", {}):
        errs.append(f"{i}: the signed record does not list this decision")
    elif hdr["decisions"][i] != d.get("chosen_option"):
        errs.append(f"{i}: the signed record names option {hdr['decisions'][i]!r}, the gate entry {d.get('chosen_option')!r}")
    if d.get("counsel_reference") != WAIVER_REF_PREFIX + record_sha256:
        errs.append(f"{i}: counsel_reference must be exactly {WAIVER_REF_PREFIX}<sha256 of the signed record> for an owner decision without counsel")
    want = {}
    for slot, v in (d.get("slot_texts") or {}).items():
        want[slot] = "not-applicable" if isinstance(v, dict) and v.get("not_applicable") is True else (v or {}).get("sha256")
    have = {slot: v for slot, v in hdr.get("slots", {}).items() if ABOUT_SLOTS.get(slot, {}).get("decisions", ("",))[0] == i}
    if have != want:
        errs.append(f"{i}: the signed record authorizes different slots than the gate entry names "
                    f"(only in the record: {sorted(set(have) - set(want))}; only in the gate entry: {sorted(set(want) - set(have))}; "
                    f"text hash differs: {sorted(k for k in set(have) & set(want) if have[k] != want[k])})")
    return errs


def binding_problems(d: dict, hdr: dict) -> list[str]:
    """Compare what the SIGNED record says with the gate entry: decision id, chosen option and the slots (with the sha256 of each text)
    it authorizes. One signed record can therefore not stand for another decision, another option or another slot."""
    i, errs = d.get("id"), []
    if hdr.get("decision") != i:
        errs.append(f"{i}: the signed record is for decision {hdr.get('decision')!r}, not {i!r}")
    if hdr.get("option") != d.get("chosen_option"):
        errs.append(f"{i}: the signed record names option {hdr.get('option')!r}, the gate entry {d.get('chosen_option')!r}")
    want = {}
    for slot, v in (d.get("slot_texts") or {}).items():
        want[slot] = "not-applicable" if isinstance(v, dict) and v.get("not_applicable") is True else (v or {}).get("sha256")
    have = hdr.get("slots", {})
    if have != want:
        extra, missing = sorted(set(have) - set(want)), sorted(set(want) - set(have))
        diff = sorted(k for k in set(have) & set(want) if have[k] != want[k])
        errs.append(f"{i}: the signed record authorizes different slots than the gate entry names "
                    f"(only in the record: {extra}; only in the gate entry: {missing}; text hash differs: {diff})")
    return errs


# ------------------------------------------------------------------------------------------------ signer list
_KEYTYPE = re.compile(r"^(ssh-ed25519|ssh-rsa|ssh-dss|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)$")


def parse_allowed_signers(path: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """The keys of an allowed-signers file as (key type, base64 key, SHA256 fingerprint) and the parse problems. The file must be printable ASCII
    (plus TAB and LF): ssh-keygen splits lines and tokens differently from Python (a control character, a quote or a Unicode line separator can make
    one tool see a comment where the other sees a key), so anything else is refused outright."""
    keys: list[tuple[str, str, str]] = []
    probs: list[str] = []
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return [], [f"allowed-signers file {path} is unreadable or not UTF-8"]
    odd = sorted({hex(ord(c)) for c in text if ord(c) not in (9, 10) and not 32 <= ord(c) <= 126})
    if odd:
        return [], [f"legal-allowed-signers holds characters that are not printable ASCII ({', '.join(odd[:4])}); ssh-keygen and this gate would read it differently"]
    for n, ln in enumerate(text.split("\n"), 1):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        try:
            toks = shlex.split(ln)
        except ValueError:
            probs.append(f"legal-allowed-signers line {n} cannot be parsed")
            continue
        idx = next((k for k, t in enumerate(toks) if _KEYTYPE.match(t) and k + 1 < len(toks)), -1)
        if idx < 0 or "cert-authority" in ln:
            probs.append(f"legal-allowed-signers line {n} is not a plain 'principal [options] key-type base64-key' line")
            continue
        try:
            blob = base64.b64decode(toks[idx + 1], validate=True)
        except ValueError:
            probs.append(f"legal-allowed-signers line {n} holds an invalid key")
            continue
        keys.append((toks[idx], toks[idx + 1], "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")))
    return keys, probs


def allowed_signer_fingerprints(path: Path) -> tuple[list[str], list[str]]:
    """The SHA256 fingerprints (ssh-keygen -l style) of the keys listed in an allowed-signers file, and parse problems."""
    keys, probs = parse_allowed_signers(path)
    return [k[2] for k in keys], probs


def rebuilt_allowed_signers(path: Path) -> str:
    """The allowed-signers text handed to ssh-keygen: ONLY the keys this gate parsed and fingerprinted, one canonical line each, restricted to
    the legal-decision namespace. The repository file itself is never given to ssh-keygen."""
    keys, _ = parse_allowed_signers(path)
    return "".join(f'redevops-legal-decision namespaces="{SIG_NAMESPACE}" {kt} {b64}\n' for kt, b64, _ in keys)


def anchor_problems(allowed_signers: Path, signers) -> list[str]:
    """packaging/legal-allowed-signers and the gate file's legal_signer_fingerprints must list exactly the same keys (both are
    repository data: the owner confirms the fingerprints against an independent channel before a distribution build)."""
    fps, probs = allowed_signer_fingerprints(allowed_signers)
    out = list(probs)
    for f in fps:
        if f not in signers:
            out.append(f"the key {f} in legal-allowed-signers is not listed in legal_signer_fingerprints of the gate file")
    for f in signers:
        if f not in fps:
            out.append(f"the fingerprint {f} in legal_signer_fingerprints has no key in legal-allowed-signers")
    return out


def _ssh_keygen() -> str:
    return shutil.which("ssh-keygen", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin") or ""


def verify_sshsig_bytes(root: Path, rel: str, allowed_signers: Path, signers=()) -> tuple[bytes, str]:
    """(bytes, "") when `<rel>.sig` is a valid SSHSIG (namespace redevops-legal-decision@v1) over the bytes of `rel` made by a key listed
    in `allowed_signers` (whose keys must equal the gate file's legal_signer_fingerprints); otherwise (b"", reason). Both files are read
    without following symlinks; the signature is handed to ssh-keygen as a private copy. Nothing is signed and no private key is read."""
    anchor = anchor_problems(allowed_signers, signers)
    if anchor:
        return b"", anchor[0]
    try:
        listed = [ln for ln in Path(allowed_signers).read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return b"", f"allowed-signers file {allowed_signers} is unreadable"
    if not listed:
        return b"", "packaging/legal-allowed-signers lists no signer (the owner has not supplied a public key yet), so no signature can verify"
    try:
        data = read_repo_file(root, rel)
        sigbytes = read_repo_file(root, rel + ".sig", 1 << 16)
    except OSError as e:
        return b"", f"{rel}: {e}"
    kg = _ssh_keygen()
    if not kg:
        return b"", "ssh-keygen not found (needed to verify the detached signature)"
    env = {"PATH": "/usr/bin:/bin"}
    try:
        with tempfile.TemporaryDirectory(prefix="legal-sig-") as td:
            sig = Path(td) / "record.sig"
            sig.write_bytes(sigbytes)
            clean = Path(td) / "allowed-signers"
            clean.write_text(rebuilt_allowed_signers(allowed_signers), encoding="ascii")
            fp = subprocess.run([kg, "-Y", "find-principals", "-s", str(sig), "-f", str(clean)], capture_output=True, timeout=30, env=env)
            principals = [x.strip() for x in fp.stdout.decode("utf-8", "replace").splitlines() if x.strip()] if fp.returncode == 0 else []
            for who in principals:
                vr = subprocess.run([kg, "-Y", "verify", "-f", str(clean), "-I", who, "-n", SIG_NAMESPACE, "-s", str(sig)],
                                    input=data, capture_output=True, timeout=30, env=env)
                if vr.returncode == 0:
                    return data, ""
    except (OSError, subprocess.SubprocessError) as e:
        return b"", f"ssh-keygen failed: {e.__class__.__name__}"
    return b"", (f"{rel}.sig is not a valid {SIG_NAMESPACE} signature of {rel} by a listed signer "
                 "(missing, tampered, wrong namespace or unknown key)")


def verify_sshsig(root: Path, rel: str, allowed_signers: Path, signers=()) -> str:
    return verify_sshsig_bytes(root, rel, allowed_signers, signers)[1]


def _record_problems(d: dict, root: Path, allowed: Path, signers, cache: dict) -> list[str]:
    """Signature and content binding of the decision record of a DECIDED entry (cached per record)."""
    ref = str(d.get("decision_reference") or "")
    key = ("rec", d.get("id"), ref)
    if key not in cache:
        data, why = verify_sshsig_bytes(root, ref, allowed, signers)
        if why:
            cache[key] = [f"{d.get('id')} record: {why}"]
        else:
            first = data.split(b"\n", 1)[0]
            if first in (f"redevops-legal-decision: {BUNDLE_VERSION}".encode(), f"redevops-legal-decision: {WAIVER_VERSION}".encode()):
                hdr, probs = parse_bundle_record(data)
                if probs:
                    cache[key] = [f"{d.get('id')} record: {p}" for p in probs]
                elif hdr["version"] == WAIVER_VERSION:
                    cache[key] = waiver_binding_problems(d, hdr, hashlib.sha256(data).hexdigest())
                else:
                    cache[key] = bundle_binding_problems(d, hdr)
            else:
                hdr, probs = parse_decision_record(data)
                cache[key] = [f"{d.get('id')} record: {p}" for p in probs] if probs else binding_problems(d, hdr)
    return cache[key]


def _waiver_covers_text(d: dict, slot: str, entry: dict, root: Path, allowed: Path, signers, cache: dict) -> bool:
    """True when the primary decision `d` of `slot` is recorded in a signature-verified version 3 record that lists this slot with the sha256
    of the text file's CURRENT bytes: the owner's signature over that header line authorizes the text, no separate .sig is needed."""
    if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not isinstance(entry.get("sha256"), str):
        return False
    if _record_problems(d, root, allowed, signers, cache):
        return False
    data, why = verify_sshsig_bytes(root, str(d.get("decision_reference") or ""), allowed, signers)
    if why:
        return False
    hdr, probs = parse_bundle_record(data)
    if probs or hdr.get("version") != WAIVER_VERSION or hdr["slots"].get(slot) != entry["sha256"]:
        return False
    try:
        return hashlib.sha256(read_repo_file(root, entry["file"], SLOT_TEXT_MAX_BYTES)).hexdigest() == entry["sha256"]
    except OSError:
        return False


def _text_problems(rel: str, label: str, root: Path, allowed: Path, signers, cache: dict, covered_by=None) -> list[str]:
    """Signature problems of an approved slot text. `covered_by` = (primary decision, slot entry) lets a verified version 3 record
    authorize the text through its slot line (see _waiver_covers_text); otherwise the text needs its own detached signature."""
    key = ("txt", rel, label, (covered_by[0].get("id") if covered_by is not None else None))
    if key not in cache:
        if covered_by is not None and _waiver_covers_text(covered_by[0], label, covered_by[1], root, allowed, signers, cache):
            cache[key] = []
        else:
            why = verify_sshsig_bytes(root, rel, allowed, signers)[1]
            cache[key] = [f"{label} text: {why}"] if why else []
    return cache[key]


def signature_problems(decisions: list[dict], root: Path = ROOT, allowed_signers: Path = DEFAULT_ALLOWED_SIGNERS,
                       platform: str = "all", signers=()) -> list[str]:
    """Signature and binding problems of every DECIDED decision that blocks distribution on `platform`, of every DECIDED decision a legal
    slot lists and of every approved slot text. A distribution build refuses on any; a private build prints them as warnings."""
    slot_ids = {i for spec in ABOUT_SLOTS.values() for i in spec["decisions"]}
    anchor = anchor_problems(allowed_signers, signers)
    if anchor and any(d.get("status") == "DECIDED" for d in decisions):
        return [f"signer list: {m}" for m in anchor]            # reported once, not once per record
    cache: dict = {}
    out: list[str] = []
    texts: dict[str, tuple] = {}
    for d in decisions:
        if d.get("status") != "DECIDED":
            continue
        if d in applicable(decisions, platform) or d.get("id") in slot_ids:
            out += _record_problems(d, root, allowed_signers, signers, cache)
        for slot, v in (d.get("slot_texts") or {}).items():
            if isinstance(v, dict) and isinstance(v.get("file"), str):
                texts.setdefault(v["file"], (slot, d, v))
    for rel, (slot, owner, entry) in sorted(texts.items()):
        out += _text_problems(rel, slot, root, allowed_signers, signers, cache, covered_by=(owner, entry))
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------------------------------------ About slots
def resolve_about_slots(decisions: list[dict], root: Path = ROOT, distribution: bool = False, read=None,
                        allowed_signers: Path = DEFAULT_ALLOWED_SIGNERS, signers=()) -> tuple[dict, list[str], list[str]]:
    """Resolve the About slots. Returns (slots, errors, refusals).

    slots: slot id -> {"state": pending|approved-verified|unverified-draft|not-applicable, "tag": fixed label, "decisions": [...],
    "pending": [...], "text": the FINAL display text (every renderer shows it verbatim), "text_sha256": hex (the sha256 of the approved
    text file; approved-verified and unverified-draft only)}.
      pending           some listed decision is OPEN: the neutral placeholder; the text file is never opened.
      approved-verified every listed decision is DECIDED, the text file matches its recorded sha256 AND the signatures of the records and
                        the text verify against the signer list (which equals the gate file's legal_signer_fingerprints) AND each signed
                        record names this decision, option, slot and text hash.
      unverified-draft  all DECIDED and the text file matches, but a signature or binding does not verify: the text is shown ONLY with the
                        label DRAFT_PREFIX inside it (never as ordinary legal text), in every build mode.
    errors apply in EVERY build mode (a decision without its text, a text that does not match its hash, a symlink, a decision missing from
    the gate file, a DECIDED entry that is incomplete). refusals apply to a DISTRIBUTION build only (a slot that is not approved-verified)."""
    read = read or (lambda p: read_repo_file(root, Path(p).relative_to(root).as_posix()))
    by_id = {d.get("id"): d for d in decisions}
    slots: dict = {}
    errors: list[str] = []
    refusals: list[str] = []
    cache: dict = {}
    for slot, spec in ABOUT_SLOTS.items():
        ids = list(spec["decisions"])
        pending, broken = [], False
        for i in ids:
            d = by_id.get(i)
            if d is None:
                errors.append(f"{slot}: decision {i} is not in the gate file")
                broken = True
            elif not d.get("blocks_distribution"):
                errors.append(f"{slot}: decision {i} must stay blocks_distribution=true (it feeds a legal slot)")
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

        def placeholder(pend):
            slots[slot] = {"state": "pending", "tag": TAGS["pending"], "decisions": ids, "pending": pend,
                           "text": PLACEHOLDER.format(ids=", ".join(pend))}

        if pending:                                   # OPEN: placeholder; no text file is ever opened
            placeholder(pending)
            if distribution:
                refusals.append(f"{slot}: decision(s) {', '.join(pending)} OPEN")
            continue
        entry = (by_id[ids[0]].get("slot_texts") or {}).get(slot)
        if entry is None:
            errors.append(f"{slot}: {', '.join(ids)} DECIDED but {ids[0]} has no slot_texts entry for it")
            continue
        verify = [f"{slot}: {m}" for i in ids for m in _record_problems(by_id[i], root, allowed_signers, signers, cache)]
        if entry.get("not_applicable") is True and spec["omittable"]:
            if verify:                                # an unverified "no statement" does not hide the slot
                placeholder(ids)
                if distribution:
                    refusals.append(verify[0])
            else:
                slots[slot] = {"state": "not-applicable", "tag": TAGS["not-applicable"], "decisions": ids, "pending": [], "text": ""}
            continue
        rel, want = entry.get("file", ""), entry.get("sha256", "")
        if not isinstance(rel, str) or not SLOT_TEXT_RE.match(rel) or not re.fullmatch(r"[0-9a-f]{64}", str(want)):
            errors.append(f"{slot}: slot_texts entry is malformed")
            continue
        why = unsafe_repo_file(root, rel)
        if why:
            errors.append(f"{slot}: approved text file {rel} is not a regular file of the repository ({why})")
            continue
        try:
            raw = read(root / rel)
        except OSError as e:
            errors.append(f"{slot}: approved text file {rel} unreadable: {e.__class__.__name__}")
            continue
        if hashlib.sha256(raw).hexdigest() != want:
            errors.append(f"{slot}: approved text file {rel} does not match its recorded sha256 (a hash match proves consistency, not approval)")
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
        verify += [f"{slot}: {m}" for m in _text_problems(rel, slot, root, allowed_signers, signers, cache, covered_by=(by_id[ids[0]], entry))]
        if verify:
            slots[slot] = {"state": "unverified-draft", "tag": TAGS["unverified-draft"], "decisions": ids, "pending": [],
                           "text": DRAFT_PREFIX + text, "text_sha256": want}
            if distribution:
                refusals.append(f"{slot}: not an approved text ({verify[0]})")
        else:
            slots[slot] = {"state": "approved-verified", "tag": TAGS["approved-verified"], "decisions": ids, "pending": [],
                           "text": text, "text_sha256": want}
    return slots, errors, refusals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", default="all", choices=PLATFORMS)
    ap.add_argument("--distribution", action="store_true", help="treat this as a distribution build")
    ap.add_argument("--gate", type=Path, default=DEFAULT_GATE,
                    help="gate file (default: the repository copy; a different file is for tests only and is never valid for signing)")
    ap.add_argument("--root", type=Path, default=ROOT,
                    help="directory against which decision_reference paths are resolved (default: the repository; tests only)")
    ap.add_argument("--allowed-signers", type=Path, default=DEFAULT_ALLOWED_SIGNERS,
                    help="public keys that may sign legal records (default: packaging/legal-allowed-signers; a different file is "
                         "for tests only and is never valid for signing)")
    ap.add_argument("--about-slots", action="store_true",
                    help="also resolve the About legal slots (see the module docstring): errors exit 2, an unfilled slot "
                         "refuses a distribution build")
    a = ap.parse_args(argv)
    if (a.gate.resolve() != DEFAULT_GATE.resolve() or a.root.resolve() != ROOT.resolve()
            or a.allowed_signers.resolve() != DEFAULT_ALLOWED_SIGNERS.resolve()):
        print("NOTE: non-default --gate/--root: this result is NOT valid for signing or release decisions", file=sys.stderr)
    doc = load_doc(a.gate)
    decisions = doc["decisions"]
    signers = doc.get("legal_signer_fingerprints", [])
    errs = validate(decisions, a.root) + separation_problems(a.allowed_signers)
    if errs:
        for e in errs:
            print("GATE FILE PROBLEM:", e, file=sys.stderr)
        return 2
    dist = is_distribution(a.distribution)
    refusals: list[str] = []
    if a.about_slots:
        slots, slot_errs, refusals = resolve_about_slots(decisions, a.root, dist, allowed_signers=a.allowed_signers, signers=signers)
        if slot_errs:
            for e in slot_errs:
                print("ABOUT SLOT PROBLEM:", e, file=sys.stderr)
            return 2
        counts = {st: sum(1 for x in slots.values() if x["state"] == st) for st in TAGS}
        print(f"about slots: {counts['pending']} of {len(slots)} are placeholders (pending), {counts['approved-verified']} approved-verified, "
              f"{counts['unverified-draft']} unverified-draft, {counts['not-applicable']} not-applicable")
    blockers = open_blockers(decisions, a.platform, a.root)
    total = len(applicable(decisions, a.platform))
    ids = ", ".join(d["id"] for d in blockers)
    if not dist:
        for w in signature_problems(decisions, a.root, a.allowed_signers, a.platform, signers):
            print(f"WARNING: {w} (a distribution build would be refused)", file=sys.stderr)
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
              "are not an approved, signature-verified text:", file=sys.stderr)
        for r in refusals:
            print(f"  - {r}", file=sys.stderr)
        return 1
    sig = signature_problems(decisions, a.root, a.allowed_signers, a.platform, signers)
    if sig:
        print(f"distribution gate (platform {a.platform}): DISTRIBUTION BUILD REFUSED; {len(sig)} legal record(s) or text(s) have no "
              "valid detached signature (a hash match alone proves consistency, not who decided):", file=sys.stderr)
        for r in sig:
            print(f"  - {r}", file=sys.stderr)
        return 1
    print(f"distribution gate (platform {a.platform}): all {total} distribution-blocking decisions are DECIDED with a written record "
          "and a valid detached signature (this proves who signed, not that the decision is right)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
