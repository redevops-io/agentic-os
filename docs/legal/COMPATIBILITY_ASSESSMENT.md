# Engineering compatibility assessment of the twelve counsel-named decisions (release 0.6.0)

**What this is.** A review of each counsel-named gate decision against the licence files, the component inventory, the ownership records and the owner's signed instructions, written by engineering. It lists the facts, the reading the facts support, the least restrictive path that engineering can see, and the question that is still open.

**What this is not.** It is not a legal opinion and it is not counsel's approval. It does not mark any decision as decided, it supplies no counsel reference, and the release gate (`packaging/check-distribution-gate.py`) keeps refusing a distribution build for every decision below until a valid record exists. Where a decision needs a qualified legal opinion, this document says so and does not guess at the opinion.

**Inputs.** `LICENSE.md`, `LICENSING.md` (schedules A, B, C), `LICENSE-COMMONS-CLAUSE`, `docs/legal/COMPONENT_LICENCE_INVENTORY.md`, `docs/legal/DECISION_PACKAGES.md`, `docs/legal/COUNSEL_BRIEF.md`, `docs/legal/ARTWORK_PROVENANCE.md`, `packaging/release/decision-manifest-0.6.0.json` (owner direction of 2026-11-11), the owner-signed release approval of 2026-11-11 (release commit `dc78384`, base `ccac76d`: D-LIC-2, D-LIC-7, D-LIC-13, D-LIC-14, D-LIC-15, D-LIC-17, pins-04; superseded by the approval rendered for the later base commit).

**Standing policy (owner, 2026-11-10 and 2026-11-11).** Core: AGPL-3.0-or-later with the Commons Clause (source-available, not open source). The 21 applications: AGPL-3.0-only. Third-party software: its own licences, unchanged. The Commons Clause stays; no relicensing is started; the intended protection against competing redistribution of modified copies is kept. The distinction between the protected core and separately licensed enterprise capabilities is kept.

## 1. Summary table

| Decision | Scope | Engineering position | Open legal question | Can engineering reduce it? |
|---|---|---|---|---|
| D-LIC-1 wording, copyright holder | all | the statement is consistent across metadata (tested); the holder is named `RedevOps` in the licence files and `ReDevOps.io LLC` in the signing identities | whether these are one legal person; whether the short statement suffices | only the owner can state the entity; wording is a drafting question |
| D-LIC-3 Commons Clause beside AGPL, MPL, permissive, GPL-with-exception | all | the owner is the only copyright holder of every first-party file (git history, two identities of the owner); the first-party apps carry plain AGPL, the core carries AGPL plus the Clause | AGPL sections 7 and 10 and 5(c) for a combined binary | no; this is the central question |
| D-LIC-4 MPL-2.0 (certifi, five crates) | all | MPL-2.0 text is shipped; source locations are known; no MPL file was modified | what exact statement satisfies section 3.2 | yes: a source-location statement can be drafted from facts |
| D-LIC-5 AGPL section 13 for the loopback UI | all | unmodified builds; the UI is served to the same machine | whether section 13 is engaged at all | yes: an About entry makes the question moot (see D-LIC-10) |
| D-LIC-6 CPython, OpenSSL, PyInstaller notices | all | the texts are collected; linkage is not readable from the binaries | sufficiency given unverified linkage | yes: a scan of the frozen archive settles linkage (option b) |
| D-LIC-9 NSIS, WiX, WebView2 (Windows) | windows | LZMA module is CPL-1.0, WiX 3 is MS-RL; the WebView2 bootstrapper is downloaded at install time | notice set; whether the bootstrapper is outside the redistribution | yes: a non-LZMA compressor (option b) removes the CPL point |
| D-LIC-10 Appropriate Legal Notices, acceptance page | all | the About record machinery exists and is gate-checked; no acceptance page | whether section 5(d) is satisfied by the launcher alone | yes: an About/Licence entry (option c) |
| D-LIC-12 source for object code (AGPL section 6) | all | the repository, commit and tags are named in `LICENSING.md`; no source or offer is inside the artifacts | which of 6(a) to 6(e) | yes: a Corresponding Source archive generator (option a) |
| D-LIC-16 QA bundle | qa | separate repository, separate product | open items in that bundle | yes: do not distribute it (option b), changes nothing here |
| D-LIC-18 objc2 crates derived from Apple SDKs | macos | the crates' own text calls the question unclear | whether the Xcode and Apple SDKs Agreement is engaged | no; accepting the risk is a business decision |
| pins-03 content of a source offer | all | `packaging/SOURCE-OFFER.md` holds a template with a bracketed placeholder | wording, duration, treatment of third-party copyleft | partly: choosing 6(a) avoids a written offer |
| pins-08 Microsoft redistributable DLLs (Windows) | windows | the sidecar carries app-local copies copied by PyInstaller | which Microsoft terms apply | yes: build without the app-local copies, or macOS and Linux first |

