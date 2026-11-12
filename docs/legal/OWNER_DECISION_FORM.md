# Owner decision form: R0-prime distribution

This is the written record that the later distribution gate requires (`packaging/check-distribution-gate.py`,
`packaging/distribution-gate.json`). It is generated from the gate file (`scripts/build_owner_decision_form.py`); do not
edit this template, copy it. It is a form, not a decision: **no box is ticked here, and nothing in this repository says that
any component or distribution is cleared, compliant or permitted.** Only the owner can decide. Where a decision is marked
`counsel` or `owner+counsel`, counsel's written answer to the numbered questions in `docs/legal/COUNSEL_BRIEF.md` must exist
first; its reference goes in the counsel line.

How to use it:

1. Copy this file, fill in the header, tick one option per decision (or write another option in the free text), sign and date.
2. Store the signed copy (a PDF or a signed file) outside the build tree or under `docs/legal/decisions/`.
3. For each decided item, edit `packaging/distribution-gate.json`: `status` DECIDED, `chosen_option`, `decision_reference`
   (path or URL of the signed copy), `decision_sha256` (sha256 of that file), `decided_by`, `decided_on`, and `counsel_reference`
   when counsel decides. The gate refuses a DECIDED entry that lacks any of these.
4. A build with `REDEVOPS_DISTRIBUTION_BUILD=1` passes only when every blocking decision that applies to its platform is DECIDED.

Header (to be completed by the owner)

| Field | Entry |
|---|---|
| Owner (name, and the legal person on whose behalf this is signed) | |
| R0-prime commit and artifact names under decision | |
| Date of this form | |
| Counsel (name, firm), if consulted | |

## Part 1: distribution-blocking decisions

### D-LIC-1: Product licence wording, copyright line, installer acceptance

