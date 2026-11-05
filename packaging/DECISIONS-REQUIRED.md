# Decisions required before any R0-prime distribution

Every item here needs the owner or counsel. Nothing in this file, in `LICENSING.md`, in
`packaging/notices/` or in the generated `THIRD_PARTY_NOTICES.txt` says that a component, a
text or a distribution is cleared, approved or compliant. "Ships meanwhile" says what the
R0-prime candidate contains if the decision is not taken: R0-prime is a private candidate, and
none of these items is closed by building it.

Evidence levels: OBSERVED (seen in a file, command or build in this work), STATIC (read from a
text or source), INFERRED, UNVERIFIED. Section references to the AGPL and the Commons Clause are to
the texts in `LICENSE` and `LICENSE-COMMONS-CLAUSE`; they are quoted, not interpreted.

The sections are: `## licences` (agent `licences`), `## pins` (agent `pins`), `## build` (agent `build`) and `## lic2` (agent `lic2`, 2026-11-04: facts resolved, the distribution gate and where the decision packages are). The decision packages, the counsel brief and the owner form are in `docs/legal/`; the machine-readable list of distribution-blocking decisions is `packaging/distribution-gate.json` (every entry OPEN; only the owner can change that).

## licences

| Id | Subject | Decides | Ships meanwhile |
|---|---|---|---|
| D-LIC-1 | Product licence wording, copyright line, installer acceptance | owner | `LICENSING.md` statement, as written |
| D-LIC-2 | Inferred licences of knowledge-frontier and redevops-connectors | owner | knowledge-frontier excluded; connectors shipped, no licence text |
| D-LIC-3 | Commons Clause term in a binary that also holds plain-AGPL and other code | counsel | shipped as is, flagged |
| D-LIC-4 | certifi (MPL-2.0) | counsel | licence text shipped, no source statement |
| D-LIC-5 | AGPL section 13 for a loopback-served UI | counsel | no statement |
| D-LIC-6 | CPython, OpenSSL, PyInstaller, Microsoft runtime and statically linked libraries | owner/counsel | texts shipped (lic2 added libyaml, libffi, DuckDB tree, bzip2, Tcl/Tk, expat); Microsoft terms read, split out as pins-08 |
| D-LIC-7 | React and other JavaScript in the UI bundle | owner | React MIT text shipped; rest unknown |
| D-LIC-8 | Replacement of `hero.jpg` and its provenance | owner | neutral generated image |
| D-LIC-9 | NSIS, WiX, Tauri (and WebView2) notices | owner/counsel | crate texts shipped; NSIS and WiX texts now in the Windows notices (lic2) |
| D-LIC-10 | Licence acceptance pages and Appropriate Legal Notices in the UI | owner | no acceptance page, no UI notice |
| D-LIC-11 | Launcher icons and mark: authorship and rights | owner | temporary neutral artwork (RC3 development; RC2 shipped the previous icons) |
| D-LIC-12 | Source availability for object code (AGPL section 6) | owner | pointer in `LICENSING.md`, no offer |
| D-LIC-13 | Components whose package holds no licence file | owner | resolved by fact except redevops-connectors (lic2): connectors is the only entry left in `NOTICES_MISSING.md` |
| D-LIC-14 | Removing knowledge-frontier from the frozen sidecar | owner | removed by spec exclude (this branch) |
| D-LIC-15 | Other shipped text that calls the product "open source" | owner | `stack_knowledge` fixed; `outreach` template unchanged |
| D-LIC-16 | Hand-off: what R0-prime for the QA bundle would need | owner | QA bundle untouched |
| D-LIC-17 | Shipped-but-unexercised components with unclear licences | owner | setuptools, packaging, _distutils_hack excluded and proven (lic2, commit 66ae496); DuckDB kept |
| D-LIC-18 | objc2 crate family: derived from Apple SDKs, no licence texts in the packages | owner/counsel | upstream `LICENSE.md` shown with an UNVERIFIED note |

---

### D-LIC-1 Product licence wording, copyright line, acceptance

* What: the one statement the artifacts carry: "source-available (family licence: AGPL-3.0-or-later with the Commons Clause)", the SPDX `LicenseRef-AGPL-3.0-or-later-with-Commons-Clause`, the copyright line "Copyright (c) 2025-2026 RedevOps." (copied from `LICENSE.md`), and the short forms in `pyproject.toml`, `Cargo.toml` (`license-file`), `tauri.conf.json`.
* Evidence (STATIC): the wording is the one `LICENSE.md`, `README.md` and `modules.yaml` already use. R0 had four disagreeing statements: `pyproject.toml` "AGPL-3.0-or-later WITH Commons-Clause" (not an SPDX expression), launcher `Cargo.toml` plain "AGPL-3.0-or-later", `stack_knowledge.py` "open source (AGPL-3.0)", and the packages displayed none (matrix rows PRD-launcher-cargo-toml, PRD-package-display, NAT-1P-agentic-os). Section 3 of `LICENSING.md` lists every field now; `packaging/notices/check_licence_statements.py` fails if one drifts.
* Options: (a) keep the short statement as written; (b) a longer statement drafted by counsel; (c) replace the family licence for the packages.
* Recommendation: (a) for the private candidate; counsel review of (b) before any public release.
* Decides: owner, with counsel for the final wording.
* Ships meanwhile: the statement as in `LICENSING.md`, the three licence files and `LICENSING.md` in `Licenses/`.

### D-LIC-2 Inferred licences of knowledge-frontier and redevops-connectors

