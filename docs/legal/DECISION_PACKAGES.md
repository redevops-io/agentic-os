# Decision packages: what must be decided before any R0-prime distribution

Prepared 2026-11-04 by agent `lic2` on branch `r0prime/lic2`. **This file decides nothing, clears nothing and is not legal advice.**
It collects, for every decision that blocks distribution, the exact question, the evidence, the options with their consequences,
a recommended default, who decides and what ships meanwhile. The machine-readable list is `packaging/distribution-gate.json`
(every entry OPEN); the written record the owner signs is `docs/legal/OWNER_DECISION_FORM.md`; the questions for counsel are
numbered Q1 to Q32 in `docs/legal/COUNSEL_BRIEF.md`; the exact wording of the licence texts is in `docs/legal/EXACT_TEXTS.md`
(cut mechanically from the files; section numbers below refer to it); the third-party excerpts are in `docs/legal/sources/`.

Rules this file follows. No SBOM entry and no inferred licence is treated as permission to redistribute (owner decision 7 of
2026-11-04). Verbatim quotations are in curly double quotation marks and are checked against the sources by `tests/test_legal_documents.py`.
Evidence levels: OBSERVED (seen in a file, command, fetch or run in this work), STATIC (read from a text or source), INFERRED,
UNVERIFIED. Dates are 2026-11-04 unless stated.

**What ships meanwhile, for every item below:** only the PRIVATE engineering candidate (owner decision of 2026-11-04: not signed, not
distributed, not published). A build that sets `REDEVOPS_DISTRIBUTION_BUILD=1` is refused by `packaging/check-distribution-gate.py`
while any applicable blocker is OPEN. Nothing here authorizes a push, tag, publication or signature.

## Status table

| Id | Subject | Decides | Platform | Blocks | Status | Facts resolved by lic2 | Left open |
|---|---|---|---|---|---|---|---|
| D-LIC-1 | Product licence wording | owner + counsel | all | yes | OPEN | exact texts compiled | wording; who "RedevOps" is |
| D-LIC-2 | Inferred licences of connectors / knowledge-frontier | owner | all | yes | OPEN | tags, trees, commit messages, cost of exclusion (14 -> 6 cards) | the owner's written licence statement and the new tags |
| D-LIC-3 | Commons Clause with other licences in one binary | counsel | all | yes | OPEN | AGPL 7 / 10 and Commons Clause wording, licences present | counsel's opinion |
| D-LIC-4 | MPL-2.0: certifi and five crates | counsel | all | yes | OPEN | MPL 3.2 wording, certifi source location and hashes, the five crates | counsel's wording |
| D-LIC-5 | AGPL 13 for the loopback UI | counsel | all | yes | OPEN | section 13 wording, how the sidecar is reached | interpretation |
| D-LIC-6 | CPython, OpenSSL, PyInstaller, linked libraries | owner + counsel | all | yes | OPEN | texts for PSF, Apache 4, Bootloader Exception, libyaml, libffi, DuckDB tree, bzip2, Tcl/Tk, expat | whether the texts suffice; Windows interpreter contents |
| D-LIC-7 | React and other UI JavaScript | owner | all | no | OPEN | only React banners exist in the bundle | npm lock of the UI build |
| D-LIC-8 | hero.jpg placeholder provenance | owner | all | yes | OPEN | placeholder drawn by code | owner's provenance statement |
| D-LIC-9 | NSIS, WiX, WebView2 | owner + counsel | windows | yes | OPEN | NSIS and WiX texts, bundler versions | counsel reading; no installer exists |
| D-LIC-10 | Acceptance pages, Appropriate Legal Notices | owner + counsel | all | yes | OPEN | sections 0, 5(d), 9 compiled; UI shows no notice | decision |
| D-LIC-11 | Launcher icons | owner | all | yes | OPEN | none (authorship not discoverable) | owner fact |
| D-LIC-12 | Source availability (AGPL 6) | owner + counsel | all | yes | OPEN | section 6 and 1 compiled; app and dmg carry no source or offer | choice of 6(a)-(e), wording |
| D-LIC-13 | Components with no licence file | owner | all | yes | OPEN | r-efi, objc2, selectors, alloc-stdlib, webview2-com* resolved by fact (table below) | acceptance of the table; connectors (D-LIC-2) |
| D-LIC-14 | knowledge-frontier excluded | owner | all | no | OPEN | unchanged | confirm |
| D-LIC-15 | "open source" in shipped outreach template | owner | all | yes | OPEN | none | wording |
| D-LIC-16 | QA bundle hand-off | owner + counsel | qa | yes (QA) | OPEN | see `QA_BUNDLE_HANDOFF.md` | decisions in redevops-qa |
| D-LIC-17 | Shipped-but-unexercised components | owner | all | no | OPEN | setuptools, packaging, _distutils_hack excluded and proven (commit 66ae496); duckdb kept | confirm |
| D-LIC-18 | objc2 crates and Apple SDKs | owner + counsel | macos | yes | OPEN | crates' statements quoted; standard texts found; Xcode agreement excerpted | counsel reading |
| pins-03 | Commons Clause and the content of a source offer | counsel | all | yes | OPEN | texts compiled | wording |
| pins-04 | The exact commit must be public first | owner | all | yes | OPEN | public repository has no R0-prime commit | publication (needs push authorization) |
| pins-08 | Microsoft redistributable DLLs | counsel | windows | yes | OPEN | two Microsoft regimes quoted | counsel; UCRT omission untested |

