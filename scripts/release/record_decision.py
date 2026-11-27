#!/usr/bin/env python3
"""A single signed decision record, as part of the release signing (``authorize_release.sh --record <id>``).

  record_decision.py verify --id D-LIC-11             the record is the one expected, and says what will be written
  record_decision.py digest --id D-LIC-11             print the sha256 of the record
  record_decision.py apply  --id D-LIC-11 --date YYYY-MM-DD    (after the signature exists) write the gate entry
  record_decision.py check  --id D-LIC-11             the release gate accepts the signature and the entry

``apply`` changes in ``packaging/distribution-gate.json`` exactly the fields the record's section 5 names, and refuses
when a value is not written in the record's own signed bytes. The chosen option never changes. It signs nothing, makes
no key and edits no signed document. Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = "packaging/distribution-gate.json"
ALLOWED = "packaging/legal-allowed-signers"

#: record id -> where it is and what its section 5 says the gate entry records once it is signed
RECORDS = {
    "D-LIC-11": {
        "doc": "docs/legal/approved/D-LIC-11_RAILS_MARK_0.6.md",
        "signer_name": "Alex Rybachek",
        "decided_by": "Alex Rybachek (owner), record {sha}",
        "ships_meanwhile": "the rAIls prime-spiral mark generated from code (brand/rails_mark.py), provenance and hashes in "
                           "docs/legal/ARTWORK_PROVENANCE.md; LICENSING.md section 4 does not list the icons as covered",
    },
}


def gate_mod(root: Path):
    spec = importlib.util.spec_from_file_location("record_gate", root / "packaging" / "check-distribution-gate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["record_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def _record(ident: str) -> dict:
    if ident not in RECORDS:
        raise SystemExit(f"no such record: {ident}; known: {', '.join(RECORDS)}")
    return RECORDS[ident]


def raw_of(ident: str, root: Path) -> bytes:
    path = root / _record(ident)["doc"]
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{_record(ident)['doc']} is not a plain file here")
    return path.read_bytes()


def entry_of(ident: str, root: Path) -> dict:
    found = [d for d in json.loads((root / GATE).read_text(encoding="utf-8"))["decisions"] if d["id"] == ident]
    if len(found) != 1:
        raise SystemExit(f"the gate file does not have exactly one {ident}")
    return found[0]


def verify(ident: str, root: Path = ROOT) -> list[str]:
    spec, raw = _record(ident), raw_of(ident, root)
    text, entry, out = raw.decode("utf-8"), entry_of(ident, root), []
    head = text.split("\n\n", 1)[0].splitlines()
    if head != ["redevops-legal-decision: 1", f"decision: {ident}", f"option: {entry.get('chosen_option')}"]:
        out.append(f"the record's header {head} does not name {ident} with the option the gate records ({entry.get('chosen_option')})")
    for value in (spec["doc"], spec["ships_meanwhile"], spec["decided_by"].split(", record")[0]):
        if value.lower() not in " ".join(text.split()).lower().replace("`", ""):
            out.append(f"the record's own text does not say: {value[:90]}")
    if entry.get("decision_reference") == spec["doc"] and (root / (spec["doc"] + ".sig")).exists():
        out.append("this record is already signed and recorded")
    return out


def apply(ident: str, date: str, root: Path = ROOT) -> dict:
    datetime.date.fromisoformat(date)
    problems = verify(ident, root)
    if problems:
        raise SystemExit("; ".join(problems))
    spec, digest = _record(ident), hashlib.sha256(raw_of(ident, root)).hexdigest()
    path = root / GATE
    current = path.read_text(encoding="utf-8")
    gate = json.loads(current)
    if path.is_symlink() or json.dumps(gate, indent=1, ensure_ascii=False) + "\n" != current:
        raise SystemExit("the gate file is not in its canonical one-space-indent form; refusing to reformat it")
    changed = {"decision_reference": spec["doc"], "decision_sha256": digest, "decided_by": spec["decided_by"].format(sha=digest),
               "decided_on": date, "ships_meanwhile": spec["ships_meanwhile"]}
    next(d for d in gate["decisions"] if d["id"] == ident).update(changed)
    path.write_text(json.dumps(gate, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def check(ident: str, root: Path = ROOT) -> list[str]:
    """With the release gate's own functions: the signature over the record verifies against a listed legal signer, and the
    entry records exactly this record."""
    mod, spec = gate_mod(root), _record(ident)
    gate = json.loads((root / GATE).read_text(encoding="utf-8"))
    signers = gate.get("legal_signer_fingerprints", [])
    entry = entry_of(ident, root)
    out = [f"signer list: {m}" for m in mod.anchor_problems(root / ALLOWED, signers)]
    out += [f"signer list: {m}" for m in mod.separation_problems(root / ALLOWED)]
    _, why = mod.verify_sshsig_bytes(root, spec["doc"], root / ALLOWED, signers)
    if why:
        out.append(f"signature: {why}")
    if entry.get("decision_reference") != spec["doc"] or entry.get("decision_sha256") != hashlib.sha256(raw_of(ident, root)).hexdigest():
        out.append(f"{ident}: the gate entry does not record this record")
    out += mod._record_problems(entry, root, root / ALLOWED, signers, {})
    out += mod._decided_problems(entry, [o["key"] for o in entry["options"]], root)
    return list(dict.fromkeys(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("verify", "digest", "apply", "check", "name", "path"))
    ap.add_argument("--id", required=True)
    ap.add_argument("--date")
    a = ap.parse_args(argv)
    if a.command == "digest":
        print(hashlib.sha256(raw_of(a.id, ROOT)).hexdigest())
    elif a.command == "name":
        print(_record(a.id)["signer_name"])
    elif a.command == "path":
        print(_record(a.id)["doc"])
    elif a.command == "apply":
        print(json.dumps(apply(a.id, a.date or "", ROOT), indent=1))
    else:
        problems = (verify if a.command == "verify" else check)(a.id, ROOT)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
