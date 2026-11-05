# Brief for counsel: licensing questions before any distribution of Agentic Apps R0-prime

Prepared 2026-11-04 for the owner to hand to counsel. It states facts, reproduces the exact texts the questions turn on, and lists
numbered questions. **It draws no conclusion, does not say that anything is cleared, compliant or permitted, and is not itself legal
advice.** The author is a software agent; every fact carries an evidence level: OBSERVED (seen in a file, command, fetch or run),
STATIC (read from a text or source), INFERRED, UNVERIFIED. Verbatim quotations are in curly double quotation marks and are checked mechanically
against the source files (`tests/test_legal_documents.py`); longer texts are in `docs/legal/EXACT_TEXTS.md` (block ids `agpl-6` and so
on) and `docs/legal/sources/`. The decision packages (`docs/legal/DECISION_PACKAGES.md`) give the options and consequences for the
owner; the owner records decisions in `docs/legal/OWNER_DECISION_FORM.md`; the machine-readable list is `packaging/distribution-gate.json`.

## 1. The product and what is to be distributed

* **Agentic Apps**: a desktop application. A Tauri (Rust) launcher shows a window and starts a frozen Python "sidecar" (one
  PyInstaller executable) that serves a web UI and a local API on a loopback address of the same machine; the launcher opens the
  user's default browser on a one-time URL. No server of the owner is involved in normal use.
* **Artifacts.** macOS arm64: `Agentic Apps.app` inside a `.dmg` (a private engineering candidate exists, built locally, unsigned).
  Windows x64: an `.msi` and an NSIS `.exe` are prepared but have never been built. Nothing has been signed, notarized, published or
  distributed by the R0-prime work; the owner decided on 2026-11-04 that R0-prime is a private engineering candidate.
* **Source.** The first-party repository is `https://github.com/redevops-io/agentic-os` (public). R0-prime is a local branch; the
  public repository has no R0-prime commit (OBSERVED). First-party siblings built into the product: `runtime-contracts` v0.3.4 (family
  licence), `redevops-connectors` v0.1.0 (licence status: Q7), and a vendored UI bundle built from the separate `redevops-projects`
  repository (its npm lock is not recorded). `knowledge-frontier` v0.1.0 was removed from the frozen binary.
* **Licence the owner states.** “Agentic Apps and the agentic-os control plane are source-available (family licence: AGPL-3.0-or-later
  with the Commons Clause).” with the identifier `LicenseRef-AGPL-3.0-or-later-with-Commons-Clause` and the line “Copyright (c)
  2025-2026 RedevOps.” (`LICENSING.md`, `LICENSE.md`). The files `LICENSE` (GNU AGPL v3, 19 November 2007), `LICENSE-COMMONS-CLAUSE`
  and `LICENSE.md` are in the repository and in the shipped `Licenses/` folder (EXACT_TEXTS `commons-clause`, `licence-md-*`).
* **What each artifact carries today (OBSERVED, final2 candidate listings).** Inside the `.app`: `Contents/Resources/Licenses/` with
  `LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`, `LICENSING.md`, `THIRD_PARTY_NOTICES.txt` (about 1.2 MB, 406 components in the
  re-run), `NOTICES_MISSING.md/.json`, `LICENSES-MANIFEST.json`; the same folder is embedded in the sidecar. The dmg root has no
  `Licenses/` folder. Neither the `.app` nor the dmg contains source, a source offer or `SOURCE-OFFER.md`. The UI displays no licence or
  copyright notice.

## 2. How to read the texts

Section numbers below are those of the AGPL, the Commons Clause, MPL-2.0 and the other texts as reproduced in `docs/legal/EXACT_TEXTS.md`.
Key passages (full sections are in that file):

