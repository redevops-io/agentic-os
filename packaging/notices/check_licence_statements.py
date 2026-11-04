#!/usr/bin/env python3
"""Check that every product-licence field agrees with LICENSING.md.

LICENSING.md holds the authoritative statement between the markers
`<!-- licence-statement:begin -->` / `<!-- licence-statement:end -->`. This script reads the sentence and the
SPDX LicenseRef from there and compares each shipped field with it. It checks agreement of wording only; it
does not decide what the wording should be (that is [OWNER DECISION D-LIC-1]).

Exit code 0 = all fields agree, 1 = at least one does not. Standard library only (Python 3.11+: tomllib).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BLOCK = re.compile(r"<!-- licence-statement:begin -->\n(.*?)\n<!-- licence-statement:end -->", re.S)
DECISION_MARK = re.compile(r"\[OWNER DECISION (D-LIC-\d+)\]")
FORBIDDEN_PRODUCT_CLAIMS = ("is open source", "are open source", "open-source licensed")


def statement(root: Path) -> tuple[str, str]:
    text = (root / "LICENSING.md").read_text(encoding="utf-8")
    m = BLOCK.search(text)
    if not m:
        raise SystemExit("LICENSING.md: licence-statement markers not found")
    block = m.group(1)
    sentence = next(l for l in block.splitlines() if l.strip())
    spdx = re.search(r"SPDX-License-Identifier:\s*(\S+)", block)
    if not spdx:
        raise SystemExit("LICENSING.md: SPDX-License-Identifier missing in the statement block")
    return sentence.strip(), spdx.group(1)


def check(root: Path = ROOT) -> list[str]:
    errs: list[str] = []
    sentence, spdx = statement(root)
    if "source-available (family licence: AGPL-3.0-or-later with the Commons Clause)" not in sentence:
        errs.append("LICENSING.md statement does not use the required family-licence wording")

    for f in ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md"):
        if not (root / f).is_file():
            errs.append(f"{f} is missing")
    if (root / "LICENSE-COMMONS-CLAUSE").is_file():
        cc = (root / "LICENSE-COMMONS-CLAUSE").read_text(encoding="utf-8")
        if "Commons Clause" not in cc or "Sell" not in cc:
            errs.append("LICENSE-COMMONS-CLAUSE does not look like the Commons Clause condition")
    lic_md = (root / "LICENSE.md").read_text(encoding="utf-8") if (root / "LICENSE.md").is_file() else ""
    if spdx not in lic_md:
        errs.append(f"LICENSE.md does not carry {spdx}")

    # pyproject
    py = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if py.get("license") != spdx:
        errs.append(f"pyproject [project] license is {py.get('license')!r}, expected {spdx!r}")
    for f in ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md"):
        if f not in py.get("license-files", []):
            errs.append(f"pyproject license-files does not list {f}")
    if any(c.startswith("License ::") for c in py.get("classifiers", [])):
        errs.append("pyproject carries a 'License ::' classifier (conflicts with the PEP 639 expression)")

    # launcher crate
    cargo = tomllib.loads((root / "deploy/launcher/src-tauri/Cargo.toml").read_text(encoding="utf-8"))["package"]
    if "license" in cargo:
        errs.append(f"launcher Cargo.toml has license = {cargo['license']!r}; the family licence has no SPDX id")
    lf = cargo.get("license-file")
    if not lf:
        errs.append("launcher Cargo.toml has no license-file")
    elif not (root / "deploy/launcher/src-tauri" / lf).resolve().samefile(root / "LICENSING.md"):
        errs.append(f"launcher Cargo.toml license-file {lf!r} does not resolve to LICENSING.md")

    # tauri bundle
    conf = json.loads((root / "deploy/launcher/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["bundle"]
    res = conf.get("resources")
    if not (isinstance(res, dict) and any(str(v).rstrip("/") == "Licenses" for v in res.values())):
        errs.append("tauri.conf.json bundle.resources does not deliver a Licenses/ folder")
    if "RedevOps" not in (conf.get("copyright") or ""):
        errs.append("tauri.conf.json bundle.copyright is not set to the RedevOps copyright line")

    # README, modules.yaml
    readme = (root / "README.md").read_text(encoding="utf-8")
    if not re.search(r"AGPL-3\.0.{0,12}Commons\s+Clause", readme.replace("\n", " "), re.S) \
            or "source-available" not in readme:
        errs.append("README.md licence section/badge does not state AGPL-3.0-or-later with the Commons Clause, "
                    "source-available")
    mods = (root / "modules.yaml").read_text(encoding="utf-8")[:600]
    if "AGPL-3.0-or-later WITH Commons Clause" not in mods or "source-available" not in mods:
        errs.append("modules.yaml header does not carry the family-licence statement")

    # the sidecar's own licensing answer
    ans = _licensing_answer(root)
    core = re.search(r"source-available \(family licence: [^)]*\)", sentence).group(0)
    if core not in ans:
        errs.append("agentic_os.stack_knowledge 'licensing' answer does not carry the LICENSING.md statement")
    for bad in FORBIDDEN_PRODUCT_CLAIMS:
        if bad in ans.lower():
            errs.append(f"licensing answer says {bad!r} about the product")

    # every marker in LICENSING.md is registered
    reg = root / "packaging" / "DECISIONS-REQUIRED.md"
    regtext = reg.read_text(encoding="utf-8") if reg.is_file() else ""
    for d in sorted(set(DECISION_MARK.findall((root / "LICENSING.md").read_text(encoding="utf-8")))):
        if f"{d}" not in regtext:
            errs.append(f"{d} appears in LICENSING.md but not in packaging/DECISIONS-REQUIRED.md")
    return errs


def _licensing_answer(root: Path) -> str:
    src = (root / "agentic_os" / "stack_knowledge.py").read_text(encoding="utf-8")
    i = src.index('id="licensing"')
    j = src.index("KnowledgeEntry(", i)
    seg = src[i:j]
    k = seg.index("answer=(")
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', seg[k:seg.index("source=")])
    return "".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    errs = check(ap.parse_args(argv).root)
    for e in errs:
        print("FAIL:", e, file=sys.stderr)
    print("licence statements agree with LICENSING.md" if not errs else f"{len(errs)} disagreement(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
