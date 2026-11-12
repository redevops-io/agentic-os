#!/usr/bin/env python3
"""Render, verify and apply the OWNER DECISION WITHOUT COUNSEL for the counsel-named decisions of release 0.6.0.

  waiver_approval.py render --commit F2 --out FILE      render OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md for commit F2 (DRAFT, nothing signed)
  waiver_approval.py verify --doc FILE [--commit F2]    check a rendered document against commit F2 (header, manifest hash, options, slot texts)
  waiver_approval.py apply  --doc FILE --pubkey FILE --decided-by TEXT --date YYYY-MM-DD   (used by authorize_release.sh --waiver)
  waiver_approval.py check                              verify, with the gate's own functions, the committed document and the gate entries
  waiver_approval.py digest --doc FILE

What this is. The release gate freezes twelve decisions as "counsel-named"; each normally needs a counsel reference. The owner has stated that no
counsel exists and that the owner decides them. This document records exactly that, as an explicit WAIVER: the signed header says
`counsel-consulted: no` and `waiver: owner-decision-without-counsel`, every gate entry it decides carries the counsel_reference
`owner-waiver:<sha256 of this signed document>` (never an invented counsel reference), and nothing in it is a legal conclusion or a counsel
approval. The gate verifies the owner's SSHSIG over these exact bytes (record header version 3, packaging/check-distribution-gate.py); the
About-slot texts printed in section 3 are authorized by their sha256 lines in the same signature.

The document is generated from packaging/release/decision-manifest-0.6.0.json (section `counsel_waiver`), packaging/distribution-gate.json and the
slot text files AS THEY ARE AT commit F2 (read with `git show`), so the same commit always renders the same bytes. It never contains a signature,
a secret or a timestamp. Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import re
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval as ap  # noqa: E402

ROOT = ap.ROOT
DOC_REL = "docs/legal/approved/OWNER_DECISION_WITHOUT_COUNSEL_0.6.0.md"
BUNDLE_NAME = "OWNER_DECISION_WITHOUT_COUNSEL_0.6.0"
BANNER = ap.BANNER


_ODD = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")


def plain(text: str, what: str) -> str:
    """Text that is embedded in the document the owner reads and signs: no control, bidi or zero-width characters (they can hide content from a
    terminal or viewer) and no line that opens or closes a code fence."""
    if _ODD.search(text):
        raise SystemExit(f"{what} holds a control, bidirectional or zero-width character")
    if any(ln.lstrip().startswith(("~~~", "```")) for ln in text.split("\n")):
        raise SystemExit(f"{what} holds a line that starts a code fence")
    return text


def waiver_section(man: dict) -> dict:
    w = man.get("counsel_waiver")
    if not isinstance(w, dict) or not isinstance(w.get("decisions"), dict) or not isinstance(w.get("slots"), dict):
        raise SystemExit("the manifest has no counsel_waiver section (decisions, slots)")
    return w


def plan(commit: str, root: Path = ROOT, include: set | None = None):
    """(commit, tree, manifest sha, manifest, gate, wanted decisions {id: option}, slots {slot: (file, sha256)}). Refuses anything inconsistent."""
    commit = ap.git("rev-parse", commit + "^{commit}", root=root)
    tree = ap.git("rev-parse", commit + "^{tree}", root=root)
    man_raw, man, gate = ap.load(commit, root)
    mod = ap.gate_mod(root)
    frozen = set(mod.COUNSEL_NAMED)
    w = waiver_section(man)
    if set(w["decisions"]) != frozen:
        raise SystemExit(f"counsel_waiver.decisions must name exactly the frozen counsel-named set; difference: {sorted(set(w['decisions']) ^ frozen)}")
    by_id = {d["id"]: d for d in gate["decisions"]}
    for i, m in w["decisions"].items():
        d = by_id.get(i)
        if d is None or m.get("option") not in [o["key"] for o in d["options"]]:
            raise SystemExit(f"counsel_waiver names an unknown decision or option: {i} {m.get('option')}")
        if not str(m.get("owner_statement") or "").strip():
            raise SystemExit(f"counsel_waiver: {i} has no owner_statement")
        plain(m["owner_statement"], f"the owner statement of {i}")
        for o in d["options"]:
            plain(o["text"], f"an option text of {i}")
        if d["status"] != "OPEN":
            raise SystemExit(f"{i} is not OPEN at {commit[:12]}: a decision is recorded once")
    include = set(w["decisions"]) if include is None else include
    if not include or not include <= frozen:
        raise SystemExit("include must be a non-empty subset of the counsel-named decisions")
    slots = {}
    for slot, spec in w["slots"].items():
        s = mod.ABOUT_SLOTS.get(slot)
        if s is None:
            raise SystemExit(f"counsel_waiver names an unknown slot {slot}")
        if s["decisions"][0] not in include:
            continue                                   # the primary decision is not in this document: the slot is not authorized by it
        rel = spec.get("file", "")
        if not mod.SLOT_TEXT_RE.match(rel):
            raise SystemExit(f"slot {slot}: file must match {mod.SLOT_TEXT_DIR}/<name>.txt")
        raw = ap.show(commit, rel, root)
        if not raw.strip() or len(raw) > mod.SLOT_TEXT_MAX_BYTES or b"\r" in raw:
            raise SystemExit(f"slot {slot}: text file {rel} is empty, too large or has CR characters")
        plain(raw.decode("utf-8"), f"the text of {slot}")
        slots[slot] = (rel, ap.sha(raw))
    missing = sorted(sl for sl, s in mod.ABOUT_SLOTS.items() if s["decisions"][0] in include and sl not in slots and not s["omittable"])
    if missing:
        raise SystemExit(f"counsel_waiver gives no text for the non-omittable slots {missing}")
    return commit, tree, ap.sha(man_raw), man, gate, {i: w["decisions"][i]["option"] for i in sorted(include)}, slots


def header_lines(commit, tree, man_sha, wanted, slots) -> list[str]:
    mod = ap.gate_mod()
    out = [f"redevops-legal-decision: {mod.WAIVER_VERSION}", f"bundle: {BUNDLE_NAME}", f"base-commit: {commit}", f"base-tree: {tree}",
           f"manifest-sha256: {man_sha}", "counsel-consulted: no", f"waiver: {mod.WAIVER_STATEMENT}"]
    out += [f"decision: {i} {o}" for i, o in wanted.items()]
    out += [f"slot: {sl} sha256 {h}" for sl, (_, h) in sorted(slots.items())]
    return out


def render(commit: str, root: Path = ROOT, include: set | None = None) -> str:
    commit, tree, man_sha, man, gate, wanted, slots = plan(commit, root, include)
    by_id = {d["id"]: d for d in gate["decisions"]}
    w = waiver_section(man)
    o = man["owner"]
    plain(w["why"], "counsel_waiver.why")
    for r_ in w["accepted_risks"]:
        plain(r_, "an accepted risk")
    plain(o["signature_name_on_prior_forms"] + o["initials_on_prior_forms"], "the owner name")
    L: list[str] = header_lines(commit, tree, man_sha, wanted, slots)
    a = L.append
    a("")
    a("# OWNER DECISION WITHOUT COUNSEL, RELEASE 0.6.0")
    a("")
    a(f"> **{BANNER}.** This document is a draft prepared by an agent. It authorizes nothing by itself, including after it has been read, initialled or")
    a("> copied. It becomes the owner's decision only if the owner signs these exact bytes with the legal-decision key and the release gate verifies that")
    a("> signature (section 6). **No counsel was consulted and none approved anything in it.** It is not a legal opinion, a counsel approval or a legal")
    a("> conclusion, and it contains no counsel reference. The banner stays in the signed bytes on purpose: the signature, not the banner, is the decision.")
    a("")
    a("## 0. What you are deciding")
    a("")
    a(f"By signing this document once you would, **for release 0.6.0 and the base commit `{commit}`** (tree `{tree}`):")
    a("")
    a(f"1. **Decide {len(wanted)} decisions that the release gate names as counsel decisions** ({', '.join(wanted)}) yourself, with the options in section 1, **without counsel**. {w['why']}")
    a(f"2. **Approve {len(slots)} About-slot texts** (section 3): {', '.join(sorted(slots))}. Each is authorized by its sha256 line in the signed header; no separate signature is needed.")
    a("3. **State the owner statements of section 2 as your own decisions and accept the risks of section 4.**")
    a("")
    a("**What this changes in the gate:** each listed entry becomes DECIDED with the `counsel_reference` `owner-waiver:<sha256 of this signed document>`, so that anyone reading the gate file sees that the decision was made by the owner without counsel and which document records it. The gate still checks your signature over these exact bytes.")
    a("")
    a("**What this does not change:** the legal questions behind these decisions (section 4) remain unanswered by any lawyer. Nothing here says that the Commons Clause and the AGPL are compatible, that any notice set is sufficient, or that any distribution is permitted. The tag `v0.6.0` and the first approval (`RELEASE_OWNER_APPROVAL_0.6.0.md`) are not changed.")
    a("")
    a("## 1. Decisions and chosen options")
    a("")
    a("| Gate id | Decision | Decides (in the gate) | Chosen option | What the option means | Counsel questions left unanswered |")
    a("|---|---|---|---|---|---|")
    for i, opt in wanted.items():
        d = by_id[i]
        a(f"| {i} | {ap.esc(d['title'])} | {d['decides']} | **{opt}** | {ap.esc(ap.opt_text(d, opt))} | {', '.join(d['counsel_questions']) or '-'} |")
    a("")
    a("Where an option's wording mentions counsel (for example 'counsel words the offer'), this record is still made without counsel: the owner statement of section 2 says what is decided.")
    a("")
    a("## 2. Owner statements")
    a("")
    for i in wanted:
        a(f"* **{i}.** {w['decisions'][i]['owner_statement']}")
    a("")
    a("## 3. About-slot texts approved by this signature")
    a("")
    a("Each text below is the exact content of the file named, whose sha256 is in the signed header. The product shows it as an approved text only while the file's bytes match.")
    a("")
    if not slots:
        a("(none in this document)")
        a("")
    for sl, (rel, h) in sorted(slots.items()):
        raw = ap.show(commit, rel, root).decode("utf-8")
        a(f"### {sl}")
        a("")
        a(f"File `{rel}`, sha256 `{h}`:")
        a("")
        a("~~~text")
        a(raw.rstrip("\n"))
        a("~~~")
        a("")
    for sl, s in sorted(mod_slots().items()):
        if s["omittable"] and sl not in slots and s["decisions"][0] in wanted:
            a(f"* {sl} is not given a text here (omittable slot).")
    a("## 4. Risks you accept by deciding without counsel")
    a("")
    for r in w["accepted_risks"]:
        a(f"* {r}")
    a("")
    a("The unresolved legal questions of the counsel brief (`docs/legal/COUNSEL_BRIEF.md`) are not answered by this decision; `docs/legal/COMPATIBILITY_ASSESSMENT.md` is an engineering assessment and not a legal opinion.")
    a("")
    a("## 5. Source and artifacts")
    a("")
    a("| Item | Value |")
    a("|---|---|")
    a(f"| Base commit F2 (the commit this document is generated on) | `{commit}` |")
    a(f"| Its source tree | `{tree}` |")
    a(f"| Decision manifest at F2 | `{ap.MANIFEST}`, sha256 `{man_sha}` |")
    a("")
    a("The release commit rule of the first approval applies: the commit that records this decision differs from F2 in exactly three paths (this document, its signature and the gate file); the signer list does not change.")
    a("")
    a("## 6. How this decision becomes effective")
    a("")
    a("1. You read this document and its sha256 (printed by `scripts/release/authorize_release.sh --waiver`, shown separately from the file).")
    a("2. You say explicitly that you approve this exact document.")
    a("3. You run `scripts/release/authorize_release.sh --waiver --doc <file> --pubkey <legal-decision public key>` at your own terminal. It works on a private copy, re-checks it against the commit, shows the digest and the key fingerprint, asks you to type both and the signing name of section 7, signs the copy once with `ssh-keygen -Y sign` (namespace `redevops-legal-decision@v1`, agent-held key), writes the document, its signature and the gate entries, asks the release gate itself to verify them, and commits exactly those three paths. It signs nothing else and pushes nothing.")
    a("4. `packaging/check-distribution-gate.py` verifies the signature, requires the header lines `counsel-consulted: no` and `waiver: owner-decision-without-counsel`, maps each decision and slot of the signed header to its gate entry, and refuses an entry whose counsel_reference is not exactly `owner-waiver:<sha256 of this document>`.")
    a("")
    a("## 7. Signature block (prefilled DRAFT)")
    a("")
    a("The values below are prefilled from the owner's earlier signed form. **They are not an act of the owner and not a signature.** The date is never prefilled.")
    a("")
    a(f"* Owner name as signing (from the 2026-11-10 form): `____{o['signature_name_on_prior_forms']}____`  *(prefilled, DRAFT; `authorize_release.sh` makes the owner type exactly this name, so it is part of the signed bytes)*")
    a(f"* Initials: `_{o['initials_on_prior_forms']}_`  *(prefilled, DRAFT)*")
    a("* Date: `____________`  *(left blank; set at the signing run)*")
    a(f"* Status: **{BANNER}**")
    a("")
    return "\n".join(L) + "\n"


def mod_slots() -> dict:
    return ap.gate_mod().ABOUT_SLOTS


# ---------------------------------------------------------------------------------------------------------------- verify
def parse_header(text: str, root: Path = ROOT) -> dict:
    mod = ap.gate_mod(root)
    hdr, probs = mod.parse_bundle_record(text.encode("utf-8"))
    if probs:
        raise SystemExit("not a valid owner-decision-without-counsel header: " + "; ".join(probs))
    if hdr["version"] != mod.WAIVER_VERSION:
        raise SystemExit("not a version 3 (owner decision without counsel) record")
    return hdr


def verify(doc: Path, commit: str | None = None, root: Path = ROOT) -> list[str]:
    """The document must be byte-identical to what `render` produces for its commit and its decision set."""
    text = doc.read_bytes().decode("utf-8")
    try:
        h = parse_header(text, root)
    except SystemExit as e:
        return [str(e)]
    commit = commit or h.get("base-commit", "")
    try:
        full = ap.git("rev-parse", commit + "^{commit}", root=root)
    except subprocess.CalledProcessError:
        return [f"commit {commit!r} is not in the repository"]
    errs: list[str] = []
    if h.get("base-commit") != full:
        errs.append(f"header base-commit {h.get('base-commit')} is not {full}")
    if h.get("base-tree") != ap.git("rev-parse", full + "^{tree}", root=root):
        errs.append("header base-tree does not match the tree of the commit")
    try:
        want = render(full, root, include=set(h["decisions"]))
    except SystemExit as e:
        return errs + [str(e)]
    if text != want:
        errs.append("the document is not byte-identical to what `render` produces for this commit and this decision set")
    return errs


# ---------------------------------------------------------------------------------------------------------------- apply
def apply(doc: Path, pubkey: Path, decided_by: str, date: str, root: Path = ROOT) -> list[str]:
    """Write the gate entries and the signer list after the signature exists. Refuses the release key. Idempotent."""
    import datetime
    datetime.date.fromisoformat(date)
    raw = doc.read_bytes()
    mod = ap.gate_mod(root)
    h = parse_header(raw.decode("utf-8"), root)
    if pubkey.stat().st_size > 4096:
        raise SystemExit("the public key file is too large")
    if "PRIVATE" in pubkey.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit("that file looks like a private key")
    fp = ap.fingerprint(pubkey)
    if fp == ap.RELEASE_KEY_FPR:
        raise SystemExit("refusing: that is the published RELEASE key; use a separate legal-decision key")
    pk = pubkey.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+( [^\n]*)?", pk) or "PRIVATE" in pk or "\n" in pk:
        raise SystemExit("the public key file must hold exactly one public key line")
    gate_path = root / ap.GATE
    current = gate_path.read_text(encoding="utf-8")
    if json.dumps(json.loads(current), indent=1, ensure_ascii=False) + "\n" != current:
        raise SystemExit("the gate file is not in its canonical one-space-indent form; refusing to reformat it")
    target = root / DOC_REL
    for q in (root / "docs", root / "docs/legal", root / "docs/legal/approved", target, Path(str(target) + ".sig"), gate_path, root / ap.ALLOWED):
        if q.is_symlink():
            raise SystemExit(f"refusing to write through a symlink: {q}")
    # the slot files are named by the manifest AT the base commit; their sha256 must equal the signed header lines
    commit = h["base-commit"]
    man = ap.load(commit, root)[1]
    slot_files = {sl: spec["file"] for sl, spec in waiver_section(man)["slots"].items()}
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_bytes() != raw:
        target.write_bytes(raw)
    g = json.loads(current)
    rec_sha = ap.sha(raw)
    changed = []
    for d in g["decisions"]:
        i = d["id"]
        if i not in h["decisions"]:
            continue
        if i not in mod.COUNSEL_NAMED:
            raise SystemExit(f"{i} is not a counsel-named decision")
        d.update(status="DECIDED", chosen_option=h["decisions"][i], decision_reference=DOC_REL, decision_sha256=rec_sha, decided_by=decided_by,
                 decided_on=date, counsel_reference=mod.WAIVER_REF_PREFIX + rec_sha)
        mine = {sl: v for sl, v in h["slots"].items() if mod.ABOUT_SLOTS[sl]["decisions"][0] == i}
        if mine:
            d["slot_texts"] = {}
            for sl, v in sorted(mine.items()):
                if v == "not-applicable":
                    d["slot_texts"][sl] = {"not_applicable": True}
                else:
                    if sl not in slot_files:
                        raise SystemExit(f"the manifest names no text file for {sl}")
                    d["slot_texts"][sl] = {"file": slot_files[sl], "sha256": v}
        changed.append(i)
    g["legal_signer_fingerprints"] = sorted(set(g.get("legal_signer_fingerprints", [])) | {fp})
    gate_path.write_text(json.dumps(g, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    al = root / ap.ALLOWED
    lines = al.read_text(encoding="utf-8").splitlines() if al.exists() else []
    entry = f'redevops-legal-decision namespaces="redevops-legal-decision@v1" {" ".join(pk.split()[:2])}'
    if entry not in lines:
        lines.append(entry)
    al.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def check(root: Path = ROOT) -> list[str]:
    """Verify, with the release gate's own functions, that the committed document carries a valid signature by a listed signer and that EVERY
    decision and slot of its signed header is DECIDED / approved in the gate file with matching option, reference, hash and counsel_reference."""
    mod = ap.gate_mod(root)
    g = json.loads((root / ap.GATE).read_text(encoding="utf-8"))
    doc = root / DOC_REL
    if not doc.is_file():
        return [f"{DOC_REL} is missing"]
    try:
        h = parse_header(doc.read_text(encoding="utf-8"), root)
    except SystemExit as e:
        return [str(e)]
    out: list[str] = []
    signers = g.get("legal_signer_fingerprints", [])
    out += [f"signer list: {m}" for m in mod.anchor_problems(root / ap.ALLOWED, signers)]
    out += [f"signer list: {m}" for m in mod.separation_problems(root / ap.ALLOWED)]
    if not signers:
        out.append("signer list: the gate file lists no legal-decision signer")
    _, why = mod.verify_sshsig_bytes(root, DOC_REL, root / ap.ALLOWED, signers)
    if why:
        out.append(f"signature: {why}")
    by_id = {d["id"]: d for d in g["decisions"]}
    raw_sha = ap.sha(doc.read_bytes())
    cache: dict = {}
    for i, o in h["decisions"].items():
        d = by_id.get(i)
        if d is None:
            out.append(f"{i}: not a decision of the gate file")
            continue
        if d.get("status") != "DECIDED" or d.get("chosen_option") != o or d.get("decision_reference") != DOC_REL or d.get("decision_sha256") != raw_sha:
            out.append(f"{i}: the gate entry does not record this document (status, option, reference, hash)")
            continue
        out += mod._record_problems(d, root, root / ap.ALLOWED, signers, cache)
        out += mod._decided_problems(d, [x["key"] for x in d["options"]], root)
    if out:
        return list(dict.fromkeys(out))
    # the About slots of the decisions this document covers must resolve to approved, signature-verified texts (file path, hash, encoding)
    covered = set(h["decisions"])
    slots, slot_errs, refusals = mod.resolve_about_slots(g["decisions"], root, True, allowed_signers=root / ap.ALLOWED, signers=signers)
    out += [f"About slot: {e}" for e in slot_errs]
    out += [f"About slot: {r_}" for r_ in refusals if any(r_.startswith(sl) for sl, sp_ in mod.ABOUT_SLOTS.items() if sp_["decisions"][0] in covered)]
    return list(dict.fromkeys(out))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render"); r.add_argument("--commit", default="HEAD"); r.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify"); v.add_argument("--doc", type=Path, required=True); v.add_argument("--commit")
    a = sub.add_parser("apply"); a.add_argument("--doc", type=Path, required=True); a.add_argument("--pubkey", type=Path, required=True)
    a.add_argument("--decided-by", required=True); a.add_argument("--date", required=True)
    d = sub.add_parser("digest"); d.add_argument("--doc", type=Path, required=True)
    sub.add_parser("check")
    x = p.parse_args(argv)
    if x.cmd == "render":
        x.out.write_text(render(x.commit), encoding="utf-8")
        print(ap.sha(x.out.read_bytes()), x.out)
        return 0
    if x.cmd == "verify":
        errs = verify(x.doc, x.commit)
        for e in errs:
            print("ERROR:", e, file=sys.stderr)
        print("document verified against its commit" if not errs else f"{len(errs)} problem(s)")
        return 1 if errs else 0
    if x.cmd == "check":
        errs = check()
        for e in errs:
            print("ERROR:", e, file=sys.stderr)
        print("signature and binding of the owner decisions verified by the gate" if not errs else f"{len(errs)} problem(s)")
        return 1 if errs else 0
    if x.cmd == "digest":
        print(ap.sha(x.doc.read_bytes()))
        return 0
    changed = apply(x.doc, x.pubkey, x.decided_by, x.date)
    print("gate entries set DECIDED:", ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