* What: both sibling packages are first-party code inside the R0 binary, and both licence files were added after the R0 build by commits whose message says "LICENSE INTENT INFERRED - owner to confirm".
* Evidence (OBSERVED, GitHub read-only API and `git clone` of the tags, 2026-11-04): `redevops-connectors` tag v0.1.0 (7fbb9ec4, the commit CI built from `@main`) has no LICENSE file; its pyproject says `AGPL-3.0-or-later`, its README badge said AGPL-3.0 + Commons Clause. Commit d990280a (2026-10-13, "chore(license): add AGPL-3.0-or-later + Commons Clause (LICENSE INTENT INFERRED - owner to confirm)") added LICENSE, LICENSE-COMMONS-CLAUSE, LICENSE.md and made pyproject/README match the badge, "Inferred from the badge, not owner-stated". `knowledge-frontier` tag v0.1.0 (d6a78ca3) has no LICENSE file; pyproject says AGPL-3.0-or-later; commit d32a82af (same day) added the AGPL text, "plain AGPL-3.0-or-later was inferred (no Commons Clause)". Both repositories are public: an anonymous `git ls-remote` succeeds and the GitHub API reports `private: false`, licence key `agpl-3.0` for both (OBSERVED). Neither wheel built from the v0.1.0 tags carries any licence file (OBSERVED in a build venv).
* Updated by lic2: knowledge-frontier now also has a tag v0.2.0 (2026-09-23) without a licence file; the `LICENSE.md` on connectors `main` says "Earlier versions of this repository were not covered by this file and are not affected retroactively by it"; the pinned connectors tag declares plain AGPL-3.0-or-later in its pyproject and a Commons Clause badge in its README. Cost of leaving connectors out, measured: 14 provider cards -> 6 (`tests/test_connectors_removal_cost.py`). Full package: `docs/legal/DECISION_PACKAGES.md` D-LIC-2.
* Options: (a) owner states the licence of each repository (and tags a release with the file); (b) keep the inference and record it; (c) for knowledge-frontier, remove it from the shipped set (done, D-LIC-14); for connectors this is not possible: 14 provider setup guides come from it (`/api/projects/{id}/apps`, dependency graph row 8).
* Recommendation: (a) before any distribution; connectors cannot be replaced in R0-prime.
* Decides: owner.
* Ships meanwhile: connectors at tag v0.1.0 with no licence text (listed in `NOTICES_MISSING.md` as D-LIC-2); knowledge-frontier not shipped.

### D-LIC-3 Commons Clause term in a binary that also holds plain-AGPL and other code

* What: whether the Commons Clause may stay in one binary together with code under plain AGPL-3.0 (knowledge-frontier as inferred, for R0) and permissively licensed code.
* Evidence (STATIC): AGPL section 7: "All other non-permissive additional terms are considered "further restrictions" within the meaning of section 10. If the Program as you received it, or any part of it, contains a notice stating that it is governed by this License along with a term that is a further restriction, you may remove that term." Section 10: "You may not impose any further restrictions on the exercise of the rights granted or affirmed under this License." `LICENSE.md` section "What this means for anything depending on agentic-os" states the owner's position for the family. Commons Clause: "Any license notice or attribution required by the License must also include this Commons Clause License Condition notice." The matrix lists the same question as LIC-03.
* Options: (a) counsel opinion that the family arrangement is sound for the packages; (b) separate the plain-AGPL component (done for knowledge-frontier in R0-prime); (c) relicense the siblings consistently.
* Recommendation: counsel review. This work does not conclude.
* Decides: counsel and owner.
* Ships meanwhile: all family-licence code plus the third-party texts, with the family licence statement.

### D-LIC-4 certifi (MPL-2.0)

* What: `certifi/cacert.pem` (the Mozilla CA list, 240216 bytes on macOS) ships inside both executables under MPL-2.0.
* Evidence (STATIC, matrix NAT-PY-certifi, LIC-18): MPL-2.0 section 3.2 as the text states it: if Covered Software is distributed in Executable Form it must also be made available in Source Code Form and recipients informed how to obtain it. The file is its own source form; no statement says so. The certifi wheel's `dist-info/licenses/LICENSE` is only the 989-byte MPL notice header ("If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/"), not the licence text (OBSERVED); the full MPL-2.0 text is therefore added from mozilla.org to `packaging/notices/texts/MPL-2.0.txt` and printed in `THIRD_PARTY_NOTICES.txt`.
* Updated by lic2: five more MPL-2.0 crates are statically linked in the launcher (cssparser, cssparser-macros, dtoa-short, option-ext, selectors; `selectors` has no licence file upstream either); certifi's `cacert.pem` is byte-identical in the wheel, the embedded archive and upstream tag 2026.10.19 (commit 39e29682).
* Options: (a) ship the text plus a sentence stating where certifi's source form is (PyPI/GitHub URL and version); (b) replace certifi by the system trust store (behaviour change: not done).
* Recommendation: (a), wording by counsel.
* Decides: counsel.
* Ships meanwhile: the MPL-2.0 text, no source-form statement.

### D-LIC-5 AGPL section 13 for a loopback-served UI

* What: what "users interacting with it remotely through a computer network" means when the sidecar serves the UI on `127.0.0.1` of the same machine.
* Evidence (STATIC): section 13: "if you modify the Program, your modified version must prominently offer all users interacting with it remotely through a computer network (if your version supports such interaction) an opportunity to receive the Corresponding Source of your version". The sidecar binds `127.0.0.1:8787` by default (`packaging/sidecar_main.py`); a deployment can mount it under a path behind a tunnel (`mounted_app`, `PROJECTS_BASE_PATH`). Whether the launcher UI counts as remote interaction is not concluded here.
* Options: (a) no statement; (b) an "about/licence" item in the UI linking the source location (also bears on D-LIC-10); (c) counsel opinion first.
* Recommendation: (c), then (b) if counsel wants it.
* Decides: counsel.
* Ships meanwhile: no UI statement.

### D-LIC-6 CPython, OpenSSL, PyInstaller, Microsoft runtime, statically linked libraries

* What: notices for redistributed runtimes and for libraries linked into wheels.
* Evidence: CPython 3.12.10 (PSF-2.0): text and the bundled-library acknowledgements page (Doc/license.rst) are in `packaging/notices/texts/` and in `THIRD_PARTY_NOTICES.txt` (OBSERVED); which of the acknowledged libraries the shipped python.org-style build links is UNVERIFIED. OpenSSL 3.0.16 shared libraries (Apache-2.0) and OpenSSL 4.0.2 statically inside `cryptography`: LICENSE.txt of both upstream tags is byte-identical (sha256 7d5450cb...), shown for both; whether the linked bytes equal those tags is UNVERIFIED; the Windows R0 SBOM had no entry for the OpenSSL DLLs. PyInstaller 6.22.3: COPYING.txt (GPL-2.0-or-later with the Bootloader Exception) shipped as text; the R0 build used an unpinned version. Microsoft VCRUNTIME140 14.42.34438.0, ucrtbase 10.0.26100.1742 and 50 api-ms-win forwarder DLLs (Windows only): updated by lic2: the Microsoft terms were READ on 2026-11-04 and name two regimes that could apply (see `docs/legal/DECISION_PACKAGES.md` pins-08); which governs the shipped copies is not established; no text shipped (it would be a statement about which terms apply). libffi-8.dll (Windows): version unknown (CPython v3.12.10 names libffi 3.4.4 for its Windows build); libyaml (inside PyYAML) and DuckDB's embedded libraries: no text in the wheels; updated by lic2: the upstream texts of libyaml 0.2.5, libffi 3.4.4 and the DuckDB v1.5.5 tree are now printed under REFERENCE NOTICES, with the linkage UNVERIFIED.
* Options: (a) accept the collected texts and record the UNVERIFIED items; (b) obtain the Microsoft redistributable terms and the remaining texts and add them to `packaging/notices/texts/`; (c) a scan of the frozen archive to settle what is linked.
* Recommendation: (b) and (c) before a Windows distribution; macOS is not affected by the Microsoft items.
* Decides: owner (texts), counsel (Microsoft terms).
* Ships meanwhile: what `THIRD_PARTY_NOTICES.txt` contains; the rest listed as missing.