Items that block nothing but need the owner: D-LIC-7, D-LIC-14, D-LIC-17. Total blockers: 18 (15 for macOS, 16 for Windows).

---

<a id="d-lic-1"></a>
## D-LIC-1 Product licence wording, copyright line, installer acceptance

**Question.** Is “source-available (family licence: AGPL-3.0-or-later with the Commons Clause)”, SPDX `LicenseRef-AGPL-3.0-or-later-with-Commons-Clause` and the copyright line “Copyright (c) 2025-2026 RedevOps.” the statement the distributed packages carry, and is RedevOps the right legal person for the copyright line and as the Commons Clause "Licensor"?

**Evidence.**
* `LICENSING.md` section 1 holds the statement; `check_licence_statements.py` fails if `pyproject.toml`, the launcher `Cargo.toml`, `tauri.conf.json`, `README.md`, `modules.yaml` or `stack_knowledge.py` disagree (STATIC, tested). The R0 build carried four disagreeing statements (register D-LIC-1).
* The Commons Clause file names “Software:  agentic-os” and “Licensor:  RedevOps” (EXACT_TEXTS `commons-clause`). `LICENSE.md` says the repository relicensed on 2026-08-08 and that releases up to `v0.1.0` “were distributed under **AGPL-3.0-or-later alone**” (EXACT_TEXTS `licence-md-effective`).
* `LICENSE.md` on contribution history: “All authoring commits in this repository are by RedevOps (Alex Mats, under two git identities).” (EXACT_TEXTS `licence-md-contrib`). Whether "RedevOps" is a legal person, and which, is not stated in any file of the repository (UNVERIFIED; the owner knows).
* The packages are named “Agentic Apps”; the Commons Clause names only “agentic-os” as the Software. Whether the Tauri launcher crate and the installers are part of the "Software" so named is a counsel question (Q1).

**Options.** (a) Keep the short statement and the line as written. Consequence: nothing changes; counsel may still ask for more. (b) A longer statement and/or a different holder line drafted by counsel. Consequence: five package fields and one test fixture change; the notices are regenerated. (c) Change the licence the packages carry. Consequence: the whole family arrangement (D-LIC-3) is re-opened.

**Recommended default.** (a) for the private candidate; counsel confirms (a) or drafts (b) before any distribution. **Decides:** owner, with counsel for the final wording. **Counsel:** Q1, Q2, Q3.

---

<a id="d-lic-2"></a>
## D-LIC-2 Inferred licences of redevops-connectors and knowledge-frontier

**Question.** What licence does the owner state, in writing, for the redevops-connectors bytes at tag `v0.1.0` (the R0-prime pin) and for knowledge-frontier, and how is that statement attached to the distributed source?

**Evidence** (OBSERVED, anonymous clones 2026-11-04; verbatim in `docs/legal/sources/sibling-repositories-facts.txt`; both repositories are public, GitHub reports licence key `agpl-3.0` for both).
* redevops-connectors: the only tag is `v0.1.0` (commit `7fbb9ec`, tag object `f36bdf5`); its tree holds no LICENSE file; `pyproject.toml` line 7 says `license = "AGPL-3.0-or-later"` (plain). Its README line 3 is a badge “License: AGPL-3.0 + Commons Clause” and line 148 says “Licensed AGPL-3.0-or-later.” (the two disagree at the tag).
* The licence files exist only on `main`, added by commit `d990280` (2026-10-13) whose message begins “chore(license): add AGPL-3.0-or-later + Commons Clause (LICENSE INTENT INFERRED - owner to confirm)” and says “Inferred from the badge, not owner-stated.” Commit `c5fbe82` (2026-10-15, “Docs wording cleanup”) edited `LICENSE.md` afterwards. `main` is 15 commits ahead of the pin.
* The `LICENSE.md` of `main` says: “Earlier versions of this repository were not covered by this file and are not affected retroactively by it.” So by that file's own words the pinned bytes are not covered by the family licence it describes (Q7).
* knowledge-frontier: tags `v0.1.0` (commit `d6a78ca`, the pin) and `v0.2.0` (commit `057258a`, 2026-09-23; new since the earlier review); neither tree holds a LICENSE file; `pyproject.toml` says `AGPL-3.0-or-later`; the LICENSE on `master` was added by `d32a82a`: “plain AGPL-3.0-or-later was inferred (no Commons Clause).” It is excluded from the frozen sidecar (D-LIC-14) and so is not part of the shipped bytes; it is still built into the wheelhouse (pins-13).
* Cost of leaving connectors out (OBSERVED, `tests/test_connectors_removal_cost.py`, the Projects API served with `redevops_connectors` imports blocked): `/api/projects/{id}/apps` returns **6** sample providers instead of **14** provider cards (slack, klaviyo, ayrshare, blotato, postiz, gmail, google_calendar, whatsapp_business, whatsapp_waha, web, hubspot, stripe, polar, apollo); the cards that carry setup steps (14 -> 0), setup URLs (14 -> 0), credential field definitions (14 -> 0) and required scopes (7 -> 0) disappear; every other route (`/`, `/api/projects`, overview, missions, `/hero.jpg`) still answers 200. The live provider code paths of `agentic_os/integrations/live.py`, `connect.py` and `hosted_oauth.py` import the package lazily and are unavailable without it (STATIC).