* AGPL 7: “All other non-permissive additional terms are considered "further restrictions" within the meaning of section 10. If the Program as you received it, or any part of it, contains a notice stating that it is governed by this License along with a term that is a further restriction, you may remove that term.”
* AGPL 10: “You may not impose any further restrictions on the exercise of the rights granted or affirmed under this License.”
* AGPL 13: “if you modify the Program, your modified version must prominently offer all users interacting with it remotely through a computer network (if your version supports such interaction) an opportunity to receive the Corresponding Source of your version”
* AGPL 6: object code may be conveyed “provided that you also convey the machine-readable Corresponding Source under the terms of this License, in one of these ways”, (a) to (e).
* AGPL 5(d) and 0: “If the work has interactive user interfaces, each must display Appropriate Legal Notices”; the term means “a convenient and prominently visible feature that (1) displays an appropriate copyright notice, and (2) tells the user that there is no warranty for the work (except to the extent that warranties are provided), that licensees may convey the work under this License, and how to view a copy of this License.”
* Commons Clause: “the grant of rights under the License will not include, and the License does not grant to you, the right to Sell the Software.” and “Any license notice or attribution required by the License must also include this Commons Clause License Condition notice.” It names “Software:  agentic-os” and “Licensor:  RedevOps”.
* `LICENSE.md`: the repository relicensed on 2026-08-08; releases up to `v0.1.0` “were distributed under **AGPL-3.0-or-later alone**”; “Anything importing these packages is a work based on them and takes on both the AGPL's network obligation and the Commons Clause's restriction on selling.”; “All authoring commits in this repository are by RedevOps (Alex Mats, under two git identities).”

## 3. What is in the binaries (licences present)

From the notices of the final2 candidate (405 components; a re-run on the final SBOM gives 406). Declared licences, as package metadata
states them; none is verified against the shipped bytes:

| Group | Components | Declared licence |
|---|---|---|
| Family | agentic-os, runtime-contracts 0.3.4 | the family licence |
| Sibling, status unclear | redevops-connectors 0.1.0 | pyproject at the pinned tag: `AGPL-3.0-or-later`; README badge at the tag: AGPL-3.0 + Commons Clause |
| Permissive | the large majority of the Rust crates and Python wheels | MIT, Apache-2.0, dual forms, BSD-3-Clause, BSD-2-Clause, Zlib, Unlicense, 0BSD, MIT-0, CC0-1.0 (as alternatives) |
| Unicode | 37 crates (icu_*, etc.) | Unicode-3.0; one crate "(MIT OR Apache-2.0) AND Unicode-DFS-2016" |
| MPL-2.0 | certifi (and its `cacert.pem`); crates cssparser, cssparser-macros, dtoa-short, option-ext, selectors | MPL-2.0 |
| Copyleft with alternatives | self_cell | "Apache-2.0 OR GPL-2.0-only"; r-efi: "MIT OR Apache-2.0 OR LGPL-2.1-or-later" |
| Runtime | CPython 3.12.10 | PSF-2.0 (and bundled-software acknowledgements) |
| Runtime | PyInstaller 6.22.3 bootloader | GPL-2.0-or-later with the Bootloader Exception; hooks Apache-2.0 |
| Crypto | OpenSSL 3.0.16 and 4.0.2 (static in `cryptography`) | Apache-2.0 |
| Windows only | VCRUNTIME140*.dll, ucrtbase.dll, 50 api-ms-win-*.dll, libffi-8.dll | Microsoft terms (Q26); libffi MIT |
| macOS only | objc2 crate family | MIT, or Zlib OR Apache-2.0 OR MIT; "derived from Apple SDKs" (Q25) |

The notices generator and the tests record, for each component, which text was found and its sha256. The only component without any
text is `redevops-connectors` (Q7).

## 4. Facts and questions by topic

### 4.1 Product licence and holder (decision D-LIC-1)

Facts: section 1 above; the relicensing date and the statement that earlier releases remain AGPL-only; the contribution-history
paragraph. No file of the repository states what legal person "RedevOps" is (UNVERIFIED; the owner knows).

