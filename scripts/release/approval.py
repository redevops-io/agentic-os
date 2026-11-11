#!/usr/bin/env python3
"""Render, verify and apply the consolidated owner approval of a release.

  approval.py render  --commit F --out FILE        render RELEASE_OWNER_APPROVAL_0.6.0.md for commit F (DRAFT, nothing signed)
  approval.py verify  --doc FILE [--commit F]      check a rendered document against commit F (header, manifest hash, decision set)
  approval.py apply   --doc FILE --pubkey FILE --decided-by TEXT --date YYYY-MM-DD   (used by authorize_release.sh, after the signature exists)
  approval.py digest  --doc FILE                    print the sha256 of the document

The document is generated from packaging/release/decision-manifest-0.6.0.json and packaging/distribution-gate.json AS THEY ARE AT commit F
(read with `git show`, never from the working tree), so the same commit always renders the same bytes. It never contains a signature, a
secret or a timestamp. The approval it records becomes effective only when a detached SSHSIG over its exact bytes verifies against the
legal-decision key listed in packaging/legal-allowed-signers (packaging/check-distribution-gate.py, record header version 2); the banner
"DRAFT - NOT AUTHORIZED" stays in the signed bytes on purpose: the signature, not the banner, is the authorization. Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True        # loading the gate by path must not leave __pycache__ in the (clean-checked) worktree

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "packaging/release/decision-manifest-0.6.0.json"
GATE = "packaging/distribution-gate.json"
ALLOWED = "packaging/legal-allowed-signers"
DOC_REL = "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md"
BUNDLE_NAME = "RELEASE_OWNER_APPROVAL_0.6.0"
RELEASE_KEY_FPR = "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"
ENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "LC_ALL": "C"}
LICENCE_FILES = ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md", "docs/legal/COMPONENT_LICENCE_INVENTORY.md", "apps/README.md")
BANNER = "DRAFT - NOT AUTHORIZED"


def git(*args: str, root: Path = ROOT, raw: bool = False):
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=ENV, check=True)
    return p.stdout if raw else p.stdout.decode("utf-8").strip()


def show(commit: str, rel: str, root: Path = ROOT) -> bytes:
    return git("show", f"{commit}:{rel}", root=root, raw=True)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load(commit: str, root: Path = ROOT):
    man_raw = show(commit, MANIFEST, root)
    gate = json.loads(show(commit, GATE, root))
    return man_raw, json.loads(man_raw), gate


_GATE_MOD = {}


def gate_mod(root: Path = ROOT):
    """The release gate module (packaging/check-distribution-gate.py of `root`), loaded by path: the bundle parser, the frozen set of
    counsel-named decisions and the signature verification are the gate's own, not copies."""
    key = str(root)
    if key not in _GATE_MOD:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"_gate_{len(_GATE_MOD)}", root / "packaging" / "check-distribution-gate.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _GATE_MOD[key] = mod
    return _GATE_MOD[key]


def owner_only(gate: dict, root: Path = ROOT) -> list[dict]:
    """Owner-only decisions. Refuses a gate file in which a decision that the gate freezes as counsel-named says `decides: owner`."""
    frozen = getattr(gate_mod(root), "COUNSEL_NAMED", None)
    if frozen is None:
        raise SystemExit("the gate in this tree has no frozen COUNSEL_NAMED set; refusing to treat any decision as owner-only")
    bad = sorted(d["id"] for d in gate["decisions"] if d["id"] in frozen and d["decides"] == "owner")
    if bad:
        raise SystemExit(f"the gate file marks counsel-named decisions as owner-only: {bad}")
    named = sorted(d["id"] for d in gate["decisions"] if "counsel" in d["decides"] and d["id"] not in frozen)
    if named:
        raise SystemExit(f"the gate file names counsel on decisions that the gate does not freeze as counsel-named: {named}")
    return [d for d in gate["decisions"] if d["decides"] == "owner"]


def header_lines(commit: str, tree: str, man_sha: str, man: dict, gate: dict, include: set | None = None, root: Path = ROOT) -> list[str]:
    out = ["redevops-legal-decision: 2", f"bundle: {BUNDLE_NAME}", f"base-commit: {commit}", f"base-tree: {tree}", f"manifest-sha256: {man_sha}"]
    for d in owner_only(gate, root):
        if include is None or d["id"] in include:
            out.append(f"decision: {d['id']} {man['decisions'][d['id']]['option']}")
    return out