**Options and consequences.**
* (a) The owner states the licence intent in writing; the maintainers add the licence files in a new tagged release of each repository; R0-prime pins the new tag. Needs new tags in two repositories (an owner action; nothing is pushed by any agent). Consequences: the pin in `packaging/git-pins.json`, `pyproject.toml` extras, the wheelhouse, the SBOM, the notices, the source offer and the candidate all change, so a new candidate is built; neither package is a benchmarked component (runtime-contracts 0.3.4 stays), but the connectors code between v0.1.0 and `main` (15 commits, including a URL policy for `web.fetch` and a Gmail change) would enter the product and needs its own review and tests.
* (a2) The owner states the intent in writing and it is recorded in the notices only. Consequence: the pinned tag still has no licence file, so the Corresponding Source offered for the shipped bytes has none either; whether that is acceptable is counsel's question (Q7).
* (b) Exclude connectors from the distribution. Consequence: the measured loss above. Not recommended unless (a) is refused.
* (c) Ship private-only until (a). Consequence: none for the private candidate; no distribution.

**Recommended default.** (a) before any distribution; (c) meanwhile. **Decides:** owner (counsel on Q7, Q8). **Ships meanwhile:** the private candidate with connectors at `v0.1.0`, listed in `NOTICES_MISSING.md` as the only component without a licence text; knowledge-frontier not shipped.

---

<a id="d-lic-3"></a>
## D-LIC-3 Commons Clause term in one binary with plain-AGPL, permissive and other-licence code

**Question.** May the Commons Clause condition stay in the family code of a binary that also contains code under other licences, in light of AGPL sections 7 and 10?

**Evidence (STATIC).** AGPL section 7: “All other non-permissive additional terms are considered "further restrictions" within the meaning of section 10. If the Program as you received it, or any part of it, contains a notice stating that it is governed by this License along with a term that is a further restriction, you may remove that term.” Section 10: “You may not impose any further restrictions on the exercise of the rights granted or affirmed under this License.” The Commons Clause: “Any license notice or attribution required by the License must also include this Commons Clause License Condition notice.” `LICENSE.md`: “Anything importing these packages is a work based on them and takes on both the AGPL's network obligation and the Commons Clause's restriction on selling.” (EXACT_TEXTS `agpl-7`, `agpl-10`, `commons-clause`, `licence-md-downstream`.)

Licences present in the shipped binaries (from the 405-component notices of the final2 candidate): family licence (agentic-os, runtime-contracts); redevops-connectors, declared plain AGPL-3.0-or-later at its pin (D-LIC-2); MIT, Apache-2.0 and their dual forms (the large majority of the 400+ crates and wheels); BSD-3-Clause; Unicode-3.0 (37 crates); Zlib; Unlicense; MPL-2.0 (certifi and five crates, D-LIC-4); PSF-2.0 (CPython, typing_extensions); GPL-2.0-or-later with the Bootloader Exception (PyInstaller bootloader; D-LIC-6); "Apache-2.0 OR GPL-2.0-only" (self_cell); "MIT OR Apache-2.0 OR LGPL-2.1-or-later" (r-efi). No GPL or LGPL code is elected for linking (matrix `LIC-29` and register).

**Options.** (a) Counsel opinion that the arrangement is sound as built. (b) Remove any plain-AGPL component from the distribution (done for knowledge-frontier; for connectors only if its licence stays plain AGPL, in which case D-LIC-2 (b)). (c) Relicense the siblings consistently or drop the Commons Clause for the packages (a business decision with consequences for every earlier statement: `LICENSE.md`, README, modules.yaml, SPDX identifier). (d) No distribution until counsel has opined.

**Recommended default.** (d). **Decides:** counsel and owner. **Counsel:** Q4, Q5, Q6.

---

<a id="d-lic-4"></a>
## D-LIC-4 MPL-2.0 components: certifi's cacert.pem and five statically linked crates

**Question.** What must accompany distribution of these components to meet MPL-2.0 section 3.2, and is a source-location statement in the Licenses folder sufficient?