* **Q1.** Do `LICENSE`, `LICENSE-COMMONS-CLAUSE` and `LICENSE.md` validly set out the terms under which the holder may convey these packages? The Commons Clause names only “agentic-os” as the Software: do its terms reach the Tauri launcher crate, the installers and the vendored UI in the same repository?
* **Q2.** Is "RedevOps" a legal person that holds the copyright in all authoring commits (the repository says they are by “Alex Mats, under two git identities”)? What should the holder line and the Commons Clause "Licensor" say? Is the evidence recorded in `LICENSE.md` ("Contribution history") enough for the 2026-08-08 relicensing?
* **Q3.** Is the short statement above, with a `LicenseRef-` identifier, acceptable in package metadata, installers and the UI, or is a longer statement needed? Is the word "source-available" appropriate?

### 4.2 Commons Clause and other licences in one binary (D-LIC-3)

Facts: AGPL 7 and 10 and the Commons Clause as quoted in section 2; the licence table in section 3; knowledge-frontier (plain AGPL as
inferred) is no longer in the binary; redevops-connectors may be plain AGPL (Q6).

* **Q4.** In a binary combining family code under the Commons Clause with the other licences in section 3, is the Commons Clause a "further restriction" within AGPL 10, and what follows from the AGPL 7 sentence that lets a recipient “remove that term”?
* **Q5.** Does the `LICENSE.md` paragraph on dependants have any effect on third-party components or on the sibling packages? Is there any combination among the licences in section 3 that the AGPL or the Commons Clause prevents?
* **Q6.** If the redevops-connectors bytes at v0.1.0 are plain AGPL-3.0-or-later (Q7), what follows for the combined binary and for the Commons Clause?

### 4.3 Sibling packages without licence files (D-LIC-2)

Facts (OBSERVED 2026-11-04, `docs/legal/sources/sibling-repositories-facts.txt`): redevops-connectors has one tag, `v0.1.0` (commit
`7fbb9ec`, the pin); its tree has no licence file; its `pyproject.toml` says `AGPL-3.0-or-later`; its README at the tag carries a badge
“License: AGPL-3.0 + Commons Clause” and says “Licensed AGPL-3.0-or-later.” The licence files exist only on `main`, added by commit
`d990280` whose message begins “chore(license): add AGPL-3.0-or-later + Commons Clause (LICENSE INTENT INFERRED - owner to confirm)”
and says “Inferred from the badge, not owner-stated.” `main`'s `LICENSE.md` says: “Earlier versions of this repository were not covered by
this file and are not affected retroactively by it.” knowledge-frontier has tags `v0.1.0` (the pin, excluded from the binary) and
`v0.2.0`; neither has a licence file; `master` has a plain AGPL `LICENSE` added by a commit that says “plain AGPL-3.0-or-later was
inferred (no Commons Clause).” Both repositories are public.

* **Q7.** Which licence governs the redevops-connectors bytes at v0.1.0 that are in the binary? May the owner, as holder, state it now, and in what form (a new tagged release with the files, a notice in the distribution, or both)? Is a notice alone sufficient for the Corresponding Source that is offered?
* **Q8.** The same questions for knowledge-frontier, relevant only if it is put back.

### 4.4 MPL-2.0 components (D-LIC-4)

Facts: certifi 2026.10.19's wheel licence file is only the bundle header (EXACT_TEXTS `certifi-licence`); `cacert.pem` (sha256
`50f79f98...`, 238,856 bytes) is identical in the wheel, in the embedded PyInstaller archive and at upstream tag 2026.10.19 (commit
`39e29682`); the PyPI sdist sha256 is `035c1e85...`. The full MPL-2.0 text is printed in the notices. Five MPL-2.0 crates are statically
linked in the macOS launcher; `selectors` has no licence file in its package or upstream, but its source files carry the MPL notice
(`docs/legal/sources/selectors-0.36.1-lib.rs-header.txt`). The source-offer inputs name the certifi sdist; the offer is not in the
artifacts. MPL-2.0 sections 1.6, 1.7, 1.13, 3.1 to 3.5 are in EXACT_TEXTS (`mpl-*`).

