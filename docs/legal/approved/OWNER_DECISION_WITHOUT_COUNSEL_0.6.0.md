redevops-legal-decision: 3
bundle: OWNER_DECISION_WITHOUT_COUNSEL_0.6.0
base-commit: c0c90d2e844357c03eace72b19cd988a15755305
base-tree: 3a15f366620c6d9c75eb08066c5f4ce68e5c5720
manifest-sha256: 17750803a158c2eee1832fff574083c239636e8c01f468e4c360dbfda2b36261
counsel-consulted: no
waiver: owner-decision-without-counsel
decision: D-LIC-1 o
decision: D-LIC-10 o
decision: D-LIC-12 o
decision: D-LIC-16 o
decision: D-LIC-18 o
decision: D-LIC-3 o
decision: D-LIC-4 o
decision: D-LIC-5 o
decision: D-LIC-6 o
decision: D-LIC-9 o
decision: pins-03 o
decision: pins-08 o
slot: LEGAL.COPYRIGHT_LINE sha256 469ce08ad4fa48aa650ed0200e87df7f2b404554daaa7059733f2b0e6382828c
slot: LEGAL.LICENCE_STATEMENT sha256 0d8fd1b58e90214821b813fb1d989c86e4bef999529ebfbc1b13e75987548252
slot: LEGAL.NO_WARRANTY sha256 2c93e5651d299eedc052c27908a91987775af45ed665d28e8e61647a543bad1c
slot: LEGAL.SECTION13_NOTICE sha256 2c8b4a685a93d6d1e238e1fecd0598ec8fd4a06174bdc6cbafbe624d94ba1431
slot: LEGAL.SOURCE_OFFER sha256 575daba462bb5766106ce139c6f6e74c40a8ea62241bbba33943f51c246ae6cd

# OWNER DECISION WITHOUT COUNSEL, RELEASE 0.6.0

> **DRAFT - NOT AUTHORIZED.** This document is a draft prepared by an agent. It authorizes nothing by itself, including after it has been read, initialled or
> copied. It becomes the owner's decision only if the owner signs these exact bytes with the legal-decision key and the release gate verifies that
> signature (section 6). **No counsel was consulted and none approved anything in it.** It is not a legal opinion, a counsel approval or a legal
> conclusion, and it contains no counsel reference. The banner stays in the signed bytes on purpose: the signature, not the banner, is the decision.

## 0. What you are deciding

By signing this document once you would, **for release 0.6.0 and the base commit `c0c90d2e844357c03eace72b19cd988a15755305`** (tree `3a15f366620c6d9c75eb08066c5f4ce68e5c5720`):

1. **Decide 12 decisions that the release gate names as counsel decisions** (D-LIC-1, D-LIC-10, D-LIC-12, D-LIC-16, D-LIC-18, D-LIC-3, D-LIC-4, D-LIC-5, D-LIC-6, D-LIC-9, pins-03, pins-08) yourself, with the options in section 1, **without counsel**. You have stated that no counsel exists and that these decisions are yours; the gate would otherwise keep them OPEN until a counsel reference exists.
2. **Approve 5 About-slot texts** (section 3): LEGAL.COPYRIGHT_LINE, LEGAL.LICENCE_STATEMENT, LEGAL.NO_WARRANTY, LEGAL.SECTION13_NOTICE, LEGAL.SOURCE_OFFER. Each is authorized by its sha256 line in the signed header; no separate signature is needed.
3. **State the owner statements of section 2 as your own decisions and accept the risks of section 4.**

**What this changes in the gate:** each listed entry becomes DECIDED with the `counsel_reference` `owner-waiver:<sha256 of this signed document>`, so that anyone reading the gate file sees that the decision was made by the owner without counsel and which document records it. The gate still checks your signature over these exact bytes.

**What this does not change:** the legal questions behind these decisions (section 4) remain unanswered by any lawyer. Nothing here says that the Commons Clause and the AGPL are compatible, that any notice set is sufficient, or that any distribution is permitted. The tag `v0.6.0` and the first approval (`RELEASE_OWNER_APPROVAL_0.6.0.md`) are not changed.

## 1. Decisions and chosen options

