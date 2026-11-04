#!/usr/bin/env python3
"""Generate docs/legal/OWNER_DECISION_FORM.md from packaging/distribution-gate.json.

The form is the written record that the distribution gate (packaging/check-distribution-gate.py) asks for. It is generated
so that it cannot list other decisions, options or defaults than the gate file does; tests/test_distribution_gate.py
regenerates it and fails on any difference.

  python3 scripts/build_owner_decision_form.py           write the form
  python3 scripts/build_owner_decision_form.py --check   exit 1 if the committed form differs

The form is a TEMPLATE: the owner copies it, fills it in, signs it, stores the signed copy, and records its path and sha256
in the gate file. Nothing here is a decision, a clearance or legal advice. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "packaging" / "distribution-gate.json"
OUT = ROOT / "docs" / "legal" / "OWNER_DECISION_FORM.md"


def render(decisions: list[dict]) -> str:
    o: list[str] = []
    w = o.append
    w("# Owner decision form: R0-prime distribution")
    w("")
    w("This is the written record that the later distribution gate requires (`packaging/check-distribution-gate.py`,")
    w("`packaging/distribution-gate.json`). It is generated from the gate file (`scripts/build_owner_decision_form.py`); do not")
    w("edit this template, copy it. It is a form, not a decision: **no box is ticked here, and nothing in this repository says that")
    w("any component or distribution is cleared, compliant or permitted.** Only the owner can decide. Where a decision is marked")
    w("`counsel` or `owner+counsel`, counsel's written answer to the numbered questions in `docs/legal/COUNSEL_BRIEF.md` must exist")
    w("first; its reference goes in the counsel line.")
    w("")
    w("How to use it:")
    w("")
    w("1. Copy this file, fill in the header, tick one option per decision (or write another option in the free text), sign and date.")
    w("2. Store the signed copy (a PDF or a signed file) outside the build tree or under `docs/legal/decisions/`.")
    w("3. For each decided item, edit `packaging/distribution-gate.json`: `status` DECIDED, `chosen_option`, `decision_reference`")
    w("   (path or URL of the signed copy), `decision_sha256` (sha256 of that file), `decided_by`, `decided_on`, and `counsel_reference`")
    w("   when counsel decides. The gate refuses a DECIDED entry that lacks any of these.")
    w("4. A build with `REDEVOPS_DISTRIBUTION_BUILD=1` passes only when every blocking decision that applies to its platform is DECIDED.")
    w("")
    w("Header (to be completed by the owner)")
    w("")
    w("| Field | Entry |")
    w("|---|---|")
    w("| Owner (name, and the legal person on whose behalf this is signed) | |")
    w("| R0-prime commit and artifact names under decision | |")
    w("| Date of this form | |")
    w("| Counsel (name, firm), if consulted | |")
    w("")
    blocking = [d for d in decisions if d["blocks_distribution"]]
    other = [d for d in decisions if not d["blocks_distribution"]]
    for title, group in (("Part 1: distribution-blocking decisions", blocking), ("Part 2: other owner decisions (do not block the gate)", other)):
        w(f"## {title}")
        w("")
        for d in group:
            w(f"### {d['id']}: {d['title']}")
            w("")
            w(f"* Decides: **{d['decides']}**. Applies to: {', '.join(d['scope'])}. Blocks distribution: {'yes' if d['blocks_distribution'] else 'no'}. Status in the gate file when this form was generated: {d['status']}.")
            w(f"* Question: {d['question']}")
            w(f"* Ships meanwhile (private candidate): {d['ships_meanwhile']}")
            if d["counsel_questions"]:
                w(f"* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: {', '.join(d['counsel_questions'])}")
            w(f"* Evidence and consequences: [{d['id']}](DECISION_PACKAGES.md#{d['package'].split('#')[1]}) in the decision packages")
            w("")
            w("Options (tick one):")
            w("")
            for opt in d["options"]:
                w(f"- [ ] ({opt['key']}) {opt['text']}")
            w("- [ ] other (write it below)")
            w("")
            dflt = d["recommended_default"]
            w(f"Recommended default: **({dflt['option']})**. {dflt['note']}")
            w("")
            w("Owner's written decision (free text, reasons, conditions):")
            w("")
            w("> ")
            w("")
            if "counsel" in d["decides"]:
                w("Counsel's written answer (reference, date): ______________________________")
                w("")
            w("Date: ______________   Signature: ______________________________")
            w("")
            w("---")
            w("")
    w("## Final signature")
    w("")
    w("I have read the decision packages and the counsel brief for the decisions above. The decisions recorded in this form are mine;")
    w("nothing else is decided by it.")
    w("")
    w("Name: ______________________________   Date: ______________   Signature: ______________________________")
    return "\n".join(o).rstrip("\n") + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    text = render(json.loads(GATE.read_text(encoding="utf-8"))["decisions"])
    if a.check:
        ok = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("OWNER_DECISION_FORM.md is current" if ok else "OWNER_DECISION_FORM.md differs from the gate file: run scripts/build_owner_decision_form.py")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