**Evidence.**
* certifi 2026.10.19 (the deliberate deviation of pins-16): its wheel's `LICENSE` is only the Mozilla CA bundle header, 989 bytes (EXACT_TEXTS `certifi-licence`): “This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.” The full MPL-2.0 text is therefore shipped from `texts/MPL-2.0.txt`.
* `cacert.pem` sha256 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785` is identical in the wheel, in the embedded PyInstaller archive (final2 evidence) and at upstream tag `2026.10.19` of github.com/certifi/python-certifi (commit `39e296825d323aa1c8e998ffd9cc57fe22514a24`, fetched raw 2026-11-04) (OBSERVED). The PyPI sdist `certifi-2026.10.19.tar.gz` has sha256 `035c1e85572b141bf1c3a3eff0e33a8eb74917522f2559075ff8d2fea780a1ae` and is already named in the source-offer inputs (`packaging/source-offer/third-party-sources.json`); that offer is NOT inside the .app or the dmg (OBSERVED: 0 listings in the final2 candidate).
* MPL-2.0 text (EXACT_TEXTS `mpl-1-6b`, `mpl-1-13`, `mpl-3`): section 3.2 on Executable Form, 3.3 Larger Work, 3.4 Notices.
* Five further MPL-2.0 crates are statically linked into the macOS launcher: `cssparser` 0.36.0, `cssparser-macros` 0.6.1, `dtoa-short` 0.3.5, `option-ext` 0.2.0, `selectors` 0.36.1 (OBSERVED in the notices report). Four carry their own LICENSE file. `selectors` carries none, and neither does its upstream repository (servo/stylo at the packaged commit `635e1a19d029`: root tree listing and `LICENSE`/`COPYING` fetches return 404, OBSERVED); its source files carry the MPL notice header (`docs/legal/sources/selectors-0.36.1-lib.rs-header.txt`: “This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.” in a comment; the MPL text prints the same notice as Exhibit A, EXACT_TEXTS `selectors-mpl-header`). The generic MPL text is shown for it, marked UNVERIFIED.

**Options.** (a) Ship the MPL text (done) plus a sentence stating where the Source Code Form of each MPL component is (certifi: sdist URL and sha256 above; crates: crates.io package versions with their checksums, which the SBOM records), worded by counsel. (b) Replace certifi by the OS trust store: a behaviour change for every TLS client in the sidecar, not tested; also reverses owner decision 6. (c) No distribution until (a) is worded.

**Recommended default.** (a). **Decides:** counsel. **Counsel:** Q9, Q10. **Ships meanwhile:** MPL-2.0 text and each component's own file; no source statement.

---

<a id="d-lic-5"></a>
## D-LIC-5 AGPL section 13 for a loopback-served UI

**Question.** Does section 13 apply to the Projects UI that the sidecar serves on `127.0.0.1` of the same machine, to a mounted or tunnelled deployment, or not to unmodified builds? **This file does not conclude.**

**Evidence (STATIC and OBSERVED).** Section 13: “if you modify the Program, your modified version must prominently offer all users interacting with it remotely through a computer network (if your version supports such interaction) an opportunity to receive the Corresponding Source of your version” (EXACT_TEXTS `agpl-13`). The sidecar serves the UI and API on a loopback address (`packaging/sidecar_main.py`: `127.0.0.1:8787` by default; the launcher and the lifecycle tests let the sidecar pick the port, `PROJECTS_PORT=0`); the launcher opens the user's default browser on the one-time URL; access needs a session the launch code buys (header session design, owner decision 5). A deployment can mount the app under a path behind a tunnel (`mounted_app`, `PROJECTS_BASE_PATH`) and a Docker image exists; those are not the packaged desktop use. The UI shows no licence or source statement (OBSERVED).

**Options.** (a) No statement. (b) An About/Licence item in the UI linking the source location (needs a change in the vendored UI bundle, built from the separate `redevops-projects` repository, or a sidecar-served page; also answers D-LIC-10 (c)). (c) Counsel opinion first.

**Recommended default.** (c), then (b) if counsel wants it. **Decides:** counsel. **Counsel:** Q11, Q12.

---

<a id="d-lic-6"></a>
## D-LIC-6 CPython, OpenSSL, PyInstaller and statically linked libraries

**Question.** Are the notice texts now in `THIRD_PARTY_NOTICES.txt` sufficient, given that what is linked cannot be read from the binaries?

**Resolved by fact (this round).**
* CPython: PSF License Version 2 as printed in the 3.12.10 LICENSE, section 2 requires “PSF's License Agreement and PSF's notice of copyright” to be retained “in Python alone or in any derivative version” (EXACT_TEXTS `psf-2`); the LICENSE and the acknowledgements page (Doc/license.rst) are printed in the notices. CPython v3.12.10 `PCbuild/get_externals.bat` pins, for the Windows build, libffi 3.4.4, OpenSSL 3.0.16, bzip2 1.0.8, sqlite 3.49.1.0, tcl/tk 8.6.15, xz 5.2.5, zlib 1.3.1 (STATIC; the interpreter of the future Windows build may differ: pins-06).
* The local macOS candidate's interpreter (python-build-standalone via uv): `strings` on its `libpython3.12.dylib` shows the banners `OpenSSL 3.0.16 11 Feb 2025`, `expat_2.6.3`, `1.0.8, 13-Jul-2019` (bzip2), Tcl/Tk `8.6.14` and libmpdec strings (OBSERVED), i.e. Tcl/Tk is statically inside the library although tkinter is excluded from the sidecar archive. The texts of bzip2 1.0.8, Tcl/Tk 8.6.14 and expat 2.6.3 are now in `texts/` and printed under REFERENCE NOTICES. The CI interpreter (python.org build, pins-06) was not examined (UNVERIFIED).
* OpenSSL 3.0.16 (shared libraries on the CI interpreter, static in the local one) and 4.0.2 (static in `cryptography`): the project's LICENSE.txt is the Apache License 2.0 and is byte-identical at both tags. There is no `NOTICE`, `NOTICE.md` or `NOTICE.txt` file at either tag (raw fetch returns 404, OBSERVED), so section 4(d) of the Apache text has nothing to reproduce from the project (EXACT_TEXTS `apache-4`). Whether the linked bytes equal those tags is UNVERIFIED.
* PyInstaller 6.22.3: COPYING.txt is GPL-2.0-or-later with the Bootloader Exception (EXACT_TEXTS `pyinstaller-exception`); runtime hooks Apache-2.0; printed in the notices.
* libyaml 0.2.5 (PyYAML 6.0.3's CI builds against ref 0.2.5; its LICENSE is MIT), libffi 3.4.4 (MIT), and the DuckDB source tree's root and `third_party/*` licence files at tag v1.5.5 (29 files plus tdigest NOTICES) are now printed under REFERENCE NOTICES; which DuckDB third-party libraries are linked in the wheel is UNVERIFIED.
* MIT, BSD-3-Clause and Unicode-3.0 conditions are quoted from shipped samples (EXACT_TEXTS `react-mit`, `bsd3-sample`, `unicode-3-sample`): each requires the notice to appear with copies, or, for BSD-3-Clause in binary form, to be reproduced “in the documentation and/or other materials provided with the distribution”. `THIRD_PARTY_NOTICES.txt` sits in `Contents/Resources/Licenses/` and is embedded in the sidecar; it is NOT at the dmg root (owner decision O-03 of the signing request; D-LIC-10).

**Options.** (a) Accept the collected texts and record the UNVERIFIED items. (b) Scan the frozen archive and extension binaries to settle what is linked (engineering; extends the SBOM; for the CI interpreter a build is needed). (c) No distribution until (b).

**Recommended default.** (a), with counsel's answers to Q19 to Q22, Q24; (b) is optional hardening. **Decides:** owner (texts), counsel (sufficiency). **Ships meanwhile:** the notices as generated.

---

<a id="d-lic-7"></a>
## D-LIC-7 React and other JavaScript in the UI bundle (non-blocking)

**Evidence (OBSERVED).** `agentic_os/projects_ui/assets/index-76LJRJ18.js` (400961 bytes): the only `@license` and `Copyright` strings are four React banners (“Copyright (c) Facebook, Inc. and its affiliates.” x4); the MIT text of React 18.3.1 (identical bytes in react-dom and scheduler 0.23.2) is printed. The bundle is built from the LOCAL, UNPUBLISHED `redevops-projects` commit `ea9e53e56b4cbaa1afb22404a6468695e49c24fa` (recorded with the git blob id and sha256 of its `package-lock.json` in `packaging/ui-bundle-source.json`; the lock file itself is not in this repository), so other bundled packages that carry no banner cannot be listed (UNVERIFIED). **Options:** (a) accept React-only; (b) record the redevops-projects tag/commit and lock and generate the UI notices from it. **Recommended default:** (b). **Decides:** owner.

---

<a id="d-lic-8"></a>
## D-LIC-8 hero.jpg placeholder provenance

**Question.** May the neutral generated placeholder be treated as first-party artwork under the family licence?

**Evidence.** The original image carried C2PA metadata naming "Black Forest Labs API" and is no longer shipped. The replacement is drawn by `scripts/make_neutral_hero.py` (Pillow, gradient and shapes, no model output, no photograph, no metadata segments; `tests/test_hero_asset.py`). It was written and run by an AI coding agent on 2026-11-04. Whether that makes the output the owner's to license is a counsel point (Q28). **Options:** (a) confirm first-party artwork; (b) supply an image whose rights the owner holds; (c) remove the image. **Recommended default:** (a) for the candidate, (b) when an asset exists. **Decides:** owner.

---

<a id="d-lic-9"></a>
## D-LIC-9 NSIS, WiX and WebView2 notices (Windows installers)

**Question.** What notices must the Windows installers carry for installer-tool code that ends up inside them?

**Evidence.** `tauri-bundler` 2.10.4 (source read, STATIC; the installed tauri-cli is 2.11.4 whose bundler version was not read): downloads NSIS 3.13 and `nsis_tauri_utils` 0.5.3 (MIT OR Apache-2.0 per GitHub), defaults to LZMA compression (`SetCompressor /SOLID "lzma"`), downloads `wix314-binaries.zip` (WiX 3.14.1). NSIS COPYING: “All NSIS source code, plug-ins, documentation, examples, header files and graphics, with the exception of the compression modules and where otherwise noted, are licensed under the zlib/libpng license.” and “The LZMA compression module for NSIS is licensed under the Common Public License version 1.0.” (EXACT_TEXTS `nsis-applicable`); CPL 1.0 section 3 for object-code distribution, including “states that source code for the Program is available from such Contributor” (EXACT_TEXTS `nsis-cpl-3`). WiX 3: Microsoft Reciprocal License, section 3 (EXACT_TEXTS `wix-ms-rl-3`): “For any file you distribute that contains code from the software (in source code or binary format), you must provide recipients the source code to that file along with a copy of this license”. Both texts are now in `texts/` and attached to the Windows installer-stub component. Which NSIS or WiX files end up inside an installer is UNVERIFIED (no Windows installer exists). Microsoft Edge WebView2 is downloaded by the installer's default bootstrapper, not redistributed (derived from the Tauri schema; its terms were not read: UNVERIFIED).

**Options.** (a) Counsel reads these texts and states the notice set. (b) Build the NSIS installer with another compressor to avoid the CPL-1.0 module (an untested engineering change). (c) No Windows distribution. **Recommended default:** (a) before a Windows distribution. **Decides:** owner with counsel. **Counsel:** Q27.

---

<a id="d-lic-10"></a>
## D-LIC-10 Acceptance pages and Appropriate Legal Notices

**Evidence (STATIC, OBSERVED).** Section 5(d): “If the work has interactive user interfaces, each must display Appropriate Legal Notices”; section 0 defines the term as “a convenient and prominently visible feature that (1) displays an appropriate copyright notice, and (2) tells the user that there is no warranty for the work (except to the extent that warranties are provided), that licensees may convey the work under this License, and how to view a copy of this License.” Section 9: “You are not required to accept this License in order to receive or run a copy of the Program.” (EXACT_TEXTS `agpl-0`, `agpl-5`, `agpl-9`.) The UI shows no licence statement (OBSERVED). Tauri's `licenseFile` would add an acceptance step to every installer (tauri-bundler source). The dmg root carries no `Licenses/` folder (owner decision O-03 of the signing request). **Options:** (a) files only; (b) a notice file as the installer licenseFile; (c) an About/Licence entry in the launcher or UI. **Recommended default:** (c), confirmed by counsel (Q17, Q18). **Decides:** owner with counsel.

---

<a id="d-lic-11"></a>
## D-LIC-11 Launcher icons and the "Re" mark

Authorship and rights of `deploy/launcher/src-tauri/icons/*` are not recorded in any file (matrix NAT-AS-icons). **Options:** (a) the owner confirms first-party artwork under the family licence; (b) exclude the icons from the licence statement and record their terms. **Recommended default:** (a). **Decides:** owner (a fact only the owner has). **Counsel:** Q29. **Ships meanwhile (RC3 development):** TEMPORARY neutral artwork generated from code (`scripts/make_neutral_icons.py`, no letters, no third-party content); the previous "Re" mark is not retained while this decision is open. Status: unchanged, OPEN.

---

<a id="d-lic-12"></a>
## D-LIC-12 Source availability for the object code (AGPL section 6)

**Question.** Which of section 6(a) to (e) does the owner choose, and what does each artifact carry?

**Evidence.** Section 6 (EXACT_TEXTS `agpl-6`): object code may be conveyed “provided that you also convey the machine-readable Corresponding Source under the terms of this License, in one of these ways”; (a) “accompanied by the Corresponding Source fixed on a durable physical medium customarily used for software interchange”; (b) “accompanied by a written offer, valid for at least three years and valid for as long as you offer spare parts or customer support for that product model”; (c) for occasional noncommercial distribution only; (d) “offering access from a designated place”; (e) peer-to-peer. Section 1 defines “Corresponding Source” (EXACT_TEXTS `agpl-1`). OBSERVED: the final2 candidate's `.app` and dmg listings contain no source, no offer and no `SOURCE-OFFER.md` (0 matches); `LICENSING.md` names the repository `https://github.com/redevops-io/agentic-os` as a pointer only (register). The Corresponding Source of the packages also covers the sibling packages at their pins (runtime-contracts v0.3.4, redevops-connectors v0.1.0), the vendored UI (built from `redevops-projects`, whose lock is unrecorded: D-LIC-7), the build scripts and the pinned third-party sources named in `packaging/source-offer/third-party-sources.json`.

**Options.** (a) 6(a): an accompanying source archive, produced from the build inputs (engineering available; self-contained, no hosting dependency; larger download). (b) 6(b): a written offer (counsel words it; `packaging/SOURCE-OFFER.md` has a placeholder). (c) 6(d): network access naming repository, commit and tags (needs pins-04 first and the repositories to stay public: pins-01). (d) Counsel chooses.

**Recommended default.** (d); engineering can supply (a) on request. **Decides:** owner with counsel. **Counsel:** Q13, Q14, Q16.

---

<a id="d-lic-13"></a>
## D-LIC-13 Components whose package holds no licence file

**Question.** Does the owner accept this per-item evidence table as the notice set, with its UNVERIFIED notes?

| Component | What was found (source) | Level | Result in the notices |
|---|---|---|---|
| r-efi 5.2.0 (MIT OR Apache-2.0 OR LGPL-2.1-or-later) | no LICENSE file in the package nor at upstream tag v5.2.0 (commit `db87f6c8`); the package's `AUTHORS` file (3652 bytes, identical at the upstream commit) states “This project is triple-licensed under the MIT License, the Apache License, Version 2.0, and the GNU Lesser General Public License, Version 2.1+.” and reproduces the MIT permission notice and the Apache and LGPL notice headers with copyright lines of Red Hat, Microsoft and David Rheinsberg (EXACT_TEXTS `refi-authors`) | OBSERVED (crate sha256 `74765f6d...` verified against the crates.io index) | AUTHORS printed verbatim; the election among the three licences is counsel's (Q23) |
| objc2, block2, objc2-foundation, objc2-encode (MIT) and objc2-app-kit, -core-foundation, -core-graphics, -exception-helper, -io-surface, -web-kit, dispatch2 (Zlib OR Apache-2.0 OR MIT) | the packages hold no licence file; their READMEs link `../../LICENSE.md` (not in the package); the upstream repository at the packaged commits holds only the licence-selection statement `LICENSE.md`; the standard MIT, Zlib and Apache texts were added upstream on 2026-03-18 (commit `ee9a7ada`, message "Include license text in the repository"), later than the packaged crates | OBSERVED | the statement plus the later standard texts, marked UNVERIFIED (the 2026 copyright line was not checked against the packaged versions) |
| selectors 0.36.1 (MPL-2.0) | no licence file in the package nor in servo/stylo at the packaged commit; MPL notice in each source file header | OBSERVED | generic MPL text, marked UNVERIFIED; see D-LIC-4 |
| alloc-stdlib 0.2.4 (BSD-3-Clause) | upstream `LICENSE` at dropbox/rust-alloc-no-stdlib commit `ae42d220` (sha256 `c0c56f26...`) | OBSERVED | printed, marked UNVERIFIED (not the package's own file) |
| webview2-com 0.38.2, -macros 0.8.1, -sys 0.38.2 (Windows) | upstream `LICENSE` at wravery/webview2-rs commits `b74dc5e2`, `dffa41a8` (sha256 `0dcf4151...`) | OBSERVED (fetch run on these crates) | printed when the Windows notices are generated |
| libyaml, libffi, DuckDB third-party libraries | no texts in the wheels; texts of the named versions from upstream tags | OBSERVED (texts), UNVERIFIED (linked versions) | REFERENCE NOTICES section |
| redevops-connectors 0.1.0 | no licence file at the pin | OBSERVED | still missing: D-LIC-2 |

Re-run of the generator on the final2 candidate's SBOM (offline except the crate fetch; output kept in the work area): 406 components (the 405 of the candidate plus the first-party Licenses folder item), 399 with a text, 6 first-party or system items listed as such, **1 without a text: redevops-connectors**. Before this round the list was r-efi and connectors.

**Options.** (a) Accept the table and record the elections (r-efi, self_cell "Apache-2.0 OR GPL-2.0-only", unicode-ident "(MIT OR Apache-2.0) AND Unicode-DFS-2016"). (b) Ask upstream projects for licence files. (c) Replace or exclude: not possible for the Tauri/objc2 stack on macOS. **Recommended default:** (a). **Decides:** owner. **Counsel:** Q23, Q24.

---

<a id="d-lic-14"></a>
## D-LIC-14 knowledge-frontier excluded from the frozen sidecar (non-blocking)

Unchanged and proven earlier (blocked-import and real PyInstaller runs). knowledge-frontier now also has a tag `v0.2.0` (2026-09-23) with no licence file. **Options:** (a) keep excluded; (b) restore once D-LIC-2 is decided for it. **Recommended default:** (a). **Decides:** owner.

---

<a id="d-lic-15"></a>
## D-LIC-15 "open source" in shipped text

`agentic_os/world/outreach.py` ships an outreach template that says "open-source runtime stack"; `LICENSE.md` says the project is source-available, not open source. **Options:** (a) change the template to the family wording; (b) leave it. **Recommended default:** (a) (the owner's own wording rule). **Decides:** owner. **Counsel:** Q31.

---

<a id="d-lic-16"></a>
## D-LIC-16 QA bundle hand-off

Separate repository (`redevops-qa`); nothing is changed here. See `docs/legal/QA_BUNDLE_HANDOFF.md` (report.css from HTML Dog, tablesorter, the declared-only items, the plain-AGPL agent-harness). **Options:** (a) decide each item in redevops-qa; (b) do not distribute the QA bundle. **Recommended default:** (b) until (a). **Decides:** owner with counsel. **Counsel:** Q32.

---

<a id="d-lic-17"></a>
## D-LIC-17 Shipped-but-unexercised components (non-blocking)

**Engineering resolution (OBSERVED; commit `66ae496`; tests `tests/test_sidecar_build_only_excludes.py`).** `setuptools` (131 archive entries with vendored jaraco.* and backports), `packaging` (16), `_distutils_hack` (2), `pyi_rth_setuptools` and five standard-library modules only they pulled in (`pydoc`, `pydoc_data`, `rlcompleter`, `site`, `_sitebuiltins`) are in the frozen archive only because PyInstaller's own `pyi_rth_setuptools` runtime hook imports setuptools from the freeze venv. No first-party module and no shipped third-party module imports them (AST scan of `agentic_os`, `runtime_contracts`, `redevops_connectors` and of every shipped wheel: the only hits are `cffi/setuptools_ext.py` and `cffi/_shimmed_dist_utils.py`, which are build-time helpers, and PyInstaller's own hooks). The spec now excludes them. A baseline build and an excluded build of the same inputs (PyInstaller 6.22.3, CPython 3.12.10, the 21 locked wheels) differ by exactly 156 archive entries (nothing added); on both: the 63-check sidecar smoke test 63/63, locales 8/8 variants, lifecycle PASS, `bootstrap` output byte-identical; sidecar 36,753,776 -> 35,506,144 bytes. Side effects: the setuptools advisory of pins-17 no longer concerns a shipped component; setuptools, packaging and `_distutils_hack` no longer need notices.
Not changed: `pycparser` is in the wheel lock (cffi depends on it) but in no archive; nothing to exclude. `duckdb` is kept: `MISSION_EVENT_BACKEND=duckdb` is the documented single-node durable ledger (`agentic_os/mission/event_backends.py`), so removing it changes behaviour (the spec test pins this). `libedit`/`readline` is a system library, not redistributed.
**Options:** (a) accept; (b) also drop duckdb (a behaviour change: no durable single-node backend). **Recommended default:** (a). **Decides:** owner.

---

<a id="d-lic-18"></a>
## D-LIC-18 objc2 crates derived from Apple SDKs (macOS)

**Question.** Does distributing a macOS launcher that links these crates raise obligations under the Xcode and Apple SDKs Agreement?

**Evidence.** The crates' own statements (the upstream `LICENSE.md` at the packaged commits, printed in the notices with the UNVERIFIED note): “the crates are (usually automatically) derived from Apple SDKs, and that may have implications for licensing”; “These crates are derived from Apple SDKs shipped with Xcode.”; “From reading the license, it is unclear whether distributing derived works such as these crates are allowed?”; “Xcode is required to use the crates, and when using Xcode you have already agreed to the Xcode license”. The packages themselves carry only `license = "MIT"` or `"Zlib OR Apache-2.0 OR MIT"` in `Cargo.toml` and a README badge linking `../../LICENSE.md` (OBSERVED in the registry copies). The Xcode and Apple SDKs Agreement (excerpts in `docs/legal/sources/apple-xcode-agreement-excerpts.txt`): section 2.4 “macOS applications and libraries may be distributed without entering into a separate written agreement with Apple so long as such applications and libraries comply with the terms of this Agreement.”; section 2.7 “You agree not to rent, lease, lend, upload to or host on any website or server, sell, redistribute, or sublicense the Apple Software and Apple Services, in whole or in part”, and no permission to “create derivative works of the Apple Software or Services”; section 2.5 on copies. “Apple Software” is defined as “the Xcode Developer Tools and the Apple SDKs”. Whether the crates are "Apple Software" or derived from it, and whether the launcher binary contains such derived material, is not decided here (Q25).

**Options.** (a) Accept the upstream statement and the declared licences as accepted risk. (b) Counsel reads the agreement before any macOS distribution. (c) Replace the dependency: not possible without leaving Tauri/wry/tao on macOS. **Recommended default:** (b). **Decides:** owner with counsel.

---

<a id="pins-03"></a>
## pins-03 Commons Clause and the content of a source offer

**Question.** What must a source offer contain and how long must it last; does the Commons Clause restrict what a recipient may do with the offered source, and must the offer say so; how does the offer treat the third-party copyleft components in the same product?

**Evidence (STATIC).** AGPL 6(b) and 6(d) wording (EXACT_TEXTS `agpl-6`); the Commons Clause sentence “Any license notice or attribution required by the License must also include this Commons Clause License Condition notice.”; `packaging/SOURCE-OFFER.md` is a template whose offer wording is a bracketed placeholder; the generated offer lists the first-party commit, pins and third-party sources with hashes. Third-party copyleft present: MPL-2.0 (D-LIC-4), GPL-2.0-or-later with the Bootloader Exception (PyInstaller; the source is public), the CPL-1.0 LZMA module in an NSIS installer (D-LIC-9). **Options:** (a) counsel words the offer; (b) no distribution until (a). **Recommended default:** (a). **Decides:** counsel. **Counsel:** Q15, Q16.

---

<a id="pins-04"></a>
## pins-04 The exact commit must be public before it is referenced

The source references name the `agentic-os` commit that built the artifact. The R0-prime branch is local; the public repository has `v0.3.2` -> `feeca065` and no R0-prime commit (OBSERVED, pins-04). **Options:** (a) the owner publishes the exact commit and a tag before any binary is distributed (needs a push authorization, which no agent has; the workflow `native-packaging.yml` builds on `v*` tags and no longer has a release job: build-01); (b) reference another public form of the source (counsel). **Recommended default:** (a). **Decides:** owner. **Counsel:** Q16.

---

<a id="pins-08"></a>
## pins-08 Microsoft redistributable DLLs in the Windows sidecar

**Question.** Which Microsoft terms govern the copies of `VCRUNTIME140.dll` (14.42.34438.0), `VCRUNTIME140_1.dll`, `ucrtbase.dll` (10.0.26100.1742) and the 50 `api-ms-win-*` forwarder DLLs inside the Windows sidecar, and what notice or change does counsel require?

**Evidence** (documents read 2026-11-04; excerpts and fetch hashes in `docs/legal/sources/`; no Windows build exists here). The files come from the Python installation of the build host, copied by PyInstaller (INFERRED from the frozen R0 listing; pins-07).
* Visual Studio documentation (learn.microsoft.com, page dated 2026-04-13): “Distribution of the Visual C++ Runtime Redistributable package, merge modules, and individual binaries is limited to licensed Visual Studio users and is subject to Microsoft Software License Terms.” and on app-local copies “It's also possible to directly install the Redistributable DLLs in the application local folder.” and “For servicing reasons, we don't recommend that you use this installation location.” The VS 2022 redistribution list: “Subject to the License Terms for the software, you may copy and distribute with your program any of the files within the following folder and its subfolders except as noted below. You may not modify these files.”
* The separate licence of the Visual C++ 2015-2022 Runtime (visualstudio.microsoft.com, document sha256 `f1e3d56c...`) says you may not “provide the software as a stand-alone offering or combined with any of your applications for others to use, or transfer the software or this agreement to any third party.” and “remove, minimize, block or modify any notices of Microsoft or its suppliers in the software”.
* Universal CRT page: “Local deployment of the Universal CRT is supported, but not recommended”; “On Windows 10 and Windows 11, the Universal CRT in the system directory is always used, even if an application includes an application-local copy of the Universal CRT.”
* Not obtained: the VS 2022 licence terms' own "Distributable Code" text (the page is script-rendered) and the Windows SDK redistributable terms (HTTP 404 on the two candidate URLs): UNVERIFIED.
* So two Microsoft regimes can be read as applying to the same files, and which applies depends on where the copies came from, which is not established.

**Options.** (a) Counsel reads both regimes and states the notice set (Q26). (b) Engineering: build the Windows sidecar without the app-local UCRT DLLs (ucrtbase and the api-ms-win-* set), relying on the system UCRT that Microsoft documents as always used on Windows 10 and 11; must be tested on a Windows host (CPython 3.12 still supports Windows 8.1, where the UCRT may be absent): not done. (c) Require the Visual C++ Redistributable as an installer prerequisite (central deployment, which Microsoft recommends); counsel must check the "combined with any of your applications" clause against how it is provided. (d) Distribute macOS only until counsel has answered. **Recommended default:** (d), with (a) in parallel. **Decides:** counsel. Not an issue for macOS.