def opt_text(d: dict, key: str) -> str:
    for o in d["options"]:
        if o["key"] == key:
            return o["text"]
    return "(unknown option)"


def esc(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render(commit: str, root: Path = ROOT, include: set | None = None) -> str:
    commit = git("rev-parse", commit + "^{commit}", root=root)
    tree = git("rev-parse", commit + "^{tree}", root=root)
    man_raw, man, gate = load(commit, root)
    man_sha = sha(man_raw)
    decs = {d["id"]: d for d in gate["decisions"]}
    missing = sorted(set(decs) - set(man["decisions"]))
    if missing:
        raise SystemExit(f"manifest has no choice for: {missing}")
    for i, m in man["decisions"].items():
        if i not in decs or m["option"] not in [o["key"] for o in decs[i]["options"]]:
            raise SystemExit(f"manifest names an unknown decision or option: {i} {m['option']}")
    all_ob = owner_only(gate, root)
    if include is not None and (not include or not include <= {d["id"] for d in all_ob}):
        raise SystemExit("include must be a non-empty subset of the owner-only decisions")
    ob = [d for d in all_ob if include is None or d["id"] in include]
    struck = [d for d in all_ob if d not in ob]
    other = [d for d in gate["decisions"] if d["decides"] != "owner"]
    L: list[str] = []
    w = L.append
    L += header_lines(commit, tree, man_sha, man, gate, include, root)
    w("")
    w("# RELEASE OWNER APPROVAL 0.6.0")
    w("")
    w(f"> **{BANNER}.** This document is a draft prepared by an agent. It authorizes nothing by itself, including after it has been read, initialled or")
    w("> copied. It becomes an authorization only if the owner signs these exact bytes with the legal-decision key and the release gate verifies that")
    w("> signature (section 10). The banner stays in the signed bytes on purpose: the signature, not the banner, is the authorization. No counsel approval, legal")
    w("> opinion, timestamp or cryptographic signature is contained or implied here.")
    w("")
    w("## 0. Summary: what you are authorizing")
    w("")
    w(f"By signing this document once you would, **for release 0.6.0 built from the release commit derived from `{commit}`** (tree `{tree}`):")
    w("")
    w(f"1. **Record {len(ob)} owner-only decisions** ({', '.join(d['id'] for d in ob)}) with the options in section 4. They are mapped onto their existing gate identifiers; each stays a separate entry in `packaging/distribution-gate.json`." + (f" Struck by the owner, NOT recorded and still OPEN: {', '.join(d['id'] for d in struck)}." if struck else ""))
    w("2. **State the facts in section 4 as the owner's own** (marked *attests*): first-party authorship of the hero placeholder and of the launcher icons, and the licence intent for the sibling packages.")
    w("3. **Accept the exceptions and risks in section 7** for this release.")
    w("4. **Authorize the release workflow in section 6 to run at your terminal**: build macOS, Windows and Linux from the release commit; sign macOS with the Developer ID identity and notarize with the `redevops` profile; sign Windows through Azure Artifact Signing with your own `az` login; apply the Linux policy you choose in P-LIN; and, only after every stage has passed and with your typed confirmation, **push the release commit as the branch `release/0.6.0` and the tag `v0.6.0` to the remote you name** (pins-04). Nothing is uploaded, released or deployed by it.")
    w("")
    w("**You are NOT authorizing**, and this signature cannot supply:")
    w("")
    w(f"* any decision that names counsel ({len(other)} decisions, section 5: {', '.join(d['id'] for d in other)}). They have no counsel reference and stay OPEN; the release gate keeps refusing a distribution build until each has its own record, a counsel reference, and (for the five About slots) a signed text.")
    w("* a legal conclusion about the Commons Clause and the AGPL (sections 7 and 10), the apps-versus-core question, contributor terms, or the copyright statement (section 8).")
    w("* a distribution: the macOS dmg that is already signed and notarized stays a private candidate; the new build can be distributed only when the gate passes.")
    w("* the use of any key, secret or cloud credential by an agent: the workflow runs at your terminal and reads nothing from `~/.ssh`, the Keychain or `~/.azure` except through the tools you run.")
    w("")
    w(f"**Result if you sign now:** the gate would still refuse a distribution build on every platform because of the counsel decisions in section 5 and the technical blockers in section 9. The signature removes the owner-only paperwork; it does not remove those.")
    w("")
    w("## 1. Owner and company (from verified records)")
    w("")
    o, c = man["owner"], man["company"]
    w("| Item | Value | Source |")
    w("|---|---|---|")
    w(f"| Name on the 2026-11-10 signing form | {esc(o['signature_name_on_prior_forms'])} (initials {esc(o['initials_on_prior_forms'])}) | owner-signed form |")
    w(f"| Git author name in the repository | {esc(o['git_author_name'])} | `git log` |")
    w(f"| Contact | {esc(o['contact'])} | session record |")
    w(f"| Company in the signing records | {esc(c['legal_name_in_signing_records'])}, {esc(c['location_in_signing_records'])} | Azure identity validation and Developer ID (observed 2026-11-04) |")
    w(f"| Apple signing identity | {esc(c['apple_developer_id'])} | keychain identity, observed 2026-11-04 |")
    w(f"| Azure identity validation | {esc(c['azure_identity_validation'])} | Azure Artifact Signing, observed 2026-11-04 |")
    w(f"| Copyright holder in the licence files | {esc(c['copyright_holder_in_licence_files'])} | `LICENSE.md`, `LICENSING.md` |")
    w("")
    w(f"* **To confirm by the owner:** {o['note_to_confirm']}")
    w(f"* **To confirm by the owner:** {c['fact_to_confirm']}")
    w("")
    w("## 2. Source, commit and artifacts")
    w("")
    w("| Item | Value |")
    w("|---|---|")
    w(f"| Release-preparation commit F (the commit this document is generated on) | `{commit}` |")
    w(f"| Its source tree | `{tree}` |")
    w(f"| Decision manifest at F | `{MANIFEST}`, sha256 `{man_sha}` |")
    for k, v in man["source"]["protected_refs"].items():
        w(f"| Immutable reference: {k} | `{v}` |")
    w("")
    w(f"**Release commit rule.** {man['source']['release_commit_rule']}")
    w("")
    a = man["artifacts_existing"]
    w("**Existing artifacts and evidence (preserved, never modified):**")
    w("")
    for k, v in a.items():
        w(f"* {k.replace('_', ' ')}: `{v}`" if len(v) < 90 else f"* {k.replace('_', ' ')}: {v}")
    w("")
    w("**Artifacts to be built from the release commit:** macOS " + ", ".join(man["artifacts_to_build"]["macos"]) + "; Windows " + ", ".join(man["artifacts_to_build"]["windows"]) + "; Linux " + ", ".join(man["artifacts_to_build"]["linux"]) + ". Excluded: " + "; ".join(man["artifacts_to_build"]["excluded"]) + ".")
    w("")
    w("## 3. Licensing categories and component schedules")
    w("")
    w("| Category | Licence | Where stated |")
    w("|---|---|---|")
    w("| 1. ReDevOps core platform (Schedule A) | GNU AGPL-3.0-or-later with the Commons Clause: source-available, not OSI open source | `LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`, `LICENSING.md` |")
    w("| 2. The 21 applications under `apps/` (Schedule B1) | GNU AGPL-3.0-only, without the Commons Clause | `apps/<name>/LICENSE`, `apps/README.md` |")
    w("| 3. Third-party software (Schedules B2, B3) | their own licences, unchanged | `THIRD_PARTY_NOTICES.txt`, `NOTICES_MISSING.md` |")
    w("")
    w("Separate repositories whose code the artifacts contain are Schedule C. The path-by-path inventory with flags F1 to F9 is `docs/legal/COMPONENT_LICENCE_INVENTORY.md`. Exact files at F:")
    w("")
    w("| File | sha256 |")
    w("|---|---|")
    for rel in LICENCE_FILES:
        try:
            w(f"| `{rel}` | `{sha(show(commit, rel, root))}` |")
        except subprocess.CalledProcessError:
            w(f"| `{rel}` | (not present at F) |")
    w("")
    w("No per-file SPDX headers are added in this release. The Commons Clause standard text and the AGPL text are unchanged.")
    w("")
    w("## 4. Owner-only decisions (covered by ONE signature)")
    w("")
    w("Each row is recorded in the signed header at the top of this document and maps to the gate entry with the same identifier. *Blocks* says whether the entry blocks a distribution build.")
    w("")
    w("| Gate id | Decision | Chosen option | What the option means | Blocks | Open counsel questions that remain |")
    w("|---|---|---|---|---|---|")
    for d in ob:
        m = man["decisions"][d["id"]]
        w(f"| {d['id']} | {esc(d['title'])} | **{m['option']}**{' (recommended)' if m['option'] == d['recommended_default']['option'] else ''} | {esc(opt_text(d, m['option']))} | {'yes' if d['blocks_distribution'] else 'no'} | {', '.join(d['counsel_questions']) or '-'} |")
    w("")
    for d in ob:
        m = man["decisions"][d["id"]]
        extra = []
        if m.get("note"):
            extra.append(m["note"])
        for s in m.get("attests", []):
            extra.append(f"*Attests:* {s}")
        if m.get("owner_statement"):
            extra.append(f"*Owner statement:* {m['owner_statement']}")
        if extra:
            w(f"* **{d['id']}.** " + " ".join(extra))
    w("")
    w("The gate classes these decisions as owner decisions (the set of counsel-named decisions is frozen inside the gate, not read from the gate file). Several of them list counsel questions (last column); counsel has not answered them and this approval does not claim otherwise. An owner who prefers to wait for counsel on D-LIC-2 (Q7, Q8, Q4) or on any other row can have the document re-rendered without that decision (`approval.py render --strike ID`); the struck decision then stays OPEN and the document has a different digest. A hand-edited document is refused.")
    w("")
    w("## 5. Decisions that name counsel (NOT covered; stay OPEN)")
    w("")
    w("| Gate id | Decides | Decision | Owner-side recommended option | Counsel questions | Counsel reference |")
    w("|---|---|---|---|---|---|")
    for d in other:
        m = man["decisions"][d["id"]]
        w(f"| {d['id']} | {d['decides']} | {esc(d['title'])} | {m['option']}: {esc(opt_text(d, m['option']))[:110]} | {', '.join(d['counsel_questions'])} | **none** |")
    w("")
    w("Each needs its own signed record carrying a counsel reference (and, for the five About slots, a signed approved text). A later consolidated approval for these is possible only after counsel has answered; it is not prepared here and nothing downgrades them to owner-only.")
    w("")
    w("## 6. Platform signing decisions and release actions")
    w("")
    ps = man["platform_signing"]
    mac, win, lin = ps["macos"], ps["windows"], ps["linux"]
    w(f"### P-MAC macOS")
    w("")
    for k in ("scope", "identity", "identity_sha256", "team_id", "notary_profile_name", "notary_profile_sha256", "notarize", "staple", "distribution", "entitlement", "preserved", "runs_at"):
        w(f"* {k.replace('_', ' ')}: {mac[k]}")
    w("")
    w("### P-WIN Windows (Azure Artifact Signing)")
    w("")
    for k in ("scope", "azure_endpoint", "azure_account", "azure_certificate_profile", "azure_expected_cn", "azure_region", "certificate_validity_note", "known_status"):
        w(f"* {k.replace('_', ' ')}: {win[k]}")
    w(f"* needs the owner at run time: {'; '.join(win['needs_owner_at_run_time'])}")
    w(f"* never in this document: {', '.join(win['never_in_this_document'])}")
    w("")
    w("### P-LIN Linux (recommendation; no mandatory signing requirement exists)")
    w("")
    for k, v in lin["options"].items():
        w(f"* **{k}**{' (recommended)' if k == lin['recommended'] else ''}: {v}")
    w(f"* why (a): {lin['why_a']}")
    w(f"* {lin['note']}")
    w("")
    w(f"**Chosen:** P-LIN option **{lin['recommended']}**, P-MAC and P-WIN as above.")
    w("")
    w("### Release actions authorized by a valid signature (and only these)")
    w("")
    w("1. Run `scripts/release/release_orchestrate.sh --run` at the owner's terminal; it stops at the first stage that fails or is blocked, and its first stage (S0) requires the release commit to be F plus exactly the approval files, signed by the key fingerprint you pass in `--expect-signer-fingerprint`. The release gate itself refuses a distribution build whatever the orchestrator does.")
    w("2. Build the artifacts of section 2 and verify their checksums.")
    w("3. Sign them per P-MAC, P-WIN and P-LIN; every real signing step prints its scope hash and needs the owner's own login or keychain.")
    w(f"4. After stages S0 to S7 have passed in the same run, with `--push --remote NAME` and your typed confirmation: tag the release commit `{man['source']['tag_proposal']}` and push the branch `release/0.6.0` and that tag to the remote you name, with your own git credentials (pins-04). The orchestrator then reads the remote back (`git ls-remote`). Recording the pins-04 decision does not publish anything; the publication happens only at this step.")
    w("5. Write the consolidated release manifest and evidence report.")
    w("")
    w("Not authorized: uploading or publishing artifacts, creating a GitHub release, deploying, changing other repositories, using any key an agent can reach.")
    w("")
    w("## 7. Exceptions and accepted risks (complete list)")
    w("")
    w("| Id | Exception or risk | Source |")
    w("|---|---|---|")
    for x in man["exceptions_and_accepted_risks"]:
        w(f"| {x['id']} | {esc(x['text'])} | {esc(x['source'])} |")
    w("")
    w("## 8. Legal acknowledgments and unresolved counsel questions")
    w("")
    w("By signing, the owner acknowledges (these are statements about what this document is, not legal conclusions):")
    w("")
    w("* This document, the licence schedules and the inventory are documentation of the owner's intent. They are not legal advice and no counsel has reviewed them.")
    w("* The core is described as source-available (family licence: AGPL-3.0-or-later with the Commons Clause), never as open source. The applications are AGPL-3.0-only.")
    w("* **Unresolved for counsel:** whether the Commons Clause may stand beside the AGPL (sections 7 and 10) in a combined binary (D-LIC-3, Q4 to Q6); whether AGPL-3.0-only applications may depend on the Commons-Clause core as intended (N1); the scope of \"Software\" in the Commons Clause (N2); contributor terms (N3, outside contributions are not merged); and whether the copyright statement in `LICENSE.md` is adequate given AI-agent-authored commits and three author identities (N4, flags F1 and F2).")
    w("* A documentation change does not resolve any of these, and none of them is resolved by this signature.")
    w("")
    w("## 9. Blockers that remain after this approval")
    w("")
    w("| Id | Blocker |")
    w("|---|---|")
    for x in man["technical_blockers"]:
        w(f"| {x['id']} | {esc(x['text'])} |")
    w(f"| L1 | The decisions of section 5 are OPEN: {', '.join(d['id'] for d in other)}. |")
    w("")
    w("## 10. How this approval becomes effective")
    w("")
    w("1. The owner reads this document and its sha256 (printed by `scripts/release/authorize_release.sh`, shown separately from the file).")
    w("2. The owner says explicitly that they approve this exact document.")
    w("3. The owner runs `scripts/release/authorize_release.sh --doc <file> --pubkey <legal-decision public key>` at their own terminal. It copies the document to a private directory and works only on that copy, re-checks it against the commit, shows the digest and the key fingerprint, asks the owner to type both and to type the signing name (which must equal the name in section 11), signs the copy once with `ssh-keygen -Y sign` (namespace `redevops-legal-decision@v1`, agent-held key), then writes the document, its signature, the signer list and the gate entries of the bundled decisions, asks the release gate itself to verify them, and commits exactly those four paths (rolling everything back if the gate refuses). It signs nothing else and pushes nothing.")
    w("4. `packaging/check-distribution-gate.py` verifies the signature and maps each decision of the signed header to its gate entry; an entry that is not listed, that names counsel (a set frozen inside the gate), or whose option differs is refused.")
    w("5. The owner records the fingerprint of the legal-decision key from an independent channel and passes it to the orchestrator (`--expect-signer-fingerprint`). Without that value the orchestrator cannot tell the owner's signature from a signature by whatever key the release commit lists. To make a process that holds your agent socket unable to sign silently, load the key with `ssh-add -c` (confirmation on every use).")
    w("")
    w("## 11. Signature block (prefilled DRAFT)")
    w("")
    w("The values below are prefilled from the owner's earlier signed form so that the format is the established one. **They are not an act of the owner and not a signature.** The date is never prefilled; `authorize_release.sh` records the date when the owner runs it.")
    w("")
    w(f"* Owner name as signing (from the 2026-11-10 form): `____{o['signature_name_on_prior_forms']}____`  *(prefilled, DRAFT; `authorize_release.sh` makes the owner type exactly this name, so it is part of the signed bytes)*")
    w(f"* Initials: `_{o['initials_on_prior_forms']}_`  *(prefilled, DRAFT)*")
    w("* Date: `____________`  *(left blank; set at the signing run)*")
    w(f"* Status: **{BANNER}**")
    w("")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------------------------- verify
def parse_header(text: str, root: Path = ROOT) -> dict:
    """The gate's own strict bundle parser (no slot lines, no unknown keys, no duplicates, at least one decision)."""
    hdr, probs = gate_mod(root).parse_bundle_record(text.encode("utf-8"))
    if probs:
        raise SystemExit("not a valid consolidated approval header: " + "; ".join(probs))
    return hdr


def verify(doc: Path, commit: str | None = None, root: Path = ROOT) -> list[str]:
    """The document must be byte-identical to what `render` produces for its commit and its (possibly struck) decision set."""
    errs: list[str] = []
    text = doc.read_bytes().decode("utf-8")
    try:
        h = parse_header(text, root)
    except SystemExit as e:
        return [str(e)]
    commit = commit or h.get("base-commit", "")
    try:
        full = git("rev-parse", commit + "^{commit}", root=root)
    except subprocess.CalledProcessError:
        return [f"commit {commit!r} is not in the repository"]
    if h.get("base-commit") != full:
        errs.append(f"header base-commit {h.get('base-commit')} is not {full}")
    if h.get("base-tree") != git("rev-parse", full + "^{tree}", root=root):
        errs.append("header base-tree does not match the tree of the commit")
    man_raw, man, gate = load(full, root)
    if h.get("manifest-sha256") != sha(man_raw):
        errs.append("header manifest-sha256 does not match the manifest at the commit")
    want = {d["id"]: man["decisions"][d["id"]]["option"] for d in owner_only(gate, root)}
    for i, o in h["decisions"].items():
        if want.get(i) != o:
            errs.append(f"{i}: not an owner-only decision of the manifest with option {o!r}")
    if errs:
        return errs
    if text != render(full, root, include=set(h["decisions"])):
        errs.append("the document is not byte-identical to what `render` produces for this commit and this decision set")
    return errs


# ---------------------------------------------------------------------------------------------------------------- apply
def fingerprint(pubkey: Path) -> str:
    out = subprocess.run(["/usr/bin/ssh-keygen", "-l", "-f", str(pubkey)], capture_output=True, env={"PATH": "/usr/bin:/bin"}, check=True).stdout.decode()
    return out.split()[1]


def apply(doc: Path, pubkey: Path, decided_by: str, date: str, root: Path = ROOT) -> list[str]:
    """Write the gate entries and the signer list after the signature exists. Idempotent; refuses the release key."""
    import datetime
    datetime.date.fromisoformat(date)
    raw = doc.read_bytes()
    h = parse_header(raw.decode("utf-8"), root)
    if pubkey.stat().st_size > 4096:
        raise SystemExit("the public key file is too large")
    if "PRIVATE" in pubkey.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit("that file looks like a private key")
    fp = fingerprint(pubkey)
    if fp == RELEASE_KEY_FPR:
        raise SystemExit("refusing: that is the published RELEASE key; use a separate legal-decision key")
    pk = pubkey.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+( [^\n]*)?", pk) or "PRIVATE" in pk or "\n" in pk:
        raise SystemExit("the public key file must hold exactly one public key line")
    target = root / DOC_REL
    for q in (root / "docs", root / "docs/legal", root / "docs/legal/approved", target, Path(str(target) + ".sig"), root / GATE, root / ALLOWED):
        if q.is_symlink():
            raise SystemExit(f"refusing to write through a symlink: {q}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_bytes() != raw:
        target.write_bytes(raw)
    gate_path = root / GATE
    orig = gate_path.read_text(encoding="utf-8")
    g = json.loads(orig)
    changed = []
    frozen = gate_mod(root).COUNSEL_NAMED
    for d in g["decisions"]:
        if d["id"] in h["decisions"]:
            if d["decides"] != "owner" or d["id"] in frozen:
                raise SystemExit(f"{d['id']} is not owner-only")
            d.update(status="DECIDED", chosen_option=h["decisions"][d["id"]], decision_reference=DOC_REL, decision_sha256=sha(raw),
                     decided_by=decided_by, decided_on=date)
            changed.append(d["id"])
    g["legal_signer_fingerprints"] = sorted(set(g.get("legal_signer_fingerprints", [])) | {fp})
    unchanged = json.dumps(json.loads(orig), indent=1, ensure_ascii=False) + "\n"
    if unchanged != orig:
        raise SystemExit("the gate file is not in its canonical one-space-indent form; refusing to reformat it")
    gate_path.write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    al = root / ALLOWED
    lines = al.read_text(encoding="utf-8").splitlines() if al.exists() else []
    entry = f'redevops-legal-decision namespaces="redevops-legal-decision@v1" {" ".join(pk.split()[:2])}'
    if entry not in lines:
        lines.append(entry)
    al.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def check(root: Path = ROOT) -> list[str]:
    """Verify, with the release gate's own functions, that the committed approval document carries a valid signature by a listed signer and that
    EVERY decision of its signed header (blocking or not) is DECIDED in the gate file with matching option, reference and hash. Also runs the gate's
    signer-list anchor and release-key separation checks."""
    mod = gate_mod(root)
    g = json.loads((root / GATE).read_text(encoding="utf-8"))
    doc = root / DOC_REL
    if not doc.is_file():
        return [f"{DOC_REL} is missing"]
    try:
        h = parse_header(doc.read_text(encoding="utf-8"), root)
    except SystemExit as e:
        return [str(e)]
    out: list[str] = []
    signers = g.get("legal_signer_fingerprints", [])
    out += [f"signer list: {m}" for m in mod.anchor_problems(root / ALLOWED, signers)]
    out += [f"signer list: {m}" for m in mod.separation_problems(root / ALLOWED)]
    if not signers:
        out.append("signer list: the gate file lists no legal-decision signer")
    _, why = mod.verify_sshsig_bytes(root, DOC_REL, root / ALLOWED, signers)
    if why:
        out.append(f"signature: {why}")
    by_id = {d["id"]: d for d in g["decisions"]}
    raw_sha = sha(doc.read_bytes())
    cache: dict = {}
    for i, o in h["decisions"].items():
        d = by_id.get(i)
        if d is None:
            out.append(f"{i}: not a decision of the gate file")
            continue
        if d.get("status") != "DECIDED" or d.get("chosen_option") != o or d.get("decision_reference") != DOC_REL or d.get("decision_sha256") != raw_sha:
            out.append(f"{i}: the gate entry does not record this bundle (status, option, reference, hash)")
            continue
        out += mod._record_problems(d, root, root / ALLOWED, signers, cache)
        out += mod._decided_problems(d, [x["key"] for x in d["options"]], root)
    return list(dict.fromkeys(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render"); r.add_argument("--commit", default="HEAD"); r.add_argument("--out", type=Path, required=True)
    r.add_argument("--strike", action="append", default=[], help="owner-only decision id to leave out (stays OPEN); repeatable")
    v = sub.add_parser("verify"); v.add_argument("--doc", type=Path, required=True); v.add_argument("--commit")
    a = sub.add_parser("apply"); a.add_argument("--doc", type=Path, required=True); a.add_argument("--pubkey", type=Path, required=True)
    a.add_argument("--decided-by", required=True); a.add_argument("--date", required=True)
    d = sub.add_parser("digest"); d.add_argument("--doc", type=Path, required=True)
    sub.add_parser("check")
    a_ = ap.parse_args(argv)
    if a_.cmd == "render":
        inc = None
        if a_.strike:
            full = git("rev-parse", a_.commit + "^{commit}")
            ids = {d["id"] for d in owner_only(load(full)[2])}
            if not set(a_.strike) <= ids:
                raise SystemExit(f"--strike names a decision that is not owner-only: {sorted(set(a_.strike) - ids)}")
            inc = ids - set(a_.strike)
        a_.out.write_text(render(a_.commit, include=inc), encoding="utf-8")
        print(sha(a_.out.read_bytes()), a_.out)
        return 0
    if a_.cmd == "verify":
        errs = verify(a_.doc, a_.commit)
        for e in errs:
            print("ERROR:", e, file=sys.stderr)
        print("approval document verified against its commit" if not errs else f"{len(errs)} problem(s)")
        return 1 if errs else 0
    if a_.cmd == "check":
        errs = check()
        for e in errs:
            print("ERROR:", e, file=sys.stderr)
        print("signature and binding of the bundled decisions verified by the gate" if not errs else f"{len(errs)} problem(s)")
        return 1 if errs else 0
    if a_.cmd == "digest":
        print(sha(a_.doc.read_bytes()))
        return 0
    changed = apply(a_.doc, a_.pubkey, a_.decided_by, a_.date)
    print("gate entries set DECIDED:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