## 2. Decision by decision

### D-LIC-1 Product licence wording and copyright holder
Facts: the statement in `LICENSING.md` section 1 is checked against `pyproject.toml`, the launcher `Cargo.toml`, `tauri.conf.json`, `README.md`, `modules.yaml` and `stack_knowledge.py` by `packaging/notices/check_licence_statements.py`. The Commons Clause file names `Licensor: RedevOps`. The signing identities name `ReDevOps.io LLC`. The owner's signed approval lists this as a fact for the owner to state; it is not decided.
Engineering position: the text is internally consistent. The risk is the entity name, which no file settles.
Least restrictive path: option (a), keep the short statement; the owner states the legal person behind `RedevOps`.
Open: whether `RedevOps` and `ReDevOps.io LLC` are one person (owner fact) and whether (a) is adequate (legal).

### D-LIC-3 The Commons Clause beside AGPL, permissive, MPL and GPL-with-exception code
Facts: every first-party commit is authored by the owner under two git identities (four commits are flagged as written by an AI coding agent and are listed in the inventory). The apps are AGPL-3.0-only; the core is AGPL-3.0-or-later with the Clause. Third-party code in the binaries is permissive, MPL-2.0 (file-level), or GPL-2.0-or-later with the Bootloader Exception (PyInstaller), each under its own licence and unchanged.
Reading the texts: AGPL section 7 says a recipient may remove a further restriction from code that was received under a notice stating AGPL together with that restriction. Section 10 forbids a licensee from imposing further restrictions on AGPL-licensed work they convey. A copyright holder licensing its own code is not itself a licensee of that code. Engineering reads these points as: the sole copyright holder may attach the Clause to its own core; third-party permissive, MPL and exception-licensed code is separately licensed and unaffected; recipients of the core may have a section 7 right to strip the Clause from AGPL-governed parts as they received them. The reading is not a conclusion on whether one binary containing apps under plain AGPL and a core under AGPL plus the Clause is a single combined work whose whole must go under AGPL (section 5(c)), and what the Clause then restrains.
Least restrictive compliant path: keep the arrangement as the owner decided (option a is a counsel opinion, option b is not needed because no third-party plain-AGPL component is in the binaries, knowledge-frontier being excluded). Option c (dropping the Clause) is outside the owner's instruction.
Open: yes, a legal opinion. The gate stays.

### D-LIC-4 MPL-2.0 components
Facts: certifi 2026.10.19 `cacert.pem` (sha256 `50f79f98…0785`) is identical in the wheel, the PyInstaller archive and upstream tag 2026.10.19; no change was made. Five statically linked crates (cssparser, cssparser-macros, dtoa-short, option-ext, selectors) are MPL-2.0 and unmodified. The full MPL-2.0 text is shipped.
Reading: MPL-2.0 section 3.2 requires that recipients can obtain the source of the covered files; for unmodified files a statement of where the source is satisfies this on its face.
Least restrictive path: option (a), a Licenses entry that names the upstream location and version for each component (the facts are in the decision package).
Open: the exact wording is a drafting point for a lawyer.

### D-LIC-5 AGPL section 13 and the loopback UI
Facts: section 13 applies where the user modifies the Program and users interact with it remotely through a network. The shipped builds are unmodified and the UI is served on 127.0.0.1.
Reading: for an unmodified build the section 13 offer duty is not triggered by the licence text; whether a same-machine loopback is "remotely" is unsettled.
Least restrictive path: option (b), an About/Licence entry that links the source location. It costs little, answers D-LIC-10, and makes the section 13 question moot for the shipped build.
Open: a legal opinion if (a) is wanted instead.

### D-LIC-6 CPython, OpenSSL, PyInstaller, statically linked libraries
Facts: PSF-2.0 with acknowledgements, Apache-2.0 for OpenSSL 3.0.16 and 4.0.2, GPL-2.0-or-later with the Bootloader Exception for PyInstaller, and reference notices for libyaml, libffi and DuckDB are in `THIRD_PARTY_NOTICES.txt`. Which libraries are linked cannot be read from the binaries.
Least restrictive path: option (b) first, an engineering scan of the frozen archive and the extension binaries to settle linkage, then (a). The scan is engineering work and is not done in this release preparation.
Open: sufficiency of the notice set with unverified linkage (legal), and the scan (engineering).