* Decides: **owner+counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Is 'source-available (family licence: AGPL-3.0-or-later with the Commons Clause)', SPDX LicenseRef-AGPL-3.0-or-later-with-Commons-Clause and the copyright line 'Copyright (c) 2025-2026 RedevOps.' the statement the distributed packages carry, and is RedevOps (the named copyright holder and Licensor) the right legal person?
* Ships meanwhile (private candidate): the statement as in LICENSING.md; texts in Licenses/
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q1, Q2, Q3
* Evidence and consequences: [D-LIC-1](DECISION_PACKAGES.md#d-lic-1) in the decision packages

Options (tick one):

- [ ] (a) Keep the short statement and the copyright line as written (LICENSING.md section 1).
- [ ] (b) Replace by a longer statement and/or a different copyright holder line drafted by counsel.
- [ ] (c) Change the licence the packages carry (a different family licence or plain AGPL-3.0-or-later).
- [ ] (o) Owner decision without counsel: keep the short licence statement and the copyright line as written (LICENSING.md section 1, LICENSE.md); no counsel wording.
- [ ] other (write it below)

Recommended default: **(a)**. For the private candidate. Counsel confirms (a) or drafts (b) before any distribution; the legal person behind 'RedevOps' is a fact only the owner can state.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-2: Inferred licences of redevops-connectors and knowledge-frontier (pinned tags carry no licence file)

* Decides: **owner**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: DECIDED.
* Question: What licence does the owner state, in writing, for the redevops-connectors bytes at tag v0.1.0 (the pin; its tag has no licence file and the repository's later LICENSE.md says earlier versions 'were not covered by this file') and for knowledge-frontier, and how is that statement attached to the distributed source?
* Ships meanwhile (private candidate): private candidate only: connectors at v0.1.0 with no licence text (listed in NOTICES_MISSING.md); knowledge-frontier excluded from the binary
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q7, Q8, Q4
* Evidence and consequences: [D-LIC-2](DECISION_PACKAGES.md#d-lic-2) in the decision packages

Options (tick one):

- [ ] (a) Owner states the licence intent in writing; the maintainers add the licence files in a NEW tagged release of each repository; R0-prime pins the new tag (new tags are an owner action; the pin, the wheelhouse, the SBOM, the source offer and the candidate change; neither package is a benchmarked component).
- [ ] (a2) Owner states the licence intent in writing and it is recorded in the notices only; the pinned tag stays as it is (the corresponding source for the shipped bytes then still has no licence file; counsel question Q7).
- [ ] (b) Exclude redevops-connectors from the distribution (as knowledge-frontier already is): the Projects UI then shows the 6-provider sample instead of the 14 provider setup guides (measured: tests/test_connectors_removal_cost.py).
- [ ] (c) Ship the packages privately only (no distribution) until (a) is done.
- [ ] other (write it below)

Recommended default: **(a)**. Before any distribution; (c) until then. Connectors cannot be replaced by another component in R0-prime.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-3: Commons Clause term in one distribution together with plain-AGPL, permissive and other-licence code

* Decides: **counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: May the Commons Clause condition stay in the family code of a binary that also contains code under plain AGPL-3.0 (if any sibling is), permissive licences, MPL-2.0 file-level copyleft and GPL-2.0-or-later with the Bootloader Exception, in light of AGPL sections 7 and 10?
* Ships meanwhile (private candidate): all family-licence code plus the third-party texts, with the family statement; private candidate only
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q4, Q5, Q6
* Evidence and consequences: [D-LIC-3](DECISION_PACKAGES.md#d-lic-3) in the decision packages

Options (tick one):

- [ ] (a) Counsel opinion that the family arrangement is sound for the packages as built (the owner then records it).
- [ ] (b) Separate any plain-AGPL component from the distribution (done for knowledge-frontier) and restrict the family code to what counsel accepts.
- [ ] (c) Relicense the siblings consistently, or drop the Commons Clause for the packages (owner's business decision).
- [ ] (d) No distribution until counsel has opined.
- [ ] (o) Owner decision without counsel: keep AGPL-3.0-or-later with the Commons Clause for the core and the sibling packages as built (knowledge-frontier stays out of the frozen sidecar); the owner accepts the Commons Clause term in one distribution beside plain-AGPL, permissive and other-licence code on the owner's own responsibility, without a counsel opinion.
- [ ] other (write it below)

Recommended default: **(d)**. Counsel decides; this work does not conclude. The default is the safe one: private candidate only.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-4: MPL-2.0 components: certifi cacert.pem and five statically linked crates

* Decides: **counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: What must accompany distribution of certifi's cacert.pem (inside both executables) and of the MPL-2.0 crates statically linked into the launcher (cssparser, cssparser-macros, dtoa-short, option-ext, selectors) to meet MPL-2.0 section 3.2, and is a source-location statement in the Licenses folder sufficient?
* Ships meanwhile (private candidate): MPL-2.0 text and each component's own file in THIRD_PARTY_NOTICES.txt; no source-form statement
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q9, Q10
* Evidence and consequences: [D-LIC-4](DECISION_PACKAGES.md#d-lic-4) in the decision packages

Options (tick one):

- [ ] (a) Ship the MPL-2.0 text (done) plus a statement of where the Source Code Form of each MPL component is (certifi: PyPI sdist sha256 035c1e85..., tag 2026.10.19 commit 39e29682; the crates: crates.io packages), worded by counsel.
- [ ] (b) Replace certifi by the operating-system trust store (a behaviour change; not tested; certifi stays pinned per owner decision 6).
- [ ] (c) No distribution until counsel has worded (a).
- [ ] (o) Owner decision without counsel: ship the MPL-2.0 text and a source-location statement worded by the owner (certifi: PyPI sdist and tag; the crates: crates.io packages); no counsel wording.
- [ ] other (write it below)

Recommended default: **(a)**. Wording by counsel; the facts for the statement are in the decision package.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-5: AGPL section 13 for a loopback-served UI

* Decides: **counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Does AGPL section 13 ('users interacting with it remotely through a computer network') apply to the Projects UI that the sidecar serves on 127.0.0.1 of the same machine, to a mounted/tunnelled deployment of the same sidecar, or not at all to unmodified builds?
* Ships meanwhile (private candidate): no UI statement
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q11, Q12
* Evidence and consequences: [D-LIC-5](DECISION_PACKAGES.md#d-lic-5) in the decision packages

Options (tick one):

- [ ] (a) No statement in the product (only if counsel concludes section 13 is not engaged).
- [ ] (b) Add an About/Licence item to the UI that links the source location (also serves D-LIC-10).
- [ ] (c) Counsel opinion first, then (a) or (b).
- [ ] (o) Owner decision without counsel: add an About/Licence item to the UI that links the source location, as a precaution, without concluding whether section 13 applies.
- [ ] other (write it below)

Recommended default: **(c)**. Counsel first. This work does not conclude.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-6: CPython, OpenSSL, PyInstaller and statically linked libraries: notice texts and what is linked

* Decides: **owner+counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Are the texts now in THIRD_PARTY_NOTICES.txt (PSF-2.0 with the bundled-software acknowledgements, Apache-2.0 for OpenSSL 3.0.16 and 4.0.2, GPL-2.0-or-later with the Bootloader Exception for PyInstaller, libyaml/libffi/DuckDB reference notices) sufficient, given that which libraries are linked is not readable from the binaries?
* Ships meanwhile (private candidate): THIRD_PARTY_NOTICES.txt including the REFERENCE NOTICES section
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q19, Q20, Q21, Q22, Q24
* Evidence and consequences: [D-LIC-6](DECISION_PACKAGES.md#d-lic-6) in the decision packages

Options (tick one):

- [ ] (a) Accept the collected texts; record that linkage and exact versions are UNVERIFIED (counsel: Q19-Q22).
- [ ] (b) First scan the frozen archive and the extension binaries to settle what is linked (engineering work), then (a).
- [ ] (c) No distribution until (b).
- [ ] (o) Owner decision without counsel: accept the collected texts; linkage and exact versions are recorded as UNVERIFIED.
- [ ] other (write it below)

Recommended default: **(a)**. With counsel's confirmation of Q19-Q22; (b) is optional hardening.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-8: hero.jpg placeholder: provenance statement

* Decides: **owner**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner confirm that the neutral generated placeholder (drawn by scripts/make_neutral_hero.py, no model output, no photograph) may be treated as first-party artwork under the family licence?
* Ships meanwhile (private candidate): no hero image: it is removed from the product (owner direction of 2026-11-11, option c; the decision becomes a record only when the owner signs the approval)
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q28
* Evidence and consequences: [D-LIC-8](DECISION_PACKAGES.md#d-lic-8) in the decision packages

Options (tick one):

- [ ] (a) Confirm: first-party artwork under the family licence.
- [ ] (b) Supply an image whose rights the owner holds and record its provenance.
- [ ] (c) Remove the image from the product.
- [ ] other (write it below)

Recommended default: **(a)**. For the candidate; (b) when an asset exists.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-9: NSIS, WiX and WebView2 notices (Windows installers)

* Decides: **owner+counsel**. Applies to: windows. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: What notices must the Windows installers (NSIS .exe with LZMA compression by default, WiX MSI) carry for the installer-tool code that ends up inside them, and is the Microsoft Edge WebView2 bootstrapper (downloaded at install time) outside the redistribution?
* Ships meanwhile (private candidate): crate texts; NSIS/WiX texts now in the Windows notices; no Windows build exists yet
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q27
* Evidence and consequences: [D-LIC-9](DECISION_PACKAGES.md#d-lic-9) in the decision packages

Options (tick one):

- [ ] (a) Counsel reads the NSIS (zlib/libpng, bzip2, CPL-1.0 for LZMA) and WiX (MS-RL) texts and states the notice set; WebView2 terms read.
- [ ] (b) Build the NSIS installer with a non-LZMA compressor, if counsel finds the CPL-1.0 section 3 obligations undesirable (engineering change, untested).
- [ ] (c) No Windows distribution.
- [ ] (o) Owner decision without counsel: ship the NSIS, WiX and WebView2 notices as collected in the Windows notices; no counsel reading.
- [ ] other (write it below)

Recommended default: **(a)**. Before a Windows distribution. macOS is not affected.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-10: Licence acceptance pages and Appropriate Legal Notices in the interactive UI

* Decides: **owner+counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Does AGPL section 5(d) require the launcher and the Projects UI to display Appropriate Legal Notices, and is an installer acceptance page wanted (section 9: acceptance is not required to receive or run a copy)?
* Ships meanwhile (private candidate): Licenses/ folder only
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q17, Q18
* Evidence and consequences: [D-LIC-10](DECISION_PACKAGES.md#d-lic-10) in the decision packages

Options (tick one):

- [ ] (a) Files only (Licenses/ folder); no acceptance page; no UI notice.
- [ ] (b) Add a short notice file as the installer licenseFile (acceptance step on every installer).
- [ ] (c) Add an About/Licence entry (copyright, no-warranty, conveyance, how to view the licence) to the launcher or Projects UI.
- [ ] (o) Owner decision without counsel: add an About/Licence entry (copyright, no-warranty, conveyance, how to view the licence) to the launcher or Projects UI and keep the Licenses/ folder.
- [ ] other (write it below)

Recommended default: **(c)**. Counsel confirms; (c) also answers D-LIC-5(b).

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-11: Launcher icons and the 'Re' mark

* Decides: **owner**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner confirm that icons/* are first-party artwork under the family licence (matrix NAT-AS-icons: authorship/rights not recorded)?
* Ships meanwhile (private candidate): temporary neutral artwork generated from code (scripts/make_neutral_icons.py), not the previous mark; LICENSING.md section 4 does not list the icons as covered
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q29
* Evidence and consequences: [D-LIC-11](DECISION_PACKAGES.md#d-lic-11) in the decision packages

Options (tick one):

- [ ] (a) Confirm first-party artwork under the family licence.
- [ ] (b) Exclude the icons from the licence statement and record their actual terms.
- [ ] other (write it below)

Recommended default: **(a)**. Owner fact.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-12: Source availability for the object code (AGPL section 6)

* Decides: **owner+counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Which of AGPL section 6(a)-(e) does the owner choose for the dmg/MSI/NSIS downloads, and what does the artifact carry (an accompanying source archive, a written offer, or network access naming repository, commit and tags)?
* Ships meanwhile (private candidate): repository pointer in LICENSING.md only; SOURCE-OFFER.md is NOT inside the .app or dmg (OBSERVED: 0 listings)
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q13, Q14, Q16
* Evidence and consequences: [D-LIC-12](DECISION_PACKAGES.md#d-lic-12) in the decision packages

Options (tick one):

- [ ] (a) 6(a): ship a machine-readable Corresponding Source archive next to each installer (first-party commit, sibling packages at their pins, third-party sources by reference or copy). No hosting dependency.
- [ ] (b) 6(b): a written offer valid for the period the text prescribes (counsel words it; a copy is in the artifact).
- [ ] (c) 6(d): network access to the Corresponding Source from the same place (repository, commit and tags named; requires the R0-prime commit to be public: pins-04).
- [ ] (d) Counsel chooses.
- [ ] (o) Owner decision without counsel: source availability by AGPL section 6(d): the Corresponding Source is offered from the same place as the object code (the repository, release commit and tags named in the source-offer text; the release commit is public).
- [ ] other (write it below)

Recommended default: **(d)**. Counsel chooses; engineering can produce (a) from the build inputs.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-13: Components whose package holds no licence file: accept the per-item evidence table

* Decides: **owner**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner accept the per-item evidence table of docs/legal/DECISION_PACKAGES.md (r-efi AUTHORS, objc2 family, selectors, alloc-stdlib, webview2-com*, libyaml, libffi, DuckDB tree) as the notice set, with the UNVERIFIED notes?
* Ships meanwhile (private candidate): all texts found are printed; NOTICES_MISSING lists redevops-connectors only
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q23, Q24
* Evidence and consequences: [D-LIC-13](DECISION_PACKAGES.md#d-lic-13) in the decision packages

Options (tick one):

- [ ] (a) Accept the table; record the elections (r-efi, self_cell, unicode-ident) in the notices.
- [ ] (b) Ask the upstream projects for licence files and wait.
- [ ] (c) Replace or exclude any item the owner does not accept (not possible for the Tauri/objc2 stack on macOS).
- [ ] other (write it below)

Recommended default: **(a)**. Most items are now resolved by fact; the only component still without any text is redevops-connectors (D-LIC-2).

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-15: Shipped text that calls the product 'open source'

* Decides: **owner**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner change agentic_os/world/outreach.py ('an open-source runtime stack') to the family wording?
* Ships meanwhile (private candidate): unchanged
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q31
* Evidence and consequences: [D-LIC-15](DECISION_PACKAGES.md#d-lic-15) in the decision packages

Options (tick one):

- [ ] (a) Change the template to 'source-available (family licence: AGPL-3.0-or-later with the Commons Clause)'.
- [ ] (b) Leave it.
- [ ] other (write it below)

Recommended default: **(a)**. The owner's own wording rule.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-16: QA bundle (redevops-qa): hand-off

* Decides: **owner+counsel**. Applies to: qa. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Which of the QA bundle's open items does the owner decide before any distribution of that bundle (report.css HTML Dog tab styles, tablesorter election, declared-only items, plain-AGPL agent-harness)?
* Ships meanwhile (private candidate): QA bundle untouched
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q32
* Evidence and consequences: [D-LIC-16](DECISION_PACKAGES.md#d-lic-16) in the decision packages

Options (tick one):

- [ ] (a) Decide each item in redevops-qa per docs/legal/QA_BUNDLE_HANDOFF.md.
- [ ] (b) Do not distribute the QA bundle.
- [ ] (o) Owner decision without counsel: do not distribute the QA bundle.
- [ ] other (write it below)

Recommended default: **(b)**. Separate repository; nothing is changed here.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### D-LIC-18: objc2 crate family: derived from Apple SDKs

* Decides: **owner+counsel**. Applies to: macos. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Does distributing a macOS launcher that links crates which their repository says are 'derived from Apple SDKs shipped with Xcode' (and about which it says 'it is unclear whether distributing derived works such as these crates are allowed') raise obligations under the Xcode and Apple SDKs Agreement?
* Ships meanwhile (private candidate): LICENSE.md of upstream shown with the standard texts; the Apple SDK question is open
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q25
* Evidence and consequences: [D-LIC-18](DECISION_PACKAGES.md#d-lic-18) in the decision packages

Options (tick one):

- [ ] (a) Accept the upstream statement and the crates' declared licences; record the question as accepted risk.
- [ ] (b) Counsel reads the Xcode and Apple SDKs Agreement (excerpts in docs/legal/sources) before any macOS distribution.
- [ ] (c) Replace the dependency (not possible without leaving Tauri on macOS).
- [ ] (o) Owner decision without counsel: accept the upstream statement and the crates' declared licences; the Apple SDK question is accepted as the owner's risk.
- [ ] other (write it below)

Recommended default: **(b)**. Counsel before any macOS distribution.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### pins-03: Commons Clause and the content of a source offer

* Decides: **counsel**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: What must a source offer contain and how long must it last; does the Commons Clause restrict what a recipient may do with offered source and must the offer say so; how does the offer treat third-party copyleft components in the same product?
* Ships meanwhile (private candidate): SOURCE-OFFER.md template only, not inside the artifacts
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q15, Q16
* Evidence and consequences: [pins-03](DECISION_PACKAGES.md#pins-03) in the decision packages

Options (tick one):

- [ ] (a) Counsel words the offer (packaging/SOURCE-OFFER.md has a bracketed placeholder for it).
- [ ] (b) No distribution until (a).
- [ ] (o) Owner decision without counsel: the owner words the source offer (the About slot LEGAL.SOURCE_OFFER, signed with this decision); no counsel wording.
- [ ] other (write it below)

Recommended default: **(a)**. Counsel.

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

### pins-04: The exact R0-prime commit must exist publicly before it is referenced

* Decides: **owner**. Applies to: all. Blocks distribution: yes. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner publish the exact R0-prime commit (branch, tag or release) that the source reference names, before any binary is distributed?
* Ships meanwhile (private candidate): nothing is published; the references are to a local commit
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q16
* Evidence and consequences: [pins-04](DECISION_PACKAGES.md#pins-04) in the decision packages

Options (tick one):

- [ ] (a) Publish the commit and tag it (needs push authorization, not granted to any agent).
- [ ] (b) Reference a different public form of the source (counsel).
- [ ] other (write it below)

Recommended default: **(a)**. Owner action after push authorization.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### pins-08: Microsoft redistributable DLLs in the Windows sidecar (VCRUNTIME140*, ucrtbase, api-ms-win-*)

* Decides: **counsel**. Applies to: windows. Blocks distribution: yes. Status in the gate file when this form was generated: OPEN.
* Question: Which Microsoft terms govern the copies of VCRUNTIME140.dll, VCRUNTIME140_1.dll, ucrtbase.dll and the 50 api-ms-win-* DLLs inside the Windows sidecar, and what notice or change does counsel require?
* Ships meanwhile (private candidate): Windows build does not exist yet; no text shipped for these files
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q26
* Evidence and consequences: [pins-08](DECISION_PACKAGES.md#pins-08) in the decision packages

Options (tick one):

- [ ] (a) Counsel reads the two Microsoft regimes (Visual Studio 'Distributable Code' versus the 'Visual C++ 2015-2022 Runtime' licence) and states the notice set.
- [ ] (b) Engineering: build the Windows sidecar without the app-local UCRT DLLs (Microsoft documents that on Windows 10 and 11 the system UCRT is always used); needs a Windows test; not done.
- [ ] (c) Require the Visual C++ Redistributable as an installer prerequisite (central deployment); counsel checks that the 'combined with any of your applications' clause permits the way it is provided.
- [ ] (d) Distribute macOS only until counsel has answered.
- [ ] (o) Owner decision without counsel: distribute the Windows sidecar with the Microsoft runtime DLLs it contains and the notices as collected; no counsel reading.
- [ ] other (write it below)

Recommended default: **(d)**. With counsel on (a).

Owner's written decision (free text, reasons, conditions):

> 

Counsel's written answer (reference, date): ______________________________

Date: ______________   Signature: ______________________________

---

## Part 2: other owner decisions (do not block the gate)

### D-LIC-7: React and other JavaScript in the UI bundle

* Decides: **owner**. Applies to: all. Blocks distribution: no. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner accept React 18.3.1 / react-dom / scheduler MIT (text now shipped) as the complete set of third-party code in the vendored UI bundle, and record the redevops-projects commit and npm lock that produced it?
* Ships meanwhile (private candidate): React MIT text
* Counsel questions in `docs/legal/COUNSEL_BRIEF.md`: Q22, Q30
* Evidence and consequences: [D-LIC-7](DECISION_PACKAGES.md#d-lic-7) in the decision packages

Options (tick one):

- [ ] (a) Accept React-only (the bundle's only @license / Copyright strings are the four React banners).
- [ ] (b) Record the redevops-projects tag/commit and npm lock and generate the UI notices from it.
- [ ] other (write it below)

Recommended default: **(b)**. Cheap and removes the UNVERIFIED.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-14: Removal of knowledge-frontier from the frozen sidecar

* Decides: **owner**. Applies to: all. Blocks distribution: no. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner keep knowledge-frontier excluded from the frozen sidecar?
* Ships meanwhile (private candidate): excluded
* Evidence and consequences: [D-LIC-14](DECISION_PACKAGES.md#d-lic-14) in the decision packages

Options (tick one):

- [ ] (a) Keep excluded (default).
- [ ] (b) Restore it once D-LIC-2 is decided for it.
- [ ] other (write it below)

Recommended default: **(a)**. Excluded is the safe state.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

### D-LIC-17: Shipped-but-unexercised components

* Decides: **owner**. Applies to: all. Blocks distribution: no. Status in the gate file when this form was generated: DECIDED.
* Question: Does the owner accept the engineering resolution: setuptools, packaging and _distutils_hack left out of the frozen binary (proven); pycparser not in the archive; duckdb kept (documented durable ledger backend)?
* Ships meanwhile (private candidate): (after merge) setuptools/packaging/_distutils_hack not shipped; duckdb kept
* Evidence and consequences: [D-LIC-17](DECISION_PACKAGES.md#d-lic-17) in the decision packages

Options (tick one):

- [ ] (a) Accept (the exclude commit is on r0prime/lic2).
- [ ] (b) Also drop duckdb (behaviour change: no durable single-node backend).
- [ ] other (write it below)

Recommended default: **(a)**. No behaviour change.

Owner's written decision (free text, reasons, conditions):

> 

Date: ______________   Signature: ______________________________

---

## Final signature

I have read the decision packages and the counsel brief for the decisions above. The decisions recorded in this form are mine;
nothing else is decided by it.

Name: ______________________________   Date: ______________   Signature: ______________________________