| Gate id | Decision | Decides (in the gate) | Chosen option | What the option means | Counsel questions left unanswered |
|---|---|---|---|---|---|
| D-LIC-1 | Product licence wording, copyright line, installer acceptance | owner+counsel | **o** | Owner decision without counsel: keep the short licence statement and the copyright line as written (LICENSING.md section 1, LICENSE.md); no counsel wording. | Q1, Q2, Q3 |
| D-LIC-10 | Licence acceptance pages and Appropriate Legal Notices in the interactive UI | owner+counsel | **o** | Owner decision without counsel: add an About/Licence entry (copyright, no-warranty, conveyance, how to view the licence) to the launcher or Projects UI and keep the Licenses/ folder. | Q17, Q18 |
| D-LIC-12 | Source availability for the object code (AGPL section 6) | owner+counsel | **o** | Owner decision without counsel: source availability by AGPL section 6(d): the Corresponding Source is offered from the same place as the object code (the repository, release commit and tags named in the source-offer text; the release commit is public). | Q13, Q14, Q16 |
| D-LIC-16 | QA bundle (redevops-qa): hand-off | owner+counsel | **o** | Owner decision without counsel: do not distribute the QA bundle. | Q32 |
| D-LIC-18 | objc2 crate family: derived from Apple SDKs | owner+counsel | **o** | Owner decision without counsel: accept the upstream statement and the crates' declared licences; the Apple SDK question is accepted as the owner's risk. | Q25 |
| D-LIC-3 | Commons Clause term in one distribution together with plain-AGPL, permissive and other-licence code | counsel | **o** | Owner decision without counsel: keep AGPL-3.0-or-later with the Commons Clause for the core and the sibling packages as built (knowledge-frontier stays out of the frozen sidecar); the owner accepts the Commons Clause term in one distribution beside plain-AGPL, permissive and other-licence code on the owner's own responsibility, without a counsel opinion. | Q4, Q5, Q6 |
| D-LIC-4 | MPL-2.0 components: certifi cacert.pem and five statically linked crates | counsel | **o** | Owner decision without counsel: ship the MPL-2.0 text and a source-location statement worded by the owner (certifi: PyPI sdist and tag; the crates: crates.io packages); no counsel wording. | Q9, Q10 |
| D-LIC-5 | AGPL section 13 for a loopback-served UI | counsel | **o** | Owner decision without counsel: add an About/Licence item to the UI that links the source location, as a precaution, without concluding whether section 13 applies. | Q11, Q12 |
| D-LIC-6 | CPython, OpenSSL, PyInstaller and statically linked libraries: notice texts and what is linked | owner+counsel | **o** | Owner decision without counsel: accept the collected texts; linkage and exact versions are recorded as UNVERIFIED. | Q19, Q20, Q21, Q22, Q24 |
| D-LIC-9 | NSIS, WiX and WebView2 notices (Windows installers) | owner+counsel | **o** | Owner decision without counsel: ship the NSIS, WiX and WebView2 notices as collected in the Windows notices; no counsel reading. | Q27 |
| pins-03 | Commons Clause and the content of a source offer | counsel | **o** | Owner decision without counsel: the owner words the source offer (the About slot LEGAL.SOURCE_OFFER, signed with this decision); no counsel wording. | Q15, Q16 |
| pins-08 | Microsoft redistributable DLLs in the Windows sidecar (VCRUNTIME140*, ucrtbase, api-ms-win-*) | counsel | **o** | Owner decision without counsel: distribute the Windows sidecar with the Microsoft runtime DLLs it contains and the notices as collected; no counsel reading. | Q26 |

Where an option's wording mentions counsel (for example 'counsel words the offer'), this record is still made without counsel: the owner statement of section 2 says what is decided.

## 2. Owner statements