* **Q9.** Is `cacert.pem` inside a PyInstaller archive "Executable Form" of Covered Software (MPL 1.6), and what must be stated to satisfy 3.2 and 3.4: is a sentence in `THIRD_PARTY_NOTICES.txt` naming the sdist URL, hash and tag enough?
* **Q10.** The same for the five statically linked MPL-2.0 crates (including `selectors`, which lacks a licence file) in the launcher; is the generic MPL-2.0 text acceptable for it?

### 4.5 AGPL section 13 (D-LIC-5)

Facts: the sidecar binds `127.0.0.1` (default port 8787; the launcher lets it pick one); the launcher opens the user's browser on a
one-time URL; the API needs a session obtained with that code; a deployment can mount the app under a path behind a tunnel
(`PROJECTS_BASE_PATH`) and a Docker image exists (STATIC, `packaging/sidecar_main.py`, `docs/LOCAL_API_SECURITY.md`). The UI shows no
licence or source statement (OBSERVED).

* **Q11.** Is a user of the Projects UI served on a loopback address of the same machine "interacting with it remotely through a computer network" within AGPL 13?
* **Q12.** Does section 13 apply where the holder distributes unmodified builds and the recipient only runs them? Where the same sidecar is deployed behind a tunnel or in the Docker image?

### 4.6 Source availability and the offer (D-LIC-12, pins-03, pins-04)

Facts: AGPL 1 and 6 (EXACT_TEXTS `agpl-1`, `agpl-6`): 6(a) “accompanied by the Corresponding Source fixed on a durable physical medium
customarily used for software interchange”, 6(b) a written offer “valid for at least three years and valid for as long as you offer
spare parts or customer support for that product model”, 6(d) “offering access from a designated place”. The artifacts carry no source
and no offer. `packaging/SOURCE-OFFER.md` is a template with a placeholder for the offer wording; it lists the first-party commit,
the pins and the third-party sources with hashes. The public repository has no R0-prime commit. The vendored UI is built from the
`redevops-projects` repository; the source commit that built it is recorded in `packaging/ui-bundle-source.json` but is local and unpublished, and the npm lock file is not in this repository (only its git blob id and sha256 are recorded). The Commons Clause says “Any license notice or attribution required by
the License must also include this Commons Clause License Condition notice.”

* **Q13.** Which of AGPL 6(a) to (e) is adequate for installers downloaded as a dmg, an MSI and an NSIS exe? Does 6(a)'s "durable physical medium" language admit a download, and what does 6(d) require by "the same place"?
* **Q14.** What is the Corresponding Source of these packages: the first-party commit, the sibling packages at their pins, the vendored UI (whose build inputs are partly unrecorded), the build scripts and the pinned third-party sources? Is a reference to third-party sources (hash-pinned URLs) enough?
* **Q15.** What must a written offer contain and for how long; does the Commons Clause restrict what a recipient may do with offered source, and must the offer (or the notices) carry the Commons Clause text?
* **Q16.** May the offer rely on public GitHub repositories, and what if they become private or are removed? Must the exact R0-prime commit be public before any binary is distributed?

### 4.7 Appropriate Legal Notices and acceptance (D-LIC-10)

Facts: AGPL 5(d), 0 and 9 as quoted; the UI shows none; the dmg root has no `Licenses/`; Tauri's `licenseFile` option would add an
acceptance page to every installer (STATIC, tauri-bundler 2.10.4).

* **Q17.** Must the launcher window and the Projects UI display Appropriate Legal Notices, and what should they say and where?
* **Q18.** Given AGPL 9, is any acceptance step or EULA needed or advisable in the dmg, NSIS or MSI, bearing in mind Q25 and Q26?

### 4.8 Third-party notice texts (D-LIC-6, -7, -13)

