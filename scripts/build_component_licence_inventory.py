#!/usr/bin/env python3
"""Generate docs/legal/COMPONENT_LICENCE_INVENTORY.md: which licence category each part of this repository falls into.

The table below is the single source. Every tracked file must match exactly one rule (the longest path prefix wins), so a
new directory fails tests/test_component_licence_inventory.py until somebody classifies it. The output carries no counts
and no git facts, so it changes only when this table changes.

  python3 scripts/build_component_licence_inventory.py           write the document
  python3 scripts/build_component_licence_inventory.py --check   exit 1 if the committed file differs
  python3 scripts/build_component_licence_inventory.py --unclassified   list tracked files that no rule matches

The inventory states the owner's intended category per path and flags what cannot be covered automatically. It does not
decide any legal question, and nothing in it says that a component or a distribution is cleared. Standard library only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "legal" / "COMPONENT_LICENCE_INVENTORY.md"

CORE = "C"      # ReDevOps core platform: AGPL-3.0-or-later with the Commons Clause (source-available)
APPS = "A"      # applications under apps/: AGPL-3.0-only
THIRD = "T"     # third-party text or data kept under its own licence
NONE = "N"      # not a licensed component (generated or evidence data, not shipped as a component)

CATEGORY = {
    CORE: "1 Core (AGPL-3.0-or-later + Commons Clause)",
    APPS: "2 Apps (AGPL-3.0-only)",
    THIRD: "3 Third-party (own licence)",
    NONE: "- Not a licensed component",
}

# (path prefix, category, what it is, flag ids that apply)
RULES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("agentic_os/", CORE, "the agentic-os control plane, Projects API and sidecar sources (Python package `agentic_os`)", ("F1", "F2")),
    ("apps/", APPS, "the 21 applications (`apps/<name>/`, each with its own `LICENSE`) and their shared test files", ("F1", "F2", "F5", "F6", "F9")),
    ("deploy/launcher/", CORE, "the `redevops-launcher` Tauri shell (the desktop installers contain it)", ("F2",)),
    ("deploy/desktop-shell-main/", CORE, "the desktop-shell sources of the main line", ("F2",)),
    ("deploy/assets/", CORE, "repository images", ("F8",)),
    ("deploy/apps/", CORE, "the deployment of the applications' third-party cores (compose files, theme for a third-party core)", ("F5",)),
    ("deploy/", CORE, "deployment files, demo services and reference system of the platform", ("F5",)),
    ("integrations/voice-pipecat/", CORE, "optional voice-agent integration; carries a byte copy of the family licence files", ("F7",)),
    ("integrations/", CORE, "integrations", ()),
    ("packaging/notices/texts/", THIRD, "third-party licence texts, kept verbatim as collected", ("F3",)),
    ("packaging/ui-artifacts/", THIRD, "the pinned Projects UI bundle (built output of the separate `redevops-projects` repository, with React, ReactDOM and scheduler, MIT)", ("F4",)),
    ("packaging/", CORE, "build, signing, notice and gate tooling and the release records", ("F2",)),
    ("scripts/", CORE, "repository scripts", ("F2",)),
    ("snap/", CORE, "Snap packaging files (the strict snap is excluded from the first 0.6.0 distribution)", ()),
    ("docs/legal/sources/", THIRD, "excerpts of third-party agreements and licence texts quoted for counsel (verbatim, with their sources)", ("F3",)),
    ("docs/", CORE, "product documentation", ("F2",)),
    ("tests/fixtures/", NONE, "test fixtures (copies of licence files and generated listings used as test input)", ()),
    ("tests/", CORE, "the test suite", ("F2",)),
    ("conformance/", CORE, "conformance material", ()),
    ("audit/", CORE, "audit records", ()),
    ("examples/", CORE, "examples", ()),
    (".github/", CORE, "CI workflows", ()),
    ("uv.lock", THIRD, "dependency lock file: lists third-party packages, each under its own licence", ("F3",)),
    ("LICENSE", THIRD, "the verbatim text of the GNU Affero General Public License, version 3 (FSF text; unchanged)", ()),
    ("LICENSE-COMMONS-CLAUSE", THIRD, "the verbatim text of the Commons Clause License Condition v1.0 plus its three fill-in lines (unchanged)", ()),
    ("", CORE, "root files of the repository (README, docs, build and project configuration, release notes)", ("F2",)),
]

FLAGS = [
    ("F1", "Authorship by an AI agent",
     "Four commits of 2026-11-04 (the local-capability gate for the loopback API and `docs/LOCAL_API_SECURITY.md`) have the author "
     "`Claude (security agent) <noreply@anthropic.com>`. `LICENSE.md` (\"Contribution history\") states that all authoring commits are "
     "by RedevOps. Whether code produced with an AI agent under the owner's direction is covered by the owner's copyright, and whether "
     "that sentence stays accurate as written, is for counsel (N4). Not decided here."),
    ("F2", "Identity mapping of authors",
     "Commits carry three author identities of the owner (two Alex Mats addresses and the `arybach` GitHub account, mostly through the merge "
     "button). `LICENSE.md` says these are one rightsholder; that mapping is the owner's statement and is not independently evidenced in "
     "the repository. No outside contribution has been merged (see `CONTRIBUTING.md`). Counsel question N4."),
    ("F3", "Third-party text under its own licence",
     "These paths hold other parties' texts or list other parties' packages. ReDevOps licensing does not apply to them and cannot be "
     "applied to them; they are collected, not relicensed. Their notices are preserved in `THIRD_PARTY_NOTICES.txt`."),
    ("F4", "Separate repository, licence statement not re-read here",
     "The Projects UI bundle is built from the separate `redevops-projects` repository. The pinned source commit is a local, unpublished "
     "commit (`packaging/ui-bundle-source.json`), and the repository's own licence statement was not re-read for this inventory "
     "(D-LIC-2). The core statement lists the Projects UI as a core component on the owner's decision; the bundle also contains React, "
     "ReactDOM and scheduler (MIT), which stay MIT."),
    ("F5", "Applications wrap third-party cores",
     "Several applications are an agent layer over a third-party self-hosted core (for example Lago, Chatwoot, Umami, Listmonk, "
     "changedetection.io, Metabase). The app code is category 2; the wrapped core, its images and its data stay under the core's own licence "
     "and are not relicensed by anything in this repository."),
    ("F6", "Apps import the Commons-Clause core",
     "About 190 files under `apps/` import `agentic_os`. Whether AGPL-3.0-only applications may depend on a Commons-Clause core as intended, "
     "and whether an AGPL section 7 additional permission is needed, is counsel question N1 / D-LIC-3. This inventory states the "
     "intended category only."),
    ("F7", "Third-party dependency of an integration",
     "`integrations/voice-pipecat` depends on `pipecat-ai` (third-party, BSD-2-Clause as published) which keeps its own licence. The "
     "integration's package metadata uses a free-text licence string, not the `LicenseRef-` identifier of the core."),
    ("F8", "Authorship of images and icons not recorded",
     "The repository images, launcher icons and the `apps/social-autopilot/reel/assets/hero.jpg` file have no authorship record in the "
     "repository. D-LIC-8 and D-LIC-11 ask the owner to confirm first-party authorship; until confirmed they cannot be covered "
     "automatically."),
    ("F9", "Apps that carry a third-party-derived name or assets",
     "An app directory may hold data files, samples or text taken from a third-party project. None was found by the mechanical scan "
     "(no vendored directories, no minified third-party scripts, no copyright notice of another party in tracked source), but the scan "
     "is not an audit; a licence scan of each app is part of the counsel review (N1)."),
]

SIBLINGS = [
    ("runtime-contracts", "v0.3.17", "family licence (AGPL-3.0-or-later with the Commons Clause) per its own `LICENSE.md`; its `mission-sdk` is Apache-2.0 on purpose", "D-LIC-3"),
    ("discovery-runtime", "v0.1.21", "licence status not decided here", "D-LIC-3"),
    ("redevops-connectors", "v0.1.0", "AGPL-3.0-or-later declared in its pyproject at the pinned tag; the tag has no licence file", "D-LIC-2"),
    ("knowledge-frontier", "v0.2.0", "AGPL-3.0-or-later (plain), licence file marked \"LICENSE INTENT INFERRED - owner to confirm\"; not in the sidecar", "D-LIC-2, D-LIC-14"),
]


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def classify(path: str):
    best = None
    for rule in RULES:
        prefix = rule[0]
        if prefix == "":
            continue
        hit = path == prefix or (prefix.endswith("/") and path.startswith(prefix))
        if hit and (best is None or len(prefix) > len(best[0])):
            best = rule
    if best is None and "/" not in path:
        best = next(r for r in RULES if r[0] == "")
    return best


def unclassified() -> list[str]:
    return [p for p in tracked() if classify(p) is None]


def render() -> str:
    L = []
    w = L.append
    w("# Component-to-licence inventory\n")
    w("Generated by `scripts/build_component_licence_inventory.py` from the table in that script. Do not edit by hand; "
      "`tests/test_component_licence_inventory.py` regenerates it and fails if this file differs.\n")
    w("This inventory states the **intended category** of each part of the repository, from the owner's licensing decision of "
      "2026-11-10, and flags what cannot be covered automatically. It is not legal advice, it decides no legal question, and nothing in it "
      "says that any component or distribution is cleared. The unresolved question whether the Commons Clause can stand beside "
      "AGPL-3.0 (sections 7 and 10) is for counsel (D-LIC-3); this document does not answer it.\n")
    w("Categories: **1 Core** = GNU AGPL-3.0-or-later with the Commons Clause (source-available, not OSI open source). "
      "**2 Apps** = GNU AGPL-3.0-only, without the Commons Clause. **3 Third-party** = the third party's own licence, "
      "unchanged. **-** = not a licensed component.\n")
    w("## 1. Paths\n")
    w("The longest matching path prefix decides. The last row covers root files of the repository.\n")
    w("| Path | Category | What it is | Flags |")
    w("|---|---|---|---|")
    for prefix, cat, what, flags in sorted(RULES, key=lambda r: (r[0] == "", r[0])):
        shown = f"`{prefix}`" if prefix else "(root files)"
        w(f"| {shown} | {CATEGORY[cat]} | {what} | {', '.join(flags) or '-'} |")
    w("\nThe exact files of `apps/` are 21 directories (including `_suite-server`), each with an `apps/<name>/LICENSE` that is byte-identical "
      "to the root `LICENSE` (the GNU AGPL version 3 text, which does not itself limit the version; the \"3.0 only\" is declared in "
      "`LICENSE.md` and `LICENSING.md`).\n")
    w("## 2. Components outside this repository that the artifacts contain\n")
    w("These are separate repositories pinned in `packaging/git-pins.json`. Each declares its own licence; this repository's statement "
      "does not replace theirs, and a licence statement here cannot make a missing one in their repository exist.\n")
    w("| Package | Pinned | Licence as declared in its own repository | Decision |")
    w("|---|---|---|---|")
    for name, ver, lic, dec in SIBLINGS:
        w(f"| `{name}` | {ver} | {lic} | {dec} |")
    w("\nThe Projects UI bundle is a fifth such component (flag F4). Third-party Python wheels, Rust crates, CPython, OpenSSL, "
      "PyInstaller, the Microsoft runtime files and installer stubs are listed with their texts in `THIRD_PARTY_NOTICES.txt` "
      "(`packaging/notices/components.json`); they are category 3 and out of the schedule of the core.\n")
    w("## 3. Flags: what cannot be covered automatically\n")
    for fid, title, text in FLAGS:
        w(f"**{fid}. {title}.** {text}\n")
    w("## 4. What this inventory does not do\n")
    w("* It does not relicense, retag or reclassify any third-party or mixed-ownership material.\n"
      "* It does not add per-file SPDX headers; the app-level `LICENSE` files and the statements in `LICENSING.md` carry the terms for this release.\n"
      "* It does not settle the AGPL section 7 / section 10 and Commons Clause question (D-LIC-3), the relationship of the AGPL-3.0-only "
      "apps to the core they import (N1), the scope of \"Software\" in the Commons Clause (N2), the contributor mechanism (N3) or the "
      "copyright evidence (N4).")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--unclassified", action="store_true")
    a = ap.parse_args(argv)
    if a.unclassified:
        for p in unclassified():
            print(p)
        return 0
    text = render()
    if a.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is not current; run scripts/build_component_licence_inventory.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
