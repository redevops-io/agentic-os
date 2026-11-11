#!/usr/bin/env python3
"""Generate docs/legal/EXACT_TEXTS.md: the exact wording of the licence texts that the counsel questions refer to.

Every block is cut mechanically from a file of this repository (line ranges found by pattern, never retyped), and the
header of each block names the file, its sha256 and the line range. tests/test_legal_documents.py regenerates the
document and fails if the committed copy differs, so a quotation cannot drift from its source.

  python3 scripts/build_legal_exact_texts.py           write docs/legal/EXACT_TEXTS.md
  python3 scripts/build_legal_exact_texts.py --check   exit 1 if the committed file differs

This file reproduces texts. It does not interpret them. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "legal" / "EXACT_TEXTS.md"
TEXTS = "packaging/notices/texts"

# (id, title, file, start pattern, end pattern or None = to the end of file); the start line is included, the end line
# (the first later line matching the end pattern) is not.
AGPL_SEC = lambda n, nxt: (rf"^\s{{0,3}}{n}\. [A-Z]", rf"^\s{{0,3}}{nxt}\. [A-Z]")
SPECS = [
    ("agpl-0", "AGPL-3.0 section 0 (Definitions; 'Appropriate Legal Notices')", "LICENSE", *AGPL_SEC(0, 1)),
    ("agpl-1", "AGPL-3.0 section 1 (Source Code; 'Corresponding Source')", "LICENSE", *AGPL_SEC(1, 2)),
    ("agpl-5", "AGPL-3.0 section 5 (Conveying Modified Source Versions)", "LICENSE", *AGPL_SEC(5, 6)),
    ("agpl-6", "AGPL-3.0 section 6 (Conveying Non-Source Forms)", "LICENSE", *AGPL_SEC(6, 7)),
    ("agpl-7", "AGPL-3.0 section 7 (Additional Terms)", "LICENSE", *AGPL_SEC(7, 8)),
    ("agpl-9", "AGPL-3.0 section 9 (Acceptance Not Required for Having Copies)", "LICENSE", *AGPL_SEC(9, 10)),
    ("agpl-10", "AGPL-3.0 section 10 (Automatic Licensing of Downstream Recipients)", "LICENSE", *AGPL_SEC(10, 11)),
    ("agpl-13", "AGPL-3.0 section 13 (Remote Network Interaction)", "LICENSE", *AGPL_SEC(13, 14)),
    ("commons-clause", "Commons Clause License Condition v1.0 (whole file)", "LICENSE-COMMONS-CLAUSE", r"^\"Commons Clause\"", None),
    ("licence-md-effective", "LICENSE.md: effective point of the relicensing", "LICENSE.md", r"^## Effective point", r"^## What this permits"),
    ("licence-md-permits", "LICENSE.md: what the licence permits and does not", "LICENSE.md", r"^## What this permits", r"^## Why this changed"),
    ("licence-md-downstream", "LICENSE.md: what this means for anything depending on agentic-os", "LICENSE.md",
     r"^## What this means for anything depending", r"^## Scope of the Commons Clause condition"),
    ("licence-md-scope", "LICENSE.md: scope of the Commons Clause condition (\"the Software\")", "LICENSE.md",
     r"^## Scope of the Commons Clause condition", r"^## Contribution history"),
    ("licence-md-contrib", "LICENSE.md: contribution history", "LICENSE.md", r"^## Contribution history", None),
    ("mpl-1-4", "MPL-2.0 section 1.4 (Covered Software)",
     f"{TEXTS}/MPL-2.0.txt", r"^1\.4\. ", r"^1\.5\. "),
    ("mpl-1-6b", "MPL-2.0 section 1.6 (Executable Form) and 1.7 (Larger Work)", f"{TEXTS}/MPL-2.0.txt", r"^1\.6\. ", r"^1\.8\. "),
    ("mpl-1-13", "MPL-2.0 section 1.13 (Source Code Form)", f"{TEXTS}/MPL-2.0.txt", r"^1\.13\. ", r"^1\.14\. "),
    ("mpl-3", "MPL-2.0 sections 3.1 to 3.5 (Responsibilities)", f"{TEXTS}/MPL-2.0.txt", r"^3\. Responsibilities", r"^4\. Inability"),
    ("psf-2", "PSF License Version 2, sections 1 to 8, as printed in the CPython 3.12.10 LICENSE", f"{TEXTS}/CPython-3.12.10-LICENSE.txt",
     r"^PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2", r"^BEOPEN\.COM LICENSE AGREEMENT"),
    ("apache-4", "Apache License 2.0 sections 4 (Redistribution) and 5 (as printed in the OpenSSL LICENSE.txt)", f"{TEXTS}/OpenSSL-LICENSE.txt",
     r"^\s*4\. Redistribution\.", r"^\s*6\. Trademarks\."),
    ("pyinstaller-exception", "PyInstaller 6.22.3 COPYING.txt: the Bootloader Exception", f"{TEXTS}/PyInstaller-6.22.3-COPYING.txt",
     r"^Bootloader Exception", r"^Bootloader and Related Files"),
    ("nsis-applicable", "NSIS COPYING: which licence covers which part of the NSIS distribution", f"{TEXTS}/NSIS-trunk-COPYING.txt",
     r"^APPLICABLE LICENSES", r"^ZLIB/LIBPNG LICENSE"),
    ("nsis-cpl-3", "NSIS COPYING, Common Public License 1.0 section 3 (the licence of NSIS's LZMA module)", f"{TEXTS}/NSIS-trunk-COPYING.txt",
     r"^3\. REQUIREMENTS", r"^4\. COMMERCIAL DISTRIBUTION"),
    ("wix-ms-rl-3", "WiX Toolset v3 LICENSE.TXT: Microsoft Reciprocal License section 3 (Conditions and Limitations)", f"{TEXTS}/WiX3-LICENSE.txt",
     r"^3\. Conditions and Limitations", None),
    ("react-mit", "MIT licence text of React 18.3.1 (the permission and notice condition)", f"{TEXTS}/React-18.3.1-MIT-LICENSE.txt", r"^MIT License", None),
    ("selectors-mpl-header", "MPL-2.0 Exhibit A: the Source Code Form License Notice that MPL files carry", f"{TEXTS}/MPL-2.0.txt",
     r"^Exhibit A - Source Code Form License Notice", r"^You may add additional accurate notices"),
    ("bsd3-sample", "BSD-3-Clause as shipped by starlette 1.6.0 (sample of the attribution condition for binary form)",
     "docs/legal/sources/BSD-3-Clause-sample-starlette-1.6.0-LICENSE.md", r".", None),
    ("unicode-3-sample", "Unicode-3.0 as shipped in the icu_collections 2.3.0 crate (sample of the notice condition)",
     "docs/legal/sources/Unicode-3.0-sample-icu_collections-2.3.0-LICENSE.txt", r".", None),
    ("refi-authors", "r-efi 5.2.0: AUTHORS (the crate's only licence statement; at upstream commit db87f6c8 it is identical)",
     "docs/legal/sources/r-efi-5.2.0-AUTHORS.txt", r".", None),
    ("certifi-licence", "certifi 2026.10.19: the wheel's LICENSE file (the whole of it)", "docs/legal/sources/certifi-2026.10.19-LICENSE.txt", r".", None),
]


def cut(path: Path, start: str, end: str | None) -> tuple[int, int, str]:
    lines = path.read_text(encoding="utf-8", errors="strict").replace("\r\n", "\n").split("\n")
    s = next((i for i, l in enumerate(lines) if re.search(start, l)), None)
    if s is None:
        raise SystemExit(f"{path}: start pattern {start!r} not found")
    e = len(lines)
    if end:
        e = next((i for i in range(s + 1, len(lines)) if re.search(end, lines[i])), None)
        if e is None:
            raise SystemExit(f"{path}: end pattern {end!r} not found after line {s + 1}")
    while e > s and not lines[e - 1].strip():
        e -= 1
    return s + 1, e, "\n".join(lines[s:e])


def build() -> str:
    out = ["# Exact texts quoted in the counsel brief", "",
           "Generated by `scripts/build_legal_exact_texts.py`; do not edit by hand (`tests/test_legal_documents.py` regenerates",
           "this file and fails on any difference). Each block is cut from the file named in its header, with the file's",
           "sha256 and the line range. The blocks reproduce texts; they are not interpreted here, and nothing in this file",
           "says that any component or distribution is cleared, compliant or permitted.", ""]
    for sid, title, rel, start, end in SPECS:
        p = ROOT / rel
        a, b, body = cut(p, start, end)
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        out += [f"## {sid}: {title}", "", f"Source: `{rel}` lines {a} to {b}; file sha256 `{h}`.", "", "~~~text", body, "~~~", ""]
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    text = build()
    if a.check:
        ok = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("EXACT_TEXTS.md is current" if ok else "EXACT_TEXTS.md differs from the sources: run scripts/build_legal_exact_texts.py")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text)} bytes, {len(SPECS)} blocks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