Facts: CPython LICENSE section 2 (EXACT_TEXTS `psf-2`) requires “PSF's License Agreement and PSF's notice of copyright” to be retained “in
Python alone or in any derivative version”; the 3.12.10 LICENSE and the acknowledgements page are printed. In the local macOS
interpreter, version banners show OpenSSL 3.0.16, expat 2.6.3, bzip2 1.0.8 and Tcl/Tk 8.6.14 inside `libpython3.12.dylib`; texts added.
OpenSSL's LICENSE.txt is the Apache License 2.0 at tags 3.0.16 and 4.0.2 and neither tag has a NOTICE file (OBSERVED); Apache section 4
is EXACT_TEXTS `apache-4`. PyInstaller's COPYING is GPL-2.0-or-later with the Bootloader Exception (EXACT_TEXTS `pyinstaller-exception`).
Samples of the MIT, BSD-3-Clause and Unicode-3.0 conditions are EXACT_TEXTS `react-mit`, `bsd3-sample`, `unicode-3-sample`. r-efi 5.2.0
has no licence file; its `AUTHORS` states a triple licence and reproduces the MIT permission notice and the Apache and LGPL notice
headers (EXACT_TEXTS `refi-authors`). Elections not recorded: r-efi (MIT, Apache-2.0 or LGPL-2.1+), self_cell (Apache-2.0 or
GPL-2.0-only), unicode-ident ((MIT or Apache-2.0) and Unicode-DFS-2016). libyaml, libffi and DuckDB's `third_party` texts are printed
as reference notices without proof of linkage. The UI bundle's only third-party banners are React's.

* **Q19.** Is printing the 3.12.10 LICENSE and acknowledgements page in `Licenses/` (and embedded in the sidecar) enough for PSF 2 for both macOS and Windows interpreters? Which copyright line applies?
* **Q20.** For Apache-2.0 section 4 as to OpenSSL 3.0.16 and 4.0.2 (the latter statically in the `cryptography` wheel, which ships its own Apache and BSD licence files): is printing LICENSE.txt enough when no NOTICE file exists?
* **Q21.** Does the Bootloader Exception cover the frozen executable as shipped? What, if anything, must accompany the GPL-2.0-or-later bootloader and the Apache-2.0 hooks?
* **Q22.** Where must MIT, BSD-3-Clause, Zlib, Unicode-3.0 notices appear for binary distribution (in the app bundle only? at the dmg root?), and is one `THIRD_PARTY_NOTICES.txt` with identical texts printed once acceptable?
* **Q23.** Is a licence statement in an `AUTHORS` file (r-efi) sufficient as the text of the MIT/Apache/LGPL grant, and which alternative should be recorded as elected?
* **Q24.** Should the elections for self_cell, unicode-ident and other OR/AND expressions be recorded, and where?
* **Q30.** For the vendored minified UI bundle built in another repository, must the npm lock be part of the Corresponding Source and the notices?

### 4.9 Apple (D-LIC-18)

Facts: the upstream `LICENSE.md` of the objc2 repository at the packaged commits (`docs/legal/sources/objc2-upstream-LICENSE.md-at-
packaged-commit-8852b424.txt`) says “the crates are (usually automatically) derived from Apple SDKs, and that may have implications for
licensing”, “These crates are derived from Apple SDKs shipped with Xcode.” and “From reading the license, it is unclear whether
distributing derived works such as these crates are allowed?” The packages carry only a `license` field and a README badge. Xcode and
Apple SDKs Agreement (excerpts in `docs/legal/sources/apple-xcode-agreement-excerpts.txt`, 2.4, 2.5, 2.7): “macOS applications and
libraries may be distributed without entering into a separate written agreement with Apple so long as such applications and libraries
comply with the terms of this Agreement.”; “You agree not to rent, lease, lend, upload to or host on any website or server, sell,
redistribute, or sublicense the Apple Software and Apple Services, in whole or in part”; “Apple Software” means “the Xcode Developer
Tools and the Apple SDKs”.

* **Q25.** Are the objc2 crates "Apple Software" or derived from it within that agreement, and does distributing a macOS launcher that links them raise obligations under 2.4, 2.5 or 2.7?