### D-LIC-7 React and other JavaScript in the UI bundle

* What: `agentic_os/projects_ui/assets/index-DolE0eja.js` (223508 bytes) is vendored from the separate `redevops-projects` repository.
* Evidence (OBSERVED): banners for react, react-jsx-runtime, scheduler and react-dom (MIT, "Copyright (c) Facebook, Inc. and its affiliates") point to a LICENSE file that does not ship; the MIT text of react 18.3.1 (identical bytes in react-dom and scheduler 0.23.2 tarballs) is now in `texts/` and the notices. The npm dependency tree of the UI build is not recorded, so other bundled packages cannot be listed (UNVERIFIED). The UI repository's own licence statement ("SEE LICENSE IN LICENSE.md", GitHub AGPL-3.0 per the matrix) was not re-read.
* Options: (a) accept React texts only; (b) record the UI build's lock file and generate its notices; (c) rebuild the UI with a licence-extraction plugin.
* Recommendation: (b).
* Decides: owner.
* Ships meanwhile: React MIT text.

### D-LIC-8 `hero.jpg` replacement and provenance

* What: `agentic_os/projects_ui/hero.jpg` (1024x576, 187627 bytes, sha256 recorded in the report) carried C2PA metadata naming "Black Forest Labs API" as claim generator and IPTC `trainedAlgorithmicMedia`; the generator's terms and the rights in the output were never read.
* Evidence (OBSERVED): APP11 (JUMBF/C2PA) segment and the strings above in the original; it is only used as the `preview` of one sample "Generated hero asset" context item (`agentic_os/projects_api.py`, and the same mock in the UI bundle). The replacement is `scripts/make_neutral_hero.py`'s output: gradient and geometric shapes drawn with Pillow, no text, no photograph, no model output, no metadata segments; test `tests/test_hero_asset.py`.
* Options: (a) keep the generated placeholder; (b) the owner supplies an image whose rights they hold and records its provenance; (c) read the BFL terms and keep the original (not recommended).
* Recommendation: (a) for the candidate, (b) when the owner has an asset.
* Decides: owner (provenance statement: the placeholder was drawn by code in this repository on 2026-11-04 by the agent that wrote the script; the owner should confirm they are content to own it as first-party artwork).
* Ships meanwhile: the generated placeholder. The upstream `redevops-projects` repository still holds the original image; `scripts/build_projects_ui.sh` now keeps the placeholder when re-vendoring.

### D-LIC-9 NSIS, WiX, Tauri (and WebView2) notices

* What: installer tooling output and the Tauri runtime inside the launcher.
* Evidence: Tauri crates: the licence files of every registry crate in the launcher's normal-dependency closure are collected from the crate packages (OBSERVED with a generated `Cargo.lock`; the committed lock is another agent's work; 5 crates on Windows and 14 on macOS have no licence file in their package: D-LIC-13). NSIS stub and WiX output: updated by lic2: the NSIS COPYING (zlib/libpng, bzip2, CPL-1.0 for the LZMA module) and the WiX 3 licence (MS-RL) were read and are in `texts/`; which files end up in an installer is UNVERIFIED (matrix NAT-IN-installer-stubs). Microsoft Edge WebView2 runtime is downloaded at install time by the Windows installer default (derived from the Tauri schema, not observed); Apple WebKit is a system framework: neither is redistributed by the artifacts.
* Options: (a) record the tool terms as read by counsel; (b) leave as listed.
* Recommendation: (a) before a Windows distribution.
* Decides: owner/counsel.
* Ships meanwhile: crate texts; installer-tool and WebView2 items listed.

### D-LIC-10 Licence acceptance pages and Appropriate Legal Notices