* **D-LIC-1.** The short licence statement and the copyright line stay as written: 'Copyright (c) 2025-2026 RedevOps.' and the family-licence statement of LICENSING.md section 1. The owner states these as the intended wording of the product; no counsel has worded or reviewed them.
* **D-LIC-10.** The launcher or Projects UI shows an About/Licence entry (copyright line, no-warranty text, conveyance, how to view the licence), in addition to the Licenses folder. No acceptance page is added.
* **D-LIC-12.** Source availability follows AGPL section 6(d): the Corresponding Source is offered from the same place as the object code, with the repository, release commit and tags named in the source-offer text (slot LEGAL.SOURCE_OFFER). The binaries must therefore be offered from a place from which the source is equally accessible; the owner is responsible for that when publishing.
* **D-LIC-16.** The QA bundle (redevops-qa) is not distributed with this release.
* **D-LIC-18.** The upstream statement and the objc2 crates' declared licences are accepted. Whether the crates derive from Apple SDK material in a way the Xcode and Apple SDKs Agreement restricts stays unanswered; the owner accepts that risk for macOS distribution.
* **D-LIC-3.** The core and the sibling packages stay under AGPL-3.0-or-later with the Commons Clause, as decided on 2026-11-10 and 2026-11-11 (no relicensing). knowledge-frontier stays out of the frozen sidecar. The owner decides that the Commons Clause term may stand in one distribution beside plain-AGPL, permissive and other-licence code, on the owner's own responsibility. No counsel opinion exists on whether that is sound (Q4 to Q6 stay unanswered).
* **D-LIC-4.** The MPL-2.0 text ships with the product, and the source location of each MPL-2.0 component is stated by the owner in the source offer and the notices: certifi through its PyPI sdist and tag, the five statically linked crates through crates.io. The owner words this; no counsel has.
* **D-LIC-5.** The product shows an About/Licence item that links the source location (slot LEGAL.SECTION13_NOTICE). It is added as a precaution; the owner does not conclude whether AGPL section 13 applies to a loopback-served UI.
* **D-LIC-6.** The collected notice texts for CPython, OpenSSL, PyInstaller and statically linked libraries are accepted as they are. What is linked and the exact versions remain UNVERIFIED; the owner accepts that.
* **D-LIC-9.** The NSIS, WiX and WebView2 notices ship as collected in the Windows notices. The owner has not had them read by counsel and accepts that.
* **pins-03.** The owner words the source offer: slot LEGAL.SOURCE_OFFER, text file docs/legal/approved/source-offer.en.txt, approved by this signature. The wording of the offer is the owner's, not counsel's.
* **pins-08.** The Windows sidecar ships with the Microsoft runtime DLLs it contains (VCRUNTIME140*, ucrtbase, api-ms-win-*) and the notices as collected. Which of the two Microsoft licence regimes governs them is not decided by anyone; the owner accepts that risk for Windows distribution.

## 3. About-slot texts approved by this signature

Each text below is the exact content of the file named, whose sha256 is in the signed header. The product shows it as an approved text only while the file's bytes match.

### LEGAL.COPYRIGHT_LINE

File `docs/legal/approved/copyright-line.en.txt`, sha256 `469ce08ad4fa48aa650ed0200e87df7f2b404554daaa7059733f2b0e6382828c`:

~~~text
Copyright (c) 2025-2026 RedevOps.
~~~

### LEGAL.LICENCE_STATEMENT

File `docs/legal/approved/licence-statement.en.txt`, sha256 `0d8fd1b58e90214821b813fb1d989c86e4bef999529ebfbc1b13e75987548252`:

~~~text
The ReDevOps core platform (the agentic-os control plane, sidecar, launcher, Projects UI and the Agentic Apps desktop distribution that contains them) is source-available (family licence: AGPL-3.0-or-later with the Commons Clause).

SPDX-License-Identifier: LicenseRef-AGPL-3.0-or-later-with-Commons-Clause

Third-party components carry their own licences, listed in the Licenses folder of this installation.
~~~

### LEGAL.NO_WARRANTY

File `docs/legal/approved/no-warranty.en.txt`, sha256 `2c93e5651d299eedc052c27908a91987775af45ed665d28e8e61647a543bad1c`:

~~~text
THERE IS NO WARRANTY FOR THE PROGRAM, TO THE EXTENT PERMITTED BY APPLICABLE LAW. EXCEPT WHEN OTHERWISE STATED IN WRITING THE COPYRIGHT HOLDERS AND/OR OTHER PARTIES PROVIDE THE PROGRAM "AS IS" WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE. THE ENTIRE RISK AS TO THE QUALITY AND PERFORMANCE OF THE PROGRAM IS WITH YOU. SHOULD THE PROGRAM PROVE DEFECTIVE, YOU ASSUME THE COST OF ALL NECESSARY SERVICING, REPAIR OR CORRECTION.

This software is licensed under the terms stated in its licence statement; licensees may convey it under those terms. The licence texts are in the Licenses folder of this installation and can be viewed from this About page.
~~~

### LEGAL.SECTION13_NOTICE

File `docs/legal/approved/section13-notice.en.txt`, sha256 `2c8b4a685a93d6d1e238e1fecd0598ec8fd4a06174bdc6cbafbe624d94ba1431`:

~~~text
If you modify this software and let other users interact with your modified version remotely through a computer network, the licence (section 13 of the GNU Affero General Public License) requires you to offer those users an opportunity to receive the Corresponding Source of your version. The source of this version is at https://github.com/redevops-io/agentic-os.
~~~