### 4.10 Microsoft (pins-08, D-LIC-9)

Facts (documents read 2026-11-04, excerpts in `docs/legal/sources/`; the Windows build does not exist; the copies come from the build
host's Python installation: INFERRED). Microsoft's Visual Studio documentation: “Distribution of the Visual C++ Runtime Redistributable
package, merge modules, and individual binaries is limited to licensed Visual Studio users and is subject to Microsoft Software License
Terms.”; “Subject to the License Terms for the software, you may copy and distribute with your program any of the files within the
following folder and its subfolders except as noted below. You may not modify these files.” The licence of the Visual C++ 2015-2022
Runtime says you may not “provide the software as a stand-alone offering or combined with any of your applications for others to use”.
Universal CRT page: “Local deployment of the Universal CRT is supported, but not recommended”; “On Windows 10 and Windows 11, the
Universal CRT in the system directory is always used, even if an application includes an application-local copy of the Universal CRT.”
The Visual Studio licence's own "Distributable Code" text and the Windows SDK redistributable terms could not be captured (UNVERIFIED).
NSIS COPYING (EXACT_TEXTS `nsis-applicable`, `nsis-cpl-3`) licenses its LZMA module under the Common Public License 1.0 and tauri-bundler
defaults to LZMA; WiX 3 is under the Microsoft Reciprocal License (EXACT_TEXTS `wix-ms-rl-3`); WebView2 is downloaded by default.

* **Q26.** Which terms govern the VCRUNTIME140*.dll, ucrtbase.dll and api-ms-win-*.dll copies in the sidecar, and what notice or change is required? May the Visual C++ Redistributable be provided as an installer prerequisite given the "combined with any of your applications" clause? May the app-local UCRT DLLs be omitted?
* **Q27.** What obligations arise from the NSIS licences (zlib/libpng, bzip2, CPL-1.0 for LZMA) and the WiX MS-RL for the installers; what notice set; are WebView2's terms engaged?

### 4.11 Assets, wording and the QA bundle (D-LIC-8, -11, -15, -16)

Facts: the former `hero.jpg` carried C2PA metadata naming "Black Forest Labs API"; the shipped replacement is drawn by a script
(`scripts/make_neutral_hero.py`) written and run by an AI coding agent. Rights in the launcher icons are not recorded. The shipped
outreach template says "open-source runtime stack". The QA bundle is a separate product (`docs/legal/QA_BUNDLE_HANDOFF.md`).

* **Q28.** Can the owner treat the script-drawn placeholder as first-party artwork it may license under the family licence?
* **Q29.** What should the owner record about the launcher icons?
* **Q31.** Does calling the product "open-source" in a shipped template, against `LICENSE.md`, create any issue?
* **Q32.** For the QA bundle: what is needed for the HTML Dog-derived tab styles (terms quoted in the hand-off), tablesorter's MIT election, and the plain-AGPL `agent-harness` tree inside a family-licence bundle?

## 5. What has not been verified

The binaries were not scanned for linked libraries beyond version banners in one interpreter build; the CI interpreter (python.org
build) was not examined; Windows contents are read from the frozen R0 listing, not from a build; the Visual Studio licence's
Distributable Code text, the Windows SDK terms and the WebView2 terms were not obtained; which NSIS or WiX files end up in an
installer is unknown; the objc2 2026 copyright line was not checked against the packaged versions; whether the UI bundle contains
third-party code without a banner is unknown; no repository owner or third party was contacted.

## 6. Index of the evidence

`docs/legal/EXACT_TEXTS.md` (texts, with file hashes); `docs/legal/sources/` (excerpts and facts, `MANIFEST.json`);
`packaging/notices/texts/` (shipped reference texts, `MANIFEST.json` with URL and sha256); `LICENSING.md`;
`packaging/DECISIONS-REQUIRED.md` (register); `docs/legal/DECISION_PACKAGES.md`; `docs/legal/OWNER_DECISION_FORM.md`;
`packaging/distribution-gate.json`.