* What: Tauri's `bundle.licenseFile` would turn on a licence page in the NSIS installer, the WiX MSI licence dialog (text converted to RTF by naive line replacement) and a click-through EULA on the macOS dmg (`bundle_dmg --eula`). It is not set: that would add an acceptance step to every installer. AGPL section 9 says: "You are not required to accept this License in order to receive or run a copy of the Program." Whether an acceptance step is wanted is the owner's call.
* Evidence (STATIC): tauri-bundler 2.10.4 source (`src/bundle/windows/nsis/mod.rs`, `msi/mod.rs`, `macos/dmg/mod.rs`); the Tauri config schema has no per-installer licence key in 2.x (the planning note's `bundle.windows.nsis.license` does not exist). Section 5(d) and section 0 define "Appropriate Legal Notices" for interactive interfaces (quoted in `LICENSING.md`); the R0 UI shows no licence statement at all (OBSERVED).
* Options: (a) files only (this branch); (b) also set `licenseFile` to a short notice file; (c) add an "About/licence" entry in the launcher or Projects UI.
* Recommendation: (a) for the candidate; decide (b) and (c) with counsel.
* Decides: owner.
* Ships meanwhile: `Licenses/` folder only.

### D-LIC-11 Launcher icons and the "Re" mark

* What: authorship and rights of `icons/*` (matrix NAT-AS-icons: "none stated; authorship/rights not recorded").
* Options: owner confirms they are first-party artwork under the family licence, or excludes them from the licence statement.
* Decides: owner. Ships meanwhile: since RC3 development, TEMPORARY neutral artwork generated from code (`scripts/make_neutral_icons.py`), not the previous mark (RC2 shipped the previous icons, unchanged); `LICENSING.md` section 4 does not list the icons as covered. Status: OPEN.

### D-LIC-12 Source availability for object code (AGPL section 6)

* What: the packages are object code of covered works. Section 6 says object code may be conveyed "provided that you also convey the machine-readable Corresponding Source under the terms of this License, in one of these ways" (6(a) to 6(e)).
* Evidence (STATIC): R0 packages carried no source, no offer and no pointer (matrix PRD-package-display). `LICENSING.md` now names the repository. The Corresponding Source also includes the sibling packages (runtime-contracts, redevops-connectors) at their pinned tags.
* Options: (a) accompanying source archive; (b) written offer; (c) network access under 6(d) with the repository and tags named; (d) counsel's choice.
* Recommendation: counsel; this work does not conclude.
* Decides: owner with counsel. Ships meanwhile: the repository pointer only.

### D-LIC-13 Components whose package holds no licence file

* Updated by lic2 (2026-11-04): r-efi keeps its licence statement in its `AUTHORS` file (now printed; package and upstream tag identical); the objc2 family now also has the standard MIT/Zlib/Apache texts (from a later upstream commit); the webview2-com crates and alloc-stdlib have upstream LICENSE files at the packaged commits; libyaml, libffi and DuckDB texts are printed as reference notices. `selectors` has no licence file upstream either (servo/stylo at the packaged commit). Re-run on the final2 SBOM: the only component without a text is redevops-connectors (D-LIC-2). Per-item table: `docs/legal/DECISION_PACKAGES.md` D-LIC-13.
* What: crates and wheels whose package carries no LICENSE/COPYING/NOTICE file, so `generate_notices.py` finds no text: on the macOS crate set `alloc-stdlib`, `block2`, `dispatch2`, `objc2` and the `objc2-*` family (SPDX declarations MIT, Zlib OR Apache-2.0 OR MIT, BSD-3-Clause per crates.io), plus libyaml and DuckDB's embedded libraries; on Windows `alloc-stdlib`, the `webview2-com*` crates and `libffi`. `selectors` (declared MPL-2.0, no licence file in the crate) is shown with the generic MPL-2.0 text and no crate copyright notice. The list is generated per build in `NOTICES_MISSING.md`.
* Options: (a) fetch the texts from the upstream repositories at the tagged versions and add them to `texts/`; (b) accept the SPDX declaration for the permissive ones; (c) per-item decision for `selectors` (MPL-2.0, file-level copyleft; as D-LIC-4).
* Recommendation: (a).
* Decides: owner. Ships meanwhile: listed, no text.

### D-LIC-14 Removing knowledge-frontier from the frozen sidecar

* What: the spec now excludes `knowledge_frontier`, `agentic_os.knowledge_frontier` and `agentic_os.knowledge_frontier_backtest` from the frozen sidecar, and the notices omit it.
* Evidence (OBSERVED): nothing in the serve/bootstrap import closure imports it (AST scan, a blocked-import run of bootstrap and of the served API, and a real PyInstaller 6.22.3 build and run; see the report). Cost: `import agentic_os.knowledge_frontier` fails inside the frozen binary (it works in a pip install).
* Options: keep excluded; or restore it once the owner states its licence (D-LIC-2). The matching `pyproject.toml` change (moving the dependency to an optional extra so the wheelhouse stops building it) is recorded in the report as a hunk, because the pins agent owns that file.
* Recommendation: keep excluded in R0-prime.
* Decides: owner. Ships meanwhile: excluded.

### D-LIC-15 Other shipped text that calls the product "open source"

* What: `agentic_os/world/outreach.py` ships an outreach email template that says "I'm building ReDevOps, an open-source runtime stack for production AI systems." `LICENSE.md` says the project is source-available, not open source. The `stack_knowledge` licensing answer was changed (this branch); the outreach copy is owner-written marketing text and was not changed. Descriptions of third-party cores ("real open-source cores") are about their licences and are unchanged, as `LICENSE.md` says.
* Options: change the template wording; leave it.
* Decides: owner. Ships meanwhile: unchanged.

### D-LIC-16 Hand-off: what R0-prime for the QA bundle would need

Separate repository (`redevops-qa`); nothing was changed here. From the matrix, a QA R0-prime would need: decisions on the 24 evidence items that say "none yet" (report.css HTML Dog tab styles, status unverified and flagged BLOCKER in the matrix; jQuery tablesorter MIT/GPL-2.0 election to confirm for public distribution; jquery-tmpl, jquery-highlight, jsxcompressor, Windows highlighting code: declared-only, no texts), the 6 shipped third-party wheels recorded `staged_unmodified` with decision "none yet", the browser-logo artwork and codicon font (two candidate licences), the plain-AGPL `agent-harness` tree inside a family-licence bundle (same question as D-LIC-3), and the public-licence statement that its own `LICENSE.md` says is not final. Owner decides; QA work stays in its repository.

### D-LIC-18 The objc2 crate family (macOS launcher)

* What: the macOS launcher links `objc2`, `block2`, `dispatch2` and ten `objc2-*` crates (through tauri/wry/tao). The crate packages carry no licence file; the upstream repository (madsmtm/objc2) holds one root file, `LICENSE.md`, that says `objc2`, `block2`, `objc2-foundation` and `objc2-encode` are MIT and the others Zlib OR Apache-2.0 OR MIT, without the licence texts, and states that the crates are derived from Apple SDKs shipped with Xcode and that "it is unclear whether distributing derived works such as these crates are allowed" under the Xcode licence.
* Evidence: OBSERVED. `fetch_crate_sources.py` fetched `LICENSE.md` from the upstream commit each package was built from (commit and sha256 in the `UPSTREAM-SOURCE.txt` next to it, shown in `THIRD_PARTY_NOTICES.txt` with an UNVERIFIED note); no MIT/Zlib/Apache text exists at those commits for these crates.
* Options: (a) accept the upstream statement and add the standard MIT/Zlib/Apache texts for these crates; (b) counsel reads the Xcode licence question before any distribution; (c) replace the dependency (not possible without leaving Tauri on macOS).
* Decides: owner/counsel. Ships meanwhile: the `LICENSE.md` text with the UNVERIFIED note; the Apple SDK question is open.

### D-LIC-17 Shipped-but-unexercised components with unclear licences

Candidates found in the frozen archive that the serve/bootstrap closure does not need (no behaviour-bearing component was removed):

* `knowledge-frontier`: removed (D-LIC-14).
* `setuptools` (130 PYZ entries, via a PyInstaller runtime hook from a `--seed` venv; not in the lock; vendored backports.tarfile and jaraco.*), `packaging`, `_distutils_hack`: licences are declared (MIT; Apache-2.0 OR BSD-2-Clause) but unpinned and no text ships; updated by lic2: PROVEN removable (156 archive entries, nothing added; smoke 63/63, locales 8/8, lifecycle PASS, bootstrap byte-identical) and excluded by the spec in commit 66ae496 with `tests/test_sidecar_build_only_excludes.py`; the setuptools advisory of pins-17 therefore no longer concerns a shipped component.
* `pycparser`: in the R0 lock, no module in either archive.
* `duckdb`: optional extra backend (`agentic_os.mission.event_backends`); kept because it is behaviour (the documented single-node durable ledger); its embedded third-party libraries now have reference notices (D-LIC-6, D-LIC-13). Not removed.
* `readline` linked to the system `libedit` on macOS: system library, not redistributed.

Decides: owner. Ships meanwhile: as listed.

## pins

Written by agent `pins`. Evidence levels: OBSERVED (seen in a run/response), STATIC (read from files),
INFERRED, UNVERIFIED. Dates are 2026-11-04.

| ID | Item | Level | What is needed |
|---|---|---|---|
| pins-01 | **Connectors repository visibility.** `redevops-io/redevops-connectors` is PUBLIC: anonymous HTTP 200 for the repository page and the REST API (`private: false`), anonymous `git ls-remote` lists `refs/tags/v0.1.0` (tag object `f36bdf55...`, peeled commit `7fbb9ec4d4fa...`) and `main` (`eed11f97...`, 15 commits after the tag). Not a blocker: a source reference to it can be satisfied while it stays public. | OBSERVED | Owner: confirm it stays public for as long as any build that ships it is distributed; the offer breaks silently if it goes private. |
| pins-02 | **The pinned tags carry no licence file.** `redevops-connectors` v0.1.0 and `knowledge-frontier` v0.1.0 contain no LICENSE file (only a `license` field in pyproject.toml: AGPL-3.0-or-later). connectors added LICENSE + LICENSE-COMMONS-CLAUSE on `main` on 2026-10-13 with the commit message "LICENSE INTENT INFERRED - owner to confirm". So the exact corresponding-source tree for the shipped bytes has no licence text. | STATIC | Owner/counsel: confirm the intended licence of both; decide whether to ship a newer tag that carries the files (that would change the pin and the shipped bytes, so it is not done here) or to state the licence in the notices only. Coordinate with agent `licences`. |
| pins-03 | **Meaning of the Commons Clause for a source offer.** The first-party code is source-available under AGPL-3.0-or-later with the Commons Clause. Open: what a source offer must contain and for how long; whether the Commons Clause restricts what a recipient may do with the offered source and whether the offer must say so; how this interacts with third-party copyleft components in the same product (PyInstaller bootloader: GPL-2.0-or-later with a bootloader exception per its metadata; certifi MPL-2.0 file-level source; no LGPL component was identified but OS-level and statically linked contents are not enumerable, see SBOM limitations). | - | Counsel. `packaging/SOURCE-OFFER.md` keeps a bracketed placeholder for the offer wording; the generator records only facts. |
| pins-04 | **The R0-prime first-party commit must exist publicly before it can be referenced.** `packaging/sbom/generate_source_offer.py` names the `agentic-os` commit that built the artifact. The R0-prime branch is local; the public repository (`github.com/redevops-io/agentic-os`, public) has `v0.3.2` -> `feeca065` and no R0-prime commit. | OBSERVED | Owner: decide the branch/tag/release that publishes the exact commit, and do it before distributing any binary; a build from a dirty tree is flagged in the manifest and the offer. |
| pins-05 | **Workflow trigger and the `release` job.** At this tree `native-packaging.yml` triggers on `v*` tags (not `apps-v*`) and its existing `release` job publishes the installers to a GitHub Release on such a tag. The pins work kept the trigger and the job unchanged as instructed. Pushing any `v*` tag would therefore build AND publish unsigned installers. | STATIC | Owner: decide the trigger (`apps-v*`) and whether the release job should stay, before any tag exists on R0-prime. |
| pins-06 | **CPython identity differs between CI and a local check build.** The frozen CI build used the runner image's preinstalled interpreter (macOS: python.org framework at `/usr/local/bin/python3.12`; Windows: `C:\hostedtoolcache\windows\Python\3.12.10`), NOT `actions/setup-python` and not uv's python-build-standalone. The pinned workflow now installs 3.12.10 with `actions/setup-python` (exact version, always available). The local check builds use uv's python-build-standalone 3.12.10. Same version, different CPython builds: the PYZ module set is identical (1089 modules) but the PKG layer differs (python.org: `Python.framework`, `libcrypto.3.dylib`/`libssl.3.dylib` OpenSSL 3.0.16, `lib-dynload/*.so`; standalone: `libpython3.12.dylib` with OpenSSL 3.0.16 linked in, no separate dylibs). | OBSERVED (local vs frozen listing) / UNVERIFIED (what setup-python installs on macos-14) | Owner: choose which interpreter is the R0-prime identity and confirm on a CI run; the SBOM verifier will report whichever one is shipped. |
| pins-07 | **windows-2022 instead of the frozen `windows-latest`.** The frozen Windows leg ran on image `windows-2025-vs2026`. The brief pins `windows-2022`. VCRUNTIME140.dll / ucrtbase.dll shipped inside the sidecar come from the Python installation (frozen: 14.42.34438.0 / 10.0.26100.1742), so they are expected to be unchanged, but that is INFERRED. | INFERRED | Confirm with the first Windows build (SBOM verifier lists the DLL versions). |
| pins-08 | **Microsoft redistributable DLLs have no source to offer.** VCRUNTIME140(_1).dll, ucrtbase.dll and the `api-ms-win-*.dll` forwarders are shipped in the Windows sidecar; they are binary-only. The SBOM now lists them (declared licence: none read). | STATIC | Counsel/owner: confirm the redistribution terms that apply to them and what the notices say. |
| pins-09 | **Interpreter, OpenSSL, libyaml, libffi, DuckDB and compiled-extension contents.** The SBOM lists CPython and both OpenSSL versions (3.0.16 in the interpreter build, 4.0.2 statically linked into `cryptography`) with release-tarball references, and the Rust crates embedded in `cryptography` (33) and `pydantic_core` (103) as UPSTREAM-DECLARED from each wheel's own embedded SBOM. DuckDB's embedded libraries, libyaml's version, libffi's version and the libraries inside CPython's extension modules cannot be enumerated from the shipped binaries and are explicit `limitations`. | OBSERVED | Owner: accept the limitation in any public SBOM statement, or fund a source-level analysis. |
| pins-10 | **UI bundle source.** The React 18.3.1 / react-dom 18.3.1 / scheduler banners are read from the vendored UI bundle; the bundle is built from the separate `redevops-projects` repository and its npm lock is not in this repository, so the corresponding source of the first-party UI JavaScript and the exact npm tree cannot be named. `scheduler`'s version is not in the bundle. The frozen `hero.jpg` carried C2PA metadata of an AI image generator; agent `licences` replaced it in R0-prime (D-LIC-8) and the SBOM reads whatever image is in the archive. | OBSERVED | Owner: record the redevops-projects tag/commit and npm lock that produced `agentic_os/projects_ui`. |
| pins-11 | **Cargo.lock is a reconstruction.** 343 crate rows recovered from the three CI logs are pinned exactly (all matched; they cover every crate compiled on the macOS, Windows and Linux legs). The other 84 rows (crates only needed on targets the CI legs did not build) are the newest crates.io release published before the frozen run, found by API (INFERRED); 427 rows vs 429 in the frozen run (2 rows of the frozen lock cannot be reproduced). A launcher built from it uses the frozen crate versions but is not claimed to be byte-identical to the frozen launcher (different build host, interpreter-independent but path- and toolchain-dependent). | OBSERVED / INFERRED | None required unless the frozen launcher bytes must be matched (they cannot be). |
| pins-12 | **Upstream wheel availability.** All 63 wheel files of the frozen run (3 legs x 21) are still on PyPI and none is yanked (checked 2026-11-04). PyPI can remove files; the hash locks make a removal a hard failure, not a silent change. | OBSERVED | Optional: mirror the 21 wheels per OS next to the release evidence. |
| pins-13 | **knowledge-frontier.** Still pinned (tag v0.1.0, commit d6a78ca3) and still built into the wheelhouse because pyproject keeps it a core dependency; the merged `redevops-sidecar.spec` (agent `licences`, D-LIC-14) excludes it from the frozen archive, so the SBOM, which is derived from the archive, does not list it as shipped. The wheelhouse therefore holds one wheel that is not shipped. | STATIC | Owner (D-LIC-2 / D-LIC-14): if it stays excluded, move the dependency to an optional extra in pyproject, drop it from `git-pins.json` and rebuild; `verify-pins.py` follows those two files. |
| pins-14 | **Runner image version floats.** The image NAME is pinned; the image VERSION (e.g. `20260831.0302.1` in the frozen run) cannot be pinned and is recorded in `build-manifest.json` (`runner.env.ImageVersion`). No byte-for-byte reproducibility across runner image versions is claimed. | OBSERVED | None; state it wherever reproducibility is mentioned. |
| pins-15 | **RESOLVED on r0prime/build (see `## build`).** **Notices tooling and the old lock.** `packaging/notices/build-licenses.sh` / `interim_sbom.py` read `packaging/requirements.lock` (the stale pip-freeze). That file is now a universal hash lock of the 21 shipped third-party wheels (same name==version syntax), so the tools keep working, but they still list lock pins, not the built archive. `packaging/build-sbom.sh` emits the archive-derived SBOM (CycloneDX 1.5) with the same component fields. | STATIC | Agent `licences` / orchestrator: point the notices generator at `packaging/dist/sbom/sbom.cdx.json` instead of the interim list. |
| pins-16 | **certifi 2026.10.19 instead of the frozen 2026.7.22 (deliberate deviation; independent review round 2, N-01 MEDIUM).** GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320: the bundle of certifi < 2026.10.18 trusts the `emSign ECC Root CA - G3` root (eight chained intermediates have compromised keys). Locks regenerated with a certifi-only cut-off (`LOCK_EXCLUDE_NEWER_CERTIFI`, `lock-constraints.in`); diff = certifi only; `verify-pins.py` allows exactly this deviation. 2026.10.19 contains emSign C1/C3/G1 (the unaffected roots, restored upstream after 2026.10.18 had removed all emSign roots) and no ECC G3. The candidate's embedded `cacert.pem` is checked for the ECC G3 subject (see the final2 report). The frozen R0 candidate still ships the vulnerable bundle. | OBSERVED (PyPI wheels hashed, bundle subjects listed, OSV: 0 advisories for the 21 pins) | Owner: accept the deviation from the frozen wheel set (reproducibility of the frozen set is given up for this one wheel); `uv.lock` (developer lock, stale) is not regenerated. |
| pins-17 | **setuptools 84.0.0 advisory GHSA-xmj3-9gf7-h52w (LOW, Windows, review N-09): NOT bumped.** `setuptools/wheel.py` legacy `Wheel.install_as_egg` can create an `__init__.py` outside the destination on Windows from a hostile wheel's `namespace_packages.txt`; fixed in 84.1.0 (84.2.1 is current). setuptools is a build tool here (the hash-locked `build-tools`/`build-backends` locks and the freeze venv); it also appears in the frozen sidecar archive as PyInstaller's runtime-hook dependency (130 PYZ entries, `licences` D-LIC-17), where no code calls the legacy wheel installer (STATIC). The build installs wheels with pip, not with setuptools' legacy path, and all wheels come from hash-locked PyPI files or from this repository. A bump changes a shipped component and the frozen module set, was not tested for PyInstaller 6.22.3 / hooks-contrib 2026.7 compatibility, and the affected platform (Windows) is not built here. | STATIC | Owner: bump `SETUPTOOLS_VERSION` and the two build locks with the same documented-deviation mechanism when the Windows build is prepared (requires a build + a PYZ module-set comparison). |
| pins-18 | **Build script environment (review N-06, INFO): fixed.** `packaging/candidate/build-local-macos-candidate.sh` now runs under a throwaway HOME and TMPDIR below the output dir (`.build-work`, excluded from SHA256SUMS), passes only the uv-managed CPython dir, uv's cache and CARGO_HOME by path, sets `PYINSTALLER_CONFIG_DIR` per build (no collision in `~/Library/Application Support/pyinstaller`), and cleans its temp dirs and a mounted dmg on exit (`trap`). | OBSERVED (final2 build) | none |

## build

Written by agent `build` (branch `r0prime/build`): integration fixes, the local macOS candidate. Evidence levels as above.

| Id | Item | Level | Needs |
|---|---|---|---|
| build-01 | **No release job any more (pins-05 closed for R0-prime).** The `release` job of `native-packaging.yml` (publishes unsigned installers on any `v*` tag) is removed, the token is read-only, and `tests/test_workflow_no_publish.py` fails on any workflow with a release/publish job, publishing action, write permission or secret. The `v*` tag trigger still BUILDS (artifacts stay private workflow artifacts). A later publication needs a separate owner authorization and signed artifacts; the old job is in git history at feeca065. | STATIC + tests | Owner: the trigger (`v*` vs `apps-v*`) is still open, publication is not wired anywhere. |
| build-02 | **Notices come from the built SBOM (pins-15 closed).** `build-licenses.sh` requires `--sbom`; `build-notices.sh` feeds it the SBOM of the pass-1 sidecar archive; `check_notices_vs_sbom.py` closes `build-all.sh` by comparing the staged notices (and the Licenses entries embedded in the sidecar, by sha256) with the final `packaging/dist/sbom/sbom.cdx.json`. `interim_sbom.py` is kept for tests and an explicit `--interim-lock` dry run only. | OBSERVED (local build) | none |
| build-03 | **Notices coverage and what is still listed as missing.** Local macOS build: 405 components, 398 with a text, 5 first-party/system items listed as such (not missing), **2 without a text**: `redevops-connectors` 0.1.0 (D-LIC-2: the pinned tag has no licence file) and `r-efi` 5.2.0 (D-LIC-13: neither the package nor the upstream repository at the packaged commit has one; declared MIT OR Apache-2.0 OR LGPL-2.1-or-later). Texts that are NOT the package's own file are marked UNVERIFIED in `THIRD_PARTY_NOTICES.txt`: the objc2 family (D-LIC-18; upstream `LICENSE.md`, a licence-selection statement), crates whose texts came from the upstream repository at the packaged commit (`UPSTREAM-SOURCE.txt`), `selectors` (generic MPL-2.0 text). 115 crates that the cryptography/pydantic_core wheels declare as statically linked were fetched from crates.io and verified by sha256 (SBOM hash or sparse-index cksum) before extraction; their versions are as DECLARED by the wheels (upstream-declared, not read from the binaries). | OBSERVED | Owner: D-LIC-2, -13, -18. |
| build-04 | **Launcher retry (security stage 2).** Bounded retry for 429 / `401 invalid_proof` after a verified handshake (`min(Retry-After,5)` s, 3 tries, then "local service busy", browser not opened, sidecar not stopped); a failed handshake proof stays final. Behaviour on Windows UNVERIFIED. | OBSERVED (cargo test) | none |
| build-05 | **Entitlement input for later signing.** `packaging/macos/sidecar.entitlements.plist` (only `disable-library-validation`) and `docs/MACOS_ENTITLEMENTS.md` (residual risk). Nothing is signed. The alternative that removes the need (sign every embedded Mach-O with one Team ID, or onedir) is not done. | STATIC + tests | Owner: accept the entitlement for the sidecar, or fund the onedir/sign-all-libraries change before a public release. |
| build-06 | **LSMinimumSystemVersion** was Tauri's default 10.13 while both Mach-O files are arm64 `minos 11.0`; set to 11.0 in `tauri.conf.json`. | OBSERVED (Info.plist of a local build) | none |
| build-07 | **The 7 failing tests are macOS-specific:** `setrlimit(RLIMIT_AS)` below ~466 GiB fails with EINVAL on this macOS (27.0.1; even `bash -c 'ulimit -v 262144'`), and `agentic_os/mission/membrane.py` calls it unguarded in a `preexec_fn`, so the rootless containment backend fails closed (error) on macOS instead of running contained. Not caused by the ssh-agent or `APPROVAL_SECRET`. | OBSERVED | Owner: leave (the failure is closed), or have the containment code tolerate macOS (a product change, not made here). |

## rc3-rlimit

Written by agent `rlimit` (branch `rc3/rlimit`). Evidence levels as above.

| Id | Item | Level | Needs |
|---|---|---|---|
| rlimit-01 | **build-07 dispositioned: keep fail-closed on macOS, make it explicit and tested (option A).** `LocalContainmentSandbox` now refuses on a platform that cannot enforce a memory ceiling (macOS) with `ContainmentUnsupported` (`containment_unsupported:<reason>`) before any process is created; a preexec failure elsewhere is a named `containment_setup_failed` (still closed, not swallowed). Linux path unchanged (RLIMIT_AS still set from the constraint). The device probe no longer claims `containment_supported` on macOS, so "Check this device" on macOS reports BLOCKED (it previously reported VERIFIED when Docker was found, which was false). Tests are platform-aware (macOS asserts the refusal, Linux runs the original assertions). Linux execution UNVERIFIED on this host. | OBSERVED (macOS) / STATIC + simulated (Linux) | Owner: accept option A, or decide on option B (run on macOS with the limits that work and no memory cap): NOT implemented, weakens containment, see `docs/RUNNING_AGENTIC_APPS_LOCAL.md` "Platform support: containment". |

## rc2

Written by agent `rc2` (branch `r0prime/rc2`): distinct identity (decision 8), limitations documentation (decisions 4 and 5), certifi compatibility test (decision 6). Evidence levels as above.

| Id | Item | Level | Needs |
|---|---|---|---|
| rc2-01 | **R0-prime identity implemented (`docs/R0_PRIME_IDENTITY.md`, `packaging/identity.json`).** Product `Agentic Apps R0-prime`, version `0.1.1` (plain, so Apple, MSI and NSIS accept it; no pre-release suffix), bundle id `io.redevops.agentic-apps.r0prime`, launcher binary `agentic-r0prime`, pinned MSI UpgradeCode `f13fc625-f77d-5c30-b308-a3c3ec1045fc`, `RedevopsReleaseLine` in Info.plist, `release_line: r0-prime` in the build manifest. | OBSERVED (scratch build of the bundle and dmg names); STATIC for Windows | owner: confirm the names and the version number before the next candidate is built |
| rc2-02 | **Evidence tooling outside the repository still hard-codes the frozen names** (`Agentic Apps.app`, `agentic-apps`, `Agentic Apps_0.1.0_aarch64.dmg`, product block in `make_release_manifest.py`). It must read `packaging/identity.json` before the next evidence run; final3 describes the old identity and must not be reused. | STATIC | orchestrator / evidence agent |
| rc2-03 | **Identity of the eventual release.** An R0-prime install is not upgraded in place by a release that uses the production identity (`io.redevops.agentic-apps`) or a different UpgradeCode, and the frozen `sign_and_notarize_macos_v2.sh` hard-codes the frozen names and must be re-reviewed for the new ones. Whether the release keeps the R0-prime identity or returns to the production one is open. | STATIC | owner |
| rc2-04 | **Header-session limitations documented (decision 5): `docs/LOCAL_API_SECURITY.md` section 9, `docs/RUNNING_AGENTIC_APPS_LOCAL.md`.** The sign-in banner text of the UI shim was changed (text only, with a test) to name the launcher button. Browser-specific behaviour (session restore, duplicate tab, bookmarks) and Docker were not exercised. | STATIC / OBSERVED for the shim | owner: accept the limitations for R0-prime |
| rc2-05 | **Sidecar entitlement kept provisionally (decision 4) with exit criteria in `docs/MACOS_ENTITLEMENTS.md`.** The evidence from the `gui` agent is a marked placeholder there; no result is claimed. | n/a | `gui` agent report |
| rc2-06 | **certifi 2026.10.19 retained (decision 6).** Compatibility test over 3,665 hosts: 0 regressions against 2026.7.22; 19 tested hosts chain to `emSign Root CA - G1` (kept) and all validate; 2026.10.18 (zero emSign roots) would have broken those 19. Limit: hosts outside the sample that depend on `emSign ECC Root CA - G3` are not excluded. | OBSERVED | none (optional: a certificate-transparency enumeration of G3-issued certificates) |

## lic2

Written by agent `lic2` (branch `r0prime/lic2`), 2026-11-04. Nothing below is a decision, a clearance or legal advice. Owner decision 7 of
2026-11-04 applies: permission to redistribute is never inferred from an SBOM entry or an inferred licence.

| Id | Item | Level | What is needed |
|---|---|---|---|
| lic2-01 | **The distribution gate.** `packaging/distribution-gate.json` lists every decision that blocks distribution (18; 15 apply to macOS, 16 to Windows), each OPEN. `packaging/check-distribution-gate.py` refuses a build with `REDEVOPS_DISTRIBUTION_BUILD=1` while any applicable blocker is OPEN or DECIDED without the written record; the candidate script and `build-all.sh` call it first (exit 3). A private candidate build passes and prints the open list. Tests: `tests/test_distribution_gate.py` (25). | OBSERVED (tests) | Owner: signs `docs/legal/OWNER_DECISION_FORM.md` and records each decision in the gate file. CI wiring (`native-packaging.yml`, `build-windows.ps1`) belongs to their owners and is not edited here. |
| lic2-02 | **Decision packages, counsel brief, owner form.** `docs/legal/DECISION_PACKAGES.md`, `COUNSEL_BRIEF.md` (32 numbered questions, no conclusions), `OWNER_DECISION_FORM.md` (generated from the gate file), `EXACT_TEXTS.md` (AGPL 0/1/5/6/7/9/10/13, the Commons Clause, `LICENSE.md` passages, MPL-2.0, PSF-2.0, Apache-2.0 section 4, the PyInstaller Bootloader Exception, NSIS CPL-1.0 section 3, WiX MS-RL, samples of MIT/BSD-3/Unicode-3.0, r-efi AUTHORS; cut mechanically from the files), `sources/` (excerpts of the Microsoft and Apple documents, sibling-repository facts), `QA_BUNDLE_HANDOFF.md`. Tests: `tests/test_legal_documents.py` (every curly-quoted passage is verbatim; the generated files are current; no claim of clearance without a negation). | OBSERVED | Owner: hand the brief to counsel. |
| lic2-03 | **Texts the generator now prints (all found, none invented).** r-efi (AUTHORS), objc2 family (standard MIT/Zlib/Apache from upstream commit 8a5abacd, a LATER commit: UNVERIFIED copyright line), NSIS and WiX (Windows), and a REFERENCE NOTICES section for libraries that the SBOM names only as a limitation: libyaml 0.2.5, libffi 3.4.4, the DuckDB v1.5.5 tree (root and 29 third_party licence files), bzip2 1.0.8, Tcl/Tk 8.6.14 and expat 2.6.3 (banners seen in the local interpreter's `libpython3.12.dylib`). `texts/MANIFEST.json` records URL and sha256 for all 43+ texts (`fetch_reference_texts.py --verify`). Re-run on the final2 candidate SBOM: 406 components, 399 with a text, 6 first-party/system items, 1 without a text (redevops-connectors). The candidate itself was NOT rebuilt. | OBSERVED | The next candidate build picks the texts up; the CI interpreter's linked libraries (python.org build, pins-06) are a different set and were not examined (UNVERIFIED). |
| lic2-04 | **Engineering resolution of the unexercised items.** setuptools, packaging and `_distutils_hack` (156 archive entries incl. five stdlib modules only they pulled in) are excluded by `redevops-sidecar.spec` (commit 66ae496). Baseline vs excluded frozen builds of the same inputs: smoke 63/63 both, locales 8/8, lifecycle PASS, bootstrap byte-identical; 36,753,776 -> 35,506,144 bytes. `pycparser`: in no archive, nothing to exclude. `duckdb`: kept (behaviour). | OBSERVED | Owner: accept (D-LIC-17); a new candidate is needed to ship it. |
| lic2-05 | **Windows: the app-local UCRT DLLs may be unnecessary on Windows 10 and 11** (Microsoft: "the Universal CRT in the system directory is always used, even if an application includes an application-local copy"), which would remove ucrtbase.dll and the 50 api-ms-win-* files from the Windows sidecar. Needs a test on a Windows host (CPython 3.12 still supports Windows 8.1); not done. | STATIC | Counsel on pins-08 first; then the Windows build decides. |