### LEGAL.SOURCE_OFFER

File `docs/legal/approved/source-offer.en.txt`, sha256 `575daba462bb5766106ce139c6f6e74c40a8ea62241bbba33943f51c246ae6cd`:

~~~text
The source code of this software is available at no charge from https://github.com/redevops-io/agentic-os. The release commit and tag this build was made from are recorded in the About record of this application.

The sibling packages that are part of this software are at https://github.com/redevops-io/redevops-connectors (tag v0.1.1) and https://github.com/redevops-io/knowledge-frontier (tag v0.2.1).

The third-party components shipped in this build and the place where the source of each is available are listed in SOURCE-OFFER.md of this build.

This offer is valid for as long as this software is offered for download from the same place.
~~~

## 4. Risks you accept by deciding without counsel

* No lawyer has reviewed the licence structure of this release (AGPL-3.0-or-later with the Commons Clause on the core, AGPL-3.0-only applications, third-party licences). A court or a licensor could read it differently from the owner.
* The Commons Clause is an added condition on an AGPL licence; whether AGPL section 7 allows it to stand, and what 'Software' covers, are unanswered (D-LIC-3, N1, N2). The owner accepts the risk of enforcement or non-enforcement.
* The source offer (section 3) and the notice sets for MPL-2.0, NSIS, WiX, WebView2, CPython, OpenSSL, PyInstaller and the Microsoft runtime DLLs are the owner's, and may be insufficient.
* Distribution of the macOS build depends on an accepted risk about Apple SDK material in the objc2 crates (D-LIC-18); distribution of the Windows build on the Microsoft runtime DLL terms (pins-08).
* The owner is responsible for offering the binaries from a place from which the source is equally accessible (D-LIC-12, AGPL section 6(d)).
* Anyone who relies on this release may assert claims that counsel would have flagged; the gate's earlier refusal existed to prevent exactly that, and this decision removes it.

The unresolved legal questions of the counsel brief (`docs/legal/COUNSEL_BRIEF.md`) are not answered by this decision; `docs/legal/COMPATIBILITY_ASSESSMENT.md` is an engineering assessment and not a legal opinion.

## 5. Source and artifacts

| Item | Value |
|---|---|
| Base commit F2 (the commit this document is generated on) | `c0c90d2e844357c03eace72b19cd988a15755305` |
| Its source tree | `3a15f366620c6d9c75eb08066c5f4ce68e5c5720` |
| Decision manifest at F2 | `packaging/release/decision-manifest-0.6.0.json`, sha256 `17750803a158c2eee1832fff574083c239636e8c01f468e4c360dbfda2b36261` |

The release commit rule of the first approval applies: the commit that records this decision differs from F2 in exactly three paths (this document, its signature and the gate file); the signer list does not change.

## 6. How this decision becomes effective

1. You read this document and its sha256 (printed by `scripts/release/authorize_release.sh --waiver`, shown separately from the file).
2. You say explicitly that you approve this exact document.
3. You run `scripts/release/authorize_release.sh --waiver --doc <file> --pubkey <legal-decision public key>` at your own terminal. It works on a private copy, re-checks it against the commit, shows the digest and the key fingerprint, asks you to type both and the signing name of section 7, signs the copy once with `ssh-keygen -Y sign` (namespace `redevops-legal-decision@v1`, agent-held key), writes the document, its signature and the gate entries, asks the release gate itself to verify them, and commits exactly those three paths. It signs nothing else and pushes nothing.
4. `packaging/check-distribution-gate.py` verifies the signature, requires the header lines `counsel-consulted: no` and `waiver: owner-decision-without-counsel`, maps each decision and slot of the signed header to its gate entry, and refuses an entry whose counsel_reference is not exactly `owner-waiver:<sha256 of this document>`.

## 7. Signature block (prefilled DRAFT)

The values below are prefilled from the owner's earlier signed form. **They are not an act of the owner and not a signature.** The date is never prefilled.

* Owner name as signing (from the 2026-11-10 form): `____Alex Rybachek____`  *(prefilled, DRAFT; `authorize_release.sh` makes the owner type exactly this name, so it is part of the signed bytes)*
* Initials: `_AR_`  *(prefilled, DRAFT)*
* Date: `____________`  *(left blank; set at the signing run)*
* Status: **DRAFT - NOT AUTHORIZED**