### D-LIC-9 Windows installer notices (NSIS, WiX, WebView2)
Facts: the bundler defaults to NSIS with the LZMA module (Common Public License 1.0) and WiX 3.14.1 (MS-RL). The WebView2 bootstrapper is downloaded at install time and is not shipped inside the installer.
Least restrictive path: option (b), a non-LZMA compressor for NSIS, if the CPL-1.0 section 3 obligations are unwanted; the WiX and NSIS texts are already in the Windows notices.
Open: the notice set and the WebView2 point (legal). No Windows distribution is possible before this is closed.

### D-LIC-10 Appropriate Legal Notices and acceptance page
Facts: AGPL section 5(d) requires interactive interfaces to display Appropriate Legal Notices; section 9 says acceptance is not required to receive or run a copy. The product has an About record (five legal slots, gate-checked) and a Licenses folder.
Least restrictive path: option (c), an About/Licence entry in the launcher or the Projects UI (copyright, no warranty, conveyance terms, how to view the licence). No acceptance page.
Open: whether the About entry is sufficient (legal).

### D-LIC-12 Source for the object code (AGPL section 6)
Facts: neither the `.app`, the dmg nor the installers contain source or an offer; `LICENSING.md` names the repository.
Least restrictive path: option (a), a Corresponding Source archive next to each installer, generated from the build inputs (first-party commit, sibling tags v0.3.17, v0.1.21, v0.1.1, v0.2.1 as pinned, third-party sources by lockfile). This removes the written-offer wording problem of pins-03 for the artifact. The generator is engineering work not done in this release preparation.
Open: which section 6 route the owner chooses (a business choice with a legal check).

### D-LIC-16 QA bundle
Facts: a separate repository (`redevops-qa`) and a separate product (`docs/legal/QA_BUNDLE_HANDOFF.md`).
Least restrictive path: option (b), do not distribute the QA bundle with this release. Nothing changes here.
Open: only if the QA bundle is to be distributed.

### D-LIC-18 objc2 crates derived from Apple SDKs
Facts: the upstream crates state that they are derived from Apple SDKs and that it is unclear whether distributing derived works is allowed; their `Cargo.toml` declares MIT or Zlib/Apache/MIT. The Tauri launcher on macOS depends on them.
Reading: engineering cannot resolve a question about the Xcode and Apple SDKs Agreement.
Least restrictive path: option (a), accept the upstream statement and the declared licences as a recorded risk, only if the owner is willing to carry it; there is no practical alternative on macOS without leaving Tauri.
Open: yes, a legal reading of the Apple agreement if the risk is not accepted. The gate stays because the record must carry a counsel reference.

### pins-03 Content of a source offer
Facts: `packaging/SOURCE-OFFER.md` is a template with a bracketed placeholder and is not inside the artifacts.
Reading: if D-LIC-12 is resolved by option (a), the artifact carries the source and no written offer is needed for it; the Commons Clause sentence about notices ("Any license notice or attribution required by the License must also include this Commons Clause License Condition notice") still applies to attribution.
Open: wording, duration and the treatment of third-party copyleft in the same product (legal), unless (a) is chosen.

### pins-08 Microsoft redistributable DLLs (Windows)
Facts: `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `ucrtbase.dll` and 50 `api-ms-win-*` forwarder DLLs come from the build host's Python and are copied by PyInstaller.
Least restrictive path: option (b), build the Windows sidecar without the app-local UCRT DLLs where the target Windows supports the central runtime, or option (d), distribute macOS and Linux first.
Open: which Microsoft terms apply (legal), unless (b) or (d) is taken.

## 3. What would make the gate pass, and who can do it

* Each decision above has a record format that the gate verifies: a record file, a detached SSHSIG by a listed key, and for decisions that name counsel a counsel reference. The set of counsel-named decisions is frozen inside the gate; an owner signature cannot cover them.
* An "authorized alternative" to a qualified legal opinion does not exist in the repository. Creating one means changing the gate's frozen set or its record rules, which is a deliberate change to a safety control. It is not made here. If the owner wants it, the owner has to say so in terms and name the alternative (for example, a named reviewer whose signed reference the gate accepts), and the change needs its own review.
* Engineering actions that would shrink the open questions without an opinion are listed in the table (About entry, Corresponding Source archive, non-LZMA NSIS, no app-local UCRT, macOS-and-Linux first, no QA bundle). None changes a gate status by itself.

## 4. Remaining blockers after this assessment

All twelve decisions stay OPEN. D-LIC-3 and D-LIC-18 need a qualified reading by their nature. The others have an engineering path (above) that lowers the question; choosing a path still requires the decision record the gate verifies.
