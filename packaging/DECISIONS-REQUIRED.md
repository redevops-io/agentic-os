# Decisions required before any R0-prime distribution

Every item here needs the owner or counsel. Nothing in this file, in `LICENSING.md`, in
`packaging/notices/` or in the generated `THIRD_PARTY_NOTICES.txt` says that a component, a
text or a distribution is cleared, approved or compliant. "Ships meanwhile" says what the
R0-prime candidate contains if the decision is not taken: R0-prime is a private candidate, and
none of these items is closed by building it.

Evidence levels: OBSERVED (seen in a file, command or build in this work), STATIC (read from a
text or source), INFERRED, UNVERIFIED. Section references to the AGPL and the Commons Clause are to
the texts in `LICENSE` and `LICENSE-COMMONS-CLAUSE`; they are quoted, not interpreted.

The sections are: `## licences` (this part, agent `licences`) and `## pins` (appended by agent `pins`).

## licences

| Id | Subject | Decides | Ships meanwhile |
|---|---|---|---|
| D-LIC-1 | Product licence wording, copyright line, installer acceptance | owner | `LICENSING.md` statement, as written |
| D-LIC-2 | Inferred licences of knowledge-frontier and redevops-connectors | owner | knowledge-frontier excluded; connectors shipped, no licence text |
| D-LIC-3 | Commons Clause term in a binary that also holds plain-AGPL and other code | counsel | shipped as is, flagged |
| D-LIC-4 | certifi (MPL-2.0) | counsel | licence text shipped, no source statement |
| D-LIC-5 | AGPL section 13 for a loopback-served UI | counsel | no statement |
| D-LIC-6 | CPython, OpenSSL, PyInstaller, Microsoft runtime and statically linked libraries | owner/counsel | texts shipped where found, rest listed missing |
| D-LIC-7 | React and other JavaScript in the UI bundle | owner | React MIT text shipped; rest unknown |
| D-LIC-8 | Replacement of `hero.jpg` and its provenance | owner | neutral generated image |
| D-LIC-9 | NSIS, WiX, Tauri (and WebView2) notices | owner/counsel | crate texts shipped; installer-tool terms unread |
| D-LIC-10 | Licence acceptance pages and Appropriate Legal Notices in the UI | owner | no acceptance page, no UI notice |
| D-LIC-11 | Launcher icons and mark: authorship and rights | owner | shipped as before |
| D-LIC-12 | Source availability for object code (AGPL section 6) | owner | pointer in `LICENSING.md`, no offer |
| D-LIC-13 | Components whose package holds no licence file | owner | listed in `NOTICES_MISSING.md` |
| D-LIC-14 | Removing knowledge-frontier from the frozen sidecar | owner | removed by spec exclude (this branch) |
| D-LIC-15 | Other shipped text that calls the product "open source" | owner | `stack_knowledge` fixed; `outreach` template unchanged |
| D-LIC-16 | Hand-off: what R0-prime for the QA bundle would need | owner | QA bundle untouched |
| D-LIC-17 | Shipped-but-unexercised components with unclear licences | owner | kept (DuckDB flagged) |

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
* Evidence: CPython 3.12.10 (PSF-2.0): text and the bundled-library acknowledgements page (Doc/license.rst) are in `packaging/notices/texts/` and in `THIRD_PARTY_NOTICES.txt` (OBSERVED); which of the acknowledged libraries the shipped python.org-style build links is UNVERIFIED. OpenSSL 3.0.16 shared libraries (Apache-2.0) and OpenSSL 4.0.2 statically inside `cryptography`: LICENSE.txt of both upstream tags is byte-identical (sha256 7d5450cb...), shown for both; whether the linked bytes equal those tags is UNVERIFIED; the Windows R0 SBOM had no entry for the OpenSSL DLLs. PyInstaller 6.22.3: COPYING.txt (GPL-2.0-or-later with the Bootloader Exception) shipped as text; the R0 build used an unpinned version. Microsoft VCRUNTIME140 14.42.34438.0, ucrtbase 10.0.26100.1742 and 50 api-ms-win forwarder DLLs (Windows only): redistribution terms UNVERIFIED (unread), no text shipped, listed in `NOTICES_MISSING.md`. libffi-8.dll (Windows): version unknown, no text. libyaml (inside PyYAML) and DuckDB's embedded libraries: no text in the wheels.
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
* Evidence: Tauri crates: the licence files of every registry crate in the launcher's normal-dependency closure are collected from the crate packages (OBSERVED with a generated `Cargo.lock`; the committed lock is another agent's work; 5 crates on Windows and 14 on macOS have no licence file in their package: D-LIC-13). NSIS stub and WiX output: terms unread (matrix NAT-IN-installer-stubs). Microsoft Edge WebView2 runtime is downloaded at install time by the Windows installer default (derived from the Tauri schema, not observed); Apple WebKit is a system framework: neither is redistributed by the artifacts.
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
* Decides: owner. Ships meanwhile: as before; `LICENSING.md` section 4 does not list the icons as covered.

### D-LIC-12 Source availability for object code (AGPL section 6)

* What: the packages are object code of covered works. Section 6 says object code may be conveyed "provided that you also convey the machine-readable Corresponding Source under the terms of this License, in one of these ways" (6(a) to 6(e)).
* Evidence (STATIC): R0 packages carried no source, no offer and no pointer (matrix PRD-package-display). `LICENSING.md` now names the repository. The Corresponding Source also includes the sibling packages (runtime-contracts, redevops-connectors) at their pinned tags.
* Options: (a) accompanying source archive; (b) written offer; (c) network access under 6(d) with the repository and tags named; (d) counsel's choice.
* Recommendation: counsel; this work does not conclude.
* Decides: owner with counsel. Ships meanwhile: the repository pointer only.

### D-LIC-13 Components whose package holds no licence file

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

### D-LIC-17 Shipped-but-unexercised components with unclear licences

Candidates found in the frozen archive that the serve/bootstrap closure does not need (no behaviour-bearing component was removed):

* `knowledge-frontier`: removed (D-LIC-14).
* `setuptools` (130 PYZ entries, via a PyInstaller runtime hook from a `--seed` venv; not in the lock; vendored backports.tarfile and jaraco.*), `packaging`, `_distutils_hack`: licences are declared (MIT; Apache-2.0 OR BSD-2-Clause) but unpinned and no text ships; candidates for exclusion after a frozen-run test by the pins/defects agents (UNVERIFIED that nothing at run time needs them).
* `pycparser`: in the R0 lock, no module in either archive.
* `duckdb`: optional extra backend (`agentic_os.mission.event_backends`); kept because it is behaviour; embedded third-party libraries have no listed notices (D-LIC-6, D-LIC-13). Flagged, not removed.
* `readline` linked to the system `libedit` on macOS: system library, not redistributed.

Decides: owner. Ships meanwhile: as listed.
