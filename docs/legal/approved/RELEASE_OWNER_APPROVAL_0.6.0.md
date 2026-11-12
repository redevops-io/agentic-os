redevops-legal-decision: 2
bundle: RELEASE_OWNER_APPROVAL_0.6.0
base-commit: fdcba4079493a111985d4f8aa13c0eecc4dac5e2
base-tree: 4ea43507682da933128a9c32ebf16c48669b43df
manifest-sha256: c33db05b553712ab804f5d5b86f2dc6420ae5ca4f998fe320560b19759b1c1b7
decision: D-LIC-2 a
decision: D-LIC-7 b
decision: D-LIC-8 c
decision: D-LIC-11 b
decision: D-LIC-13 a
decision: D-LIC-14 a
decision: D-LIC-15 a
decision: D-LIC-17 a
decision: pins-04 a

# RELEASE OWNER APPROVAL 0.6.0

> **DRAFT - NOT AUTHORIZED.** This document is a draft prepared by an agent. It authorizes nothing by itself, including after it has been read, initialled or
> copied. It becomes an authorization only if the owner signs these exact bytes with the legal-decision key and the release gate verifies that
> signature (section 10). The banner stays in the signed bytes on purpose: the signature, not the banner, is the authorization. No counsel approval, legal
> opinion, timestamp or cryptographic signature is contained or implied here.

## 0. Summary: what you are authorizing

By signing this document once you would, **for release 0.6.0 built from the release commit derived from `fdcba4079493a111985d4f8aa13c0eecc4dac5e2`** (tree `4ea43507682da933128a9c32ebf16c48669b43df`):

1. **Record 9 owner-only decisions** (D-LIC-2, D-LIC-7, D-LIC-8, D-LIC-11, D-LIC-13, D-LIC-14, D-LIC-15, D-LIC-17, pins-04) with the options in section 4. They are mapped onto their existing gate identifiers; each stays a separate entry in `packaging/distribution-gate.json`.
2. **State the facts in section 4 as the owner's own** (marked *attests* or *Owner statement*): the owner statements of D-LIC-2, D-LIC-8, D-LIC-11.
3. **Accept the exceptions and risks in section 7** for this release.
4. **Authorize the release workflow in section 6 to run at your terminal**: build macOS, Windows and Linux from the release commit; sign macOS with the Developer ID identity and notarize with the `redevops` profile; sign Windows through Azure Artifact Signing with your own `az` login; apply the Linux policy you choose in P-LIN; and, only after every stage has passed and with your typed confirmation, **push the release commit as the branch `release/0.6.0` and the tag `v0.6.0` to the remote you name** (pins-04). Nothing is uploaded, released or deployed by it.

**You are NOT authorizing**, and this signature cannot supply:

* any decision that names counsel (12 decisions, section 5: D-LIC-1, D-LIC-3, D-LIC-4, D-LIC-5, D-LIC-6, D-LIC-9, D-LIC-10, D-LIC-12, D-LIC-16, D-LIC-18, pins-03, pins-08). They have no counsel reference and stay OPEN; the release gate keeps refusing a distribution build until each has its own record, a counsel reference, and (for the five About slots) a signed text.
* a legal conclusion about the Commons Clause and the AGPL (sections 7 and 10), the apps-versus-core question, contributor terms, or the copyright statement (section 8).
* a distribution: the macOS dmg that is already signed and notarized stays a private candidate; the new build can be distributed only when the gate passes.
* the use of any key, secret or cloud credential by an agent: the workflow runs at your terminal and reads nothing from `~/.ssh`, the Keychain or `~/.azure` except through the tools you run.

**Result if you sign now:** the gate would still refuse a distribution build on every platform because of the counsel decisions in section 5 and the technical blockers in section 9. The signature removes the owner-only paperwork; it does not remove those.

## 1. Owner and company (from verified records)

| Item | Value | Source |
|---|---|---|
| Name on the 2026-11-10 signing form | Alex Rybachek (initials AR) | owner-signed form |
| Git author name in the repository | Alex Mats | `git log` |
| Contact | redevops@redevops.io | session record |
| Company in the signing records | ReDevOps.io LLC, Aventura, Florida, US | Azure identity validation and Developer ID (observed 2026-11-04) |
| Apple signing identity | Developer ID Application: ReDevOps.io LLC (Q8A92DM4T7) | keychain identity, observed 2026-11-04 |
| Azure identity validation | 31b37fcd-8c3a-4280-a402-bad98f4728a8 (ReDevOps.io LLC, valid to 2028-12-27) | Azure Artifact Signing, observed 2026-11-04 |
| Copyright holder in the licence files | RedevOps | `LICENSE.md`, `LICENSING.md` |

* **To confirm by the owner:** Two names appear in the records: the signature name on the 2026-11-10 form and the git author name. The owner states below which name signs this approval; nothing here decides it.
* **To confirm by the owner:** The licence files name 'RedevOps' as copyright holder and Licensor; the signing identities name 'ReDevOps.io LLC'. Whether these are the same legal person is a fact only the owner can state (D-LIC-1); the approval records the question and does not answer it.

## 2. Source, commit and artifacts

| Item | Value |
|---|---|
| Release-preparation commit F (the commit this document is generated on) | `fdcba4079493a111985d4f8aa13c0eecc4dac5e2` |
| Its source tree | `4ea43507682da933128a9c32ebf16c48669b43df` |
| Decision manifest at F | `packaging/release/decision-manifest-0.6.0.json`, sha256 `c33db05b553712ab804f5d5b86f2dc6420ae5ca4f998fe320560b19759b1c1b7` |
| Immutable reference: frozen_c317_commit | `016143ff1a8ab39524d1f042916dcf01298d9d04` |
| Immutable reference: frozen_c317_tree | `71ad4586e1f40c5d989143f504d956487f4f244b` |
| Immutable reference: licensing_candidate_f823e238 | `f823e238cdf28d174343830d86d9e0862bf7f59b` |
| Immutable reference: licensing_alignment_8401311b | `8401311bc4c31cbadb712092a865078820aa95c3` |
| Immutable reference: rc2_fallback_549e6282 | `549e628249cfbb9bd0d4e8c34be3654971c3cf93` |

**Release commit rule.** The approval names the release-preparation commit F (the commit the document is generated on). The release commit R is F plus ONLY four paths: docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md and its .sig (added), packaging/legal-allowed-signers (exactly one signer line added) and packaging/distribution-gate.json (the status fields of the bundled decisions and one fingerprint added to legal_signer_fingerprints). Stage S0 of the orchestrator checks exactly that with git name-status, requires the one added key to be the fingerprint the owner passes in --expect-signer-fingerprint, and has the release gate verify the signature. The gate itself does not bind the signature to F or to a tree (it checks signature, signer, decision ids and options only), so the binding to F is enforced by S0 alone.

**Existing artifacts and evidence (preserved, never modified):**

* frozen c317 unsigned dmg sha256: `d9935d59c3a95fbd09617a687c7de327e0cc140bd659dfbd4a8a852a6d441642`
* signed notarized private dmg sha256: `e32a1075328eec1dc4b002d9430009295a3cf50a4a13ceb11a2df1387872dadd`
* signed dmg status: PRIVATE candidate (distribution=no). It is preserved unchanged and can never become the distributed artifact.
* signing package c317: signing-c317/ revision 4.1, CHECKSUMS sha256 9a71b471201e598e32f2367951d7c980f5a83510a318e39c11377c84d4bf635d, 206 files (preserved)
* records dir: `~/Documents/60_Security_Audits_Code_Review/RELEASE_QUALIFICATION_2026-11-06/`
* backup mirror: `/Volumes/backup/agenticos-0.6.0-rc2-evidence/c317-candidate-v0.3.17/`

**Artifacts to be built from the release commit:** macOS Agentic Apps dmg (arm64) built from R; Windows MSI, NSIS setup exe; Linux .deb, AppImage. Excluded: strict snap (SNAP-D1 unresolved, excluded from the initial 0.6.0 by owner decision).

## 3. Licensing categories and component schedules

| Category | Licence | Where stated |
|---|---|---|
| 1. ReDevOps core platform (Schedule A) | GNU AGPL-3.0-or-later with the Commons Clause: source-available, not OSI open source | `LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md`, `LICENSING.md` |
| 2. The 21 applications under `apps/` (Schedule B1) | GNU AGPL-3.0-only, without the Commons Clause | `apps/<name>/LICENSE`, `apps/README.md` |
| 3. Third-party software (Schedules B2, B3) | their own licences, unchanged | `THIRD_PARTY_NOTICES.txt`, `NOTICES_MISSING.md` |

Separate repositories whose code the artifacts contain are Schedule C. The path-by-path inventory with flags F1 to F9 is `docs/legal/COMPONENT_LICENCE_INVENTORY.md`. Exact files at F:

| File | sha256 |
|---|---|
| `LICENSE` | `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0` |
| `LICENSE-COMMONS-CLAUSE` | `19f3499cb454c45d547b2272566497f80be2b3fa17e305488f62973d08a9aabd` |
| `LICENSE.md` | `6c091485792196a90a8b137aad4e136a48090ec376b54780920814dcef0a3846` |
| `LICENSING.md` | `103fd5c84287cd4c8c1176e3118ac34b4b501947270973d911439b9149803158` |
| `docs/legal/COMPONENT_LICENCE_INVENTORY.md` | `760049a03f86e60a0b9bf8980a1820a1272140681c4122567af8800b3701d1cd` |
| `apps/README.md` | `c00ccaf40e034f31913bd8b8362a039e02c6a7d8397205227ba6fdb537370bd3` |

No per-file SPDX headers are added in this release. The Commons Clause standard text and the AGPL text are unchanged.

## 4. Owner-only decisions (covered by ONE signature)

Each row is recorded in the signed header at the top of this document and maps to the gate entry with the same identifier. *Blocks* says whether the entry blocks a distribution build.

| Gate id | Decision | Chosen option | What the option means | Blocks | Open counsel questions that remain |
|---|---|---|---|---|---|
| D-LIC-2 | Inferred licences of redevops-connectors and knowledge-frontier (pinned tags carry no licence file) | **a** (recommended) | Owner states the licence intent in writing; the maintainers add the licence files in a NEW tagged release of each repository; R0-prime pins the new tag (new tags are an owner action; the pin, the wheelhouse, the SBOM, the source offer and the candidate change; neither package is a benchmarked component). | yes | Q7, Q8, Q4 |
| D-LIC-7 | React and other JavaScript in the UI bundle | **b** (recommended) | Record the redevops-projects tag/commit and npm lock and generate the UI notices from it. | no | Q22, Q30 |
| D-LIC-8 | hero.jpg placeholder: provenance statement | **c** | Remove the image from the product. | yes | Q28 |
| D-LIC-11 | Launcher icons and the 'Re' mark | **b** | Exclude the icons from the licence statement and record their actual terms. | yes | Q29 |
| D-LIC-13 | Components whose package holds no licence file: accept the per-item evidence table | **a** (recommended) | Accept the table; record the elections (r-efi, self_cell, unicode-ident) in the notices. | yes | Q23, Q24 |
| D-LIC-14 | Removal of knowledge-frontier from the frozen sidecar | **a** (recommended) | Keep excluded (default). | no | - |
| D-LIC-15 | Shipped text that calls the product 'open source' | **a** (recommended) | Change the template to 'source-available (family licence: AGPL-3.0-or-later with the Commons Clause)'. | yes | Q31 |
| D-LIC-17 | Shipped-but-unexercised components | **a** (recommended) | Accept (the exclude commit is on r0prime/lic2). | no | - |
| pins-04 | The exact R0-prime commit must exist publicly before it is referenced | **a** (recommended) | Publish the commit and tag it (needs push authorization, not granted to any agent). | yes | Q16 |

* **D-LIC-2.** Owner states the licence intent in writing and the maintainers add licence files in NEW tagged releases of redevops-connectors and knowledge-frontier; R0-prime pins the new tags. Approving (a) does NOT create those tags: they are actions in other repositories (technical blocker T1). *Owner statement:* Licence intent: redevops-connectors and knowledge-frontier are licensed as the family licence (AGPL-3.0-or-later with the Commons Clause) unless the owner writes otherwise here before signing.
* **D-LIC-7.** Record the redevops-projects commit and npm lock and generate the UI notices from it (packaging/ui-bundle-source.json holds the record).
* **D-LIC-8.** Option c, owner direction 2026-11-11: the hero image is removed from the product (file, generator script, and the pinned UI artifact, which is repacked without it: sha256 b4a7d83c...). Nothing is attested for an image that is not distributed. *Owner statement:* The hero image is removed from the product: no hero.jpg ships, the pinned Projects UI artifact has no image member, and no authorship or ownership is attested for it (docs/legal/ARTWORK_PROVENANCE.md section 1). The built UI still contains a sample item that points at /hero.jpg; that picture cannot load.
* **D-LIC-11.** Option b: the launcher icons are excluded from the licence statement (LICENSING.md section 4 does not list them) and their actual terms are recorded as provenance in docs/legal/ARTWORK_PROVENANCE.md (generated neutral artwork, generator and hashes). No authorship or rights are asserted. *Owner statement:* Actual terms recorded: the launcher icons are the recorded output of scripts/make_neutral_icons.py (docs/legal/ARTWORK_PROVENANCE.md), excluded from the licence statement; no rights are asserted and the 'Re' mark is not retained (Q29 stays with counsel).
* **D-LIC-13.** Accept the per-item evidence table for components whose package holds no licence file, with the UNVERIFIED notes.
* **D-LIC-14.** knowledge-frontier stays excluded from the frozen sidecar.
* **D-LIC-15.** The shipped outreach template already reads 'a source-available runtime stack' (no 'open source'); option (a) names the longer family wording. The owner accepts the current wording or asks for the longer form before signing.
* **D-LIC-17.** Accept the engineering resolution on shipped-but-unexercised components (setuptools, packaging, _distutils_hack left out; duckdb kept).
* **pins-04.** Publish the exact release commit and tag it before any binary is distributed. This approval includes the push authorization described in section 6 of the document; the push runs only at the owner's terminal, only after every gate passes.

The gate classes these decisions as owner decisions (the set of counsel-named decisions is frozen inside the gate, not read from the gate file). Several of them list counsel questions (last column); counsel has not answered them and this approval does not claim otherwise. An owner who prefers to wait for counsel on D-LIC-2 (Q7, Q8, Q4) or on any other row can have the document re-rendered without that decision (`approval.py render --strike ID`); the struck decision then stays OPEN and the document has a different digest. A hand-edited document is refused.

## 5. Decisions that name counsel (NOT covered; stay OPEN)

| Gate id | Decides | Decision | Owner-side recommended option | Counsel questions | Counsel reference |
|---|---|---|---|---|---|
| D-LIC-1 | owner+counsel | Product licence wording, copyright line, installer acceptance | a: Keep the short statement and the copyright line as written (LICENSING.md section 1). | Q1, Q2, Q3 | **none** |
| D-LIC-3 | counsel | Commons Clause term in one distribution together with plain-AGPL, permissive and other-licence code | d: No distribution until counsel has opined. | Q4, Q5, Q6 | **none** |
| D-LIC-4 | counsel | MPL-2.0 components: certifi cacert.pem and five statically linked crates | a: Ship the MPL-2.0 text (done) plus a statement of where the Source Code Form of each MPL component is (certifi: | Q9, Q10 | **none** |
| D-LIC-5 | counsel | AGPL section 13 for a loopback-served UI | c: Counsel opinion first, then (a) or (b). | Q11, Q12 | **none** |
| D-LIC-6 | owner+counsel | CPython, OpenSSL, PyInstaller and statically linked libraries: notice texts and what is linked | a: Accept the collected texts; record that linkage and exact versions are UNVERIFIED (counsel: Q19-Q22). | Q19, Q20, Q21, Q22, Q24 | **none** |
| D-LIC-9 | owner+counsel | NSIS, WiX and WebView2 notices (Windows installers) | a: Counsel reads the NSIS (zlib/libpng, bzip2, CPL-1.0 for LZMA) and WiX (MS-RL) texts and states the notice set; | Q27 | **none** |
| D-LIC-10 | owner+counsel | Licence acceptance pages and Appropriate Legal Notices in the interactive UI | c: Add an About/Licence entry (copyright, no-warranty, conveyance, how to view the licence) to the launcher or Pr | Q17, Q18 | **none** |
| D-LIC-12 | owner+counsel | Source availability for the object code (AGPL section 6) | d: Counsel chooses. | Q13, Q14, Q16 | **none** |
| D-LIC-16 | owner+counsel | QA bundle (redevops-qa): hand-off | b: Do not distribute the QA bundle. | Q32 | **none** |
| D-LIC-18 | owner+counsel | objc2 crate family: derived from Apple SDKs | b: Counsel reads the Xcode and Apple SDKs Agreement (excerpts in docs/legal/sources) before any macOS distributio | Q25 | **none** |
| pins-03 | counsel | Commons Clause and the content of a source offer | a: Counsel words the offer (packaging/SOURCE-OFFER.md has a bracketed placeholder for it). | Q15, Q16 | **none** |
| pins-08 | counsel | Microsoft redistributable DLLs in the Windows sidecar (VCRUNTIME140*, ucrtbase, api-ms-win-*) | d: Distribute macOS only until counsel has answered. | Q26 | **none** |

Each needs its own signed record carrying a counsel reference (and, for the five About slots, a signed approved text). A later consolidated approval for these is possible only after counsel has answered; it is not prepared here and nothing downgrades them to owner-only.

## 6. Platform signing decisions and release actions

### P-MAC macOS

* scope: Developer ID Application signing, notarization with the keychain profile, stapling of the dmg only. Reuses the c317 signing script and the scope format byte for byte; the signing package for a distribution run is generated from the release commit and the built dmg by scripts/release/signing_package.py (orchestrator stage S5) and its output is checked by scripts/release/verify_signed.py (S7).
* identity: Developer ID Application: ReDevOps.io LLC (Q8A92DM4T7)
* identity sha256: 8ee6c415e6960823c248718496b89e7e08dcf8414ee95282aeabf2b36b0d7ed3
* team id: Q8A92DM4T7
* notary profile name: redevops
* notary profile sha256: e0378cd483b7c9187c21a64c2a6b6746c93b0eceaef09663d47f6137da856b90
* notarize: True
* staple: dmg only
* distribution: yes (only if the gate passes for macos)
* entitlement: the one sidecar-only library-validation entitlement in packaging/macos/sidecar.entitlements.plist, exactly as it stands (accepted by the owner 2026-11-10, residual risk R1, docs/MACOS_ENTITLEMENTS.md)
* preserved: the c317 signed and notarized private dmg e32a1075... and signing package 4.1 are never modified
* runs at: the owner's Mac terminal (keychain identity and notary profile); no agent runs it

### P-WIN Windows (Azure Artifact Signing)

* scope: Authenticode signing of the launcher, sidecar and NSIS uninstaller (stage inner) and then of the MSI and NSIS setup exe (stage container) with Jsign 7.6 against Azure Artifact Signing, RFC 3161 timestamp; script packaging/signing/sign_windows_r0prime_v3.sh --real.
* azure endpoint: https://eus.codesigning.azure.net/
* azure account: redevops
* azure certificate profile: redevops-public
* azure expected cn: CN=ReDevOps.io LLC
* azure region: East US
* certificate validity note: Public Trust certificates last 3 days; signatures rely on the Microsoft RFC 3161 timestamp.
* known status: The script's --real path (version r0prime-win-3) has never run; the earlier v1 script ran once on 2026-11-04 against the same account and profile (3 sign-API calls).
* needs the owner at run time: az login by the owner (an identity holding 'Artifact Signing Certificate Profile Signer'); the owner-authorization sha256 of the scope file: the Windows signing package (scripts/release/signing_package.py build --platform windows) writes the scope draft; the owner fills the window and the host-tool hashes in a copy and passes that copy's sha256
* never in this document: tokens, az state, keys

### P-LIN Linux (recommendation; no mandatory signing requirement exists)

* **a** (recommended): Checksums plus one detached SSHSIG over the release evidence manifest (which lists the sha256 of the .deb and the AppImage), made with the existing release key through the owner's ssh-agent (packaging/signing/sign_manifest_sshsig.sh, approval M-01). The packages themselves are not individually signed. No new key, no repository infrastructure.
* **b**: Option (a) plus a detached GPG signature (.asc) per package. Needs a new GPG key and its publication; not established today.
* **c**: A signed apt repository (Release/InRelease). Needs repository hosting and a signing key; not established today.
* **d**: Checksums only (SHA256SUMS), no signature.
* why (a): It reuses a mechanism that already exists, reviewed and tested, binds every artifact hash to a key the owner already controls, and invents no mandatory requirement. Users verify the manifest signature, then the package hash. Packages are not covered by a native package-signing scheme; that is a stated limitation, not a defect.
* No Linux package-signing requirement has been established by the owner; (a) is a recommendation.

**Chosen:** P-LIN option **a**, P-MAC and P-WIN as above.

### Release actions authorized by a valid signature (and only these)

1. Run `scripts/release/release_orchestrate.sh --run` at the owner's terminal; it stops at the first stage that fails or is blocked, and its first stage (S0) requires the release commit to be F plus exactly the approval files, signed by the key fingerprint you pass in `--expect-signer-fingerprint`. The release gate itself refuses a distribution build whatever the orchestrator does.
2. Build the artifacts of section 2 and verify their checksums.
3. Sign them per P-MAC, P-WIN and P-LIN; each real signing step needs the owner's own keychain, `az` login or ssh-agent, at the owner's terminal.
4. After stages S0 to S7 have passed in the same run, with `--push --remote NAME` and your typed confirmation: tag the release commit `v0.6.0` and push the branch `release/0.6.0` and that tag to the remote you name, with your own git credentials (pins-04). The orchestrator then reads the remote back (`git ls-remote`). Recording the pins-04 decision does not publish anything; the publication happens only at this step.
5. Write the consolidated release manifest and evidence report.

Not authorized: uploading or publishing artifacts, creating a GitHub release, deploying, changing other repositories, using any key an agent can reach.

## 7. Exceptions and accepted risks (complete list)

| Id | Exception or risk | Source |
|---|---|---|
| X-1 | The sidecar-only library-validation entitlement in packaging/macos/sidecar.entitlements.plist (residual risk R1), accepted by the owner for the c317 build on 2026-11-10; re-accepted here for the release build only if the owner signs this approval. | docs/MACOS_ENTITLEMENTS.md; OWNER_DECISIONS_RECORD_2026-11-10.md |
| X-2 | Post-run check 4 of the c317 package (revision 4.1) reported a known false FAIL on Developer-ID-signed bundles (the __LINKEDIT vmsize differs by page rounding after the signature is removed); the owner accepted it for the c317 run. For this release the check is replaced in scripts/release/verify_signed.py: the signature is removed from both the input's and the signed binary and the __LINKEDIT vmsize field is zeroed in both before comparing (removing the signature from both alone was found by the independent review not to be enough). Acceptance on real data, run offline (the Gatekeeper spctl rows were not run) and recorded in RELEASE_SIGNING_PACKAGES_2026-11-11 in the records folder: against the c317 Developer-ID signed pair every signature and content row that does not need the network passes, both executables included, and the only rows that fail are those that show the c317 run was a private candidate and not a distribution run (no enforced distribution gate, the PRIVATE-CANDIDATE statement, the About record in private-candidate mode with pending legal slots). It has not been run on a release build, which does not exist yet. | C317_SIGNING_RUN_RECORD_2026-11-10.md; independent review of the run |
| X-3 | Evidence-manifest SSHSIG (M-01) was ticked 'yes' on the c317 form but never performed; it remains a separate step (Linux option a uses the same mechanism). | OWNER_DECISIONS_RECORD_2026-11-10.md |
| X-4 | LITERALS-PROVENANCE.json is verified for self-consistency only, not against its sources (gate G12 was waived for c317). | AUTHORIZATION_REQUEST.OWNER-COPY.md |
| X-5 | The AppImage bytes depend on the Linux build host's apt state (LNX-C1, measured: 8 gstreamer libraries differ between two builds on different hosts). | reports/c317/native-linux.md |
| X-6 | The Windows launcher is not byte-reproducible across Visual Studio Build Tools patch versions (PE Rich header, measured), because the installer bootstrapper floats. | reports/c317/native-windows.md |
| X-7 | The strict snap is excluded from the initial 0.6.0 (SNAP-D1 unresolved). | owner decision recorded in the release decisions |
| X-8 | The Projects UI source commit that produced the shipped bundle is a local, unpublished commit of the redevops-projects repository; publishing it is part of pins-04. | packaging/ui-bundle-source.json |
| X-9 | Four commits are authored by an AI agent identity and three author identities map to one rightsholder; LICENSE.md's contribution-history sentence is for counsel (N4). | docs/legal/COMPONENT_LICENCE_INVENTORY.md flags F1 and F2 |
| X-10 | The licence statement of the Projects UI repository was not re-read for the core schedule (D-LIC-2, flag F4). | LICENSING.md Schedule A4 |
| X-11 | Windows installers are signed with Public Trust certificates whose own validity is 3 days; trust after expiry depends on the timestamp. | records of the 2026-11-04 Azure signing run |
| X-12 | The authorization command asks the owner to type the document digest and the key fingerprint at a terminal, but it prints both itself, so a process that holds the owner's ssh-agent socket and a pseudo-terminal could answer the prompts. Mitigation: load the legal-decision key with ssh-add -c (confirmation on every use) and run the command only in the owner's own terminal. Residual risk accepted. | independent review of the release tooling, finding 9 |
| X-13 | Non-blocking owner decisions (D-LIC-7, D-LIC-14, D-LIC-17) are recorded as DECIDED but the gate does not verify non-blocking entries; the orchestrator's check of the approval does verify them. | gate behaviour; independent review |

## 8. Legal acknowledgments and unresolved counsel questions

By signing, the owner acknowledges (these are statements about what this document is, not legal conclusions):

* This document, the licence schedules and the inventory are documentation of the owner's intent. They are not legal advice and no counsel has reviewed them.
* The core is described as source-available (family licence: AGPL-3.0-or-later with the Commons Clause), never as open source. The applications are AGPL-3.0-only.
* **Unresolved for counsel:** whether the Commons Clause may stand beside the AGPL (sections 7 and 10) in a combined binary (D-LIC-3, Q4 to Q6); whether AGPL-3.0-only applications may depend on the Commons-Clause core as intended (N1); the scope of "Software" in the Commons Clause (N2); contributor terms (N3, outside contributions are not merged); and whether the copyright statement in `LICENSE.md` is adequate given AI-agent-authored commits and three author identities (N4, flags F1 and F2).
* A documentation change does not resolve any of these, and none of them is resolved by this signature.

## 9. Blockers that remain after this approval

| Id | Blocker |
|---|---|
| T1 | D-LIC-2 option (a): licence files exist in the new tags redevops-connectors v0.1.1 (v0.1.0 plus the licence commit, tag object 04d39844) and knowledge-frontier v0.2.1 (v0.2.0 plus licence files, tag object d2b52a08), created in local clones and pushed ONLY to the workspace mirrors; pins, locks, inventory and docs name them. Open: both tags must be pushed to the public GitHub repositories before a binary is distributed (pins-04), and the sidecar/wheelhouse/SBOM must be rebuilt from these pins. Note: the earlier pyprojects declared plain AGPL-3.0-or-later; the new tags apply the family licence the owner stated for D-LIC-2. |
| T2 | No legal-decision key exists in packaging/legal-allowed-signers (it ships empty). The owner creates a key separate from the release key (the gate refuses the release key as a legal signer) and confirms its fingerprint through an independent channel. |
| T3 | The About page has five legal slots (copyright line, licence statement, source offer, no-warranty, section 13 notice). Each needs an approved text file with its own detached signature, and each slot lists decisions that include counsel decisions. A distribution build is refused until they are filled. |
| T4 | pins-04 needs the release commit and tag to be pushed to the public repository before a binary is distributed; no push has been made. |
| T5 | The Windows and Linux native build hosts are driven by drivers in scripts/release/native/ (an Azure VM and a Lima VM, each run twice in separate directories, twice in one directory, and twice with SOURCE_DATE_EPOCH). The pack output is arranged into the layout S4 and S5 read by scripts/release/arrange_native.py, which refuses a tampered, unlisted, symlinked or traversing pack and records whether the builds are byte-identical. Both drivers were run to completion for commit 7ecaa12c (later commits changed only the native drivers, the analyzer, their tests and one generated owner form, none of which is built into the product) and the results are in RELEASE_NATIVE_QUALIFICATION_2026-11-11 in the records folder: on both platforms the sidecar, launcher, About record and SBOM are byte-identical in every pair; the installers are NOT byte-identical (Linux .deb: tar member mtimes; Windows NSIS and MSI: member mtimes, a fresh MSI revision UUID and regenerated identifiers; the files inside are identical; SOURCE_DATE_EPOCH does not reach these tools). Reproducibility of the installers is therefore not claimed. Open: the installers would need pinned mtimes and MSI identifiers in the packaging configuration, which is not part of this preparation; the builds were not re-run on the final release commit, only on 7ecaa12c; Windows inner executables for the signing stage come from the Windows host helper, not from this adapter. |
| T6 | macOS signing package for a distribution run: the c317 package (revision 4.1) cannot be reused (its pre-run check asserts that the gate REFUSES, it pins c317 constants, writes distribution=no, and its post-run check 4 is defective, X-2). Replaced by scripts/release/signing_package.py (derives every value from the release commit, distribution=yes, gate must ALLOW, no constants) and scripts/release/verify_signed.py, with tests. Status: two independent AI reviews are recorded in RELEASE_SIGNING_PACKAGES_2026-11-11 in the records folder: the first REJECTED the first version (six required findings, all fixed with tests), the second approved the fixes with advisories (one new required item, fixed afterwards and not re-reviewed). The scope files were checked against the keys and comparisons of the real signing scripts; the macOS verifier was run on the real c317 signed pair (X-2); the runbooks and the Windows and SSHSIG paths have run only on synthetic fixtures; no signing script has run --real for this lineage except the c317 private macOS run. |
| T7 | The release tooling of this branch (gate bundle record, approval renderer, authorization command, orchestrator) had two independent adversarial reviews; their findings, including two defects of the first fixes, are closed and each reproduction is a regression test. The last fix commit was re-verified by those tests only, not by a third review. |
| T8 | Orchestrator stages S5 (signing packages), S6 (owner signing), S7 (verification) and S9 (result) are implemented and tested on synthetic fixtures. S6 never signs: it reports OWNER until the owner's outputs are in place and then PASS; S7 verifies them (for Windows only with the pinned Authenticode verifier and the signed package, otherwise it FAILS). They have not run on a real release, because S1 stops first while counsel decisions are open. S8 (push and tag) still refuses unless S0 to S7 PASSED in the same run. Limit: the Windows container stage signs the rebuilt installers as given; the signing script does not inspect their contents, so the tie between the signed inner files and the installers is the owner's rebuild step and the container listing, not a check of this toolchain. Limit: the About record is checked for the release commit, distribution mode and approved legal slots, but is not bound to the individual deliverables (for Linux nothing else ties it to the packages). |
| T9 | The legal-decision signer's fingerprint must be recorded by the owner from an independent channel and passed to the orchestrator; the repository cannot establish it. |
| T10 | Artwork: D-LIC-8 is prepared as option c (hero image removed from the product) and D-LIC-11 as option b (generated neutral launcher icons, provenance and hashes in docs/legal/ARTWORK_PROVENANCE.md, no authorship or rights asserted). They become decided only if the owner signs the approval. Not covered by any record: apps/social-autopilot/reel/assets/hero.jpg and the 18 images in deploy/assets/repos/ (listed in the provenance record, not part of the desktop product's shipped set). Rights in script output written by an AI coding agent remain a counsel question (Q29). The built UI keeps one sample item whose picture path is /hero.jpg; that picture cannot load. |
| L1 | The decisions of section 5 are OPEN: D-LIC-1, D-LIC-3, D-LIC-4, D-LIC-5, D-LIC-6, D-LIC-9, D-LIC-10, D-LIC-12, D-LIC-16, D-LIC-18, pins-03, pins-08. (D-LIC-16 applies to the QA bundle only, not to the three desktop platforms; D-LIC-18 to macOS only; D-LIC-9 and pins-08 to Windows only.) |

## 10. How this approval becomes effective

1. The owner reads this document and its sha256 (printed by `scripts/release/authorize_release.sh`, shown separately from the file).
2. The owner says explicitly that they approve this exact document.
3. The owner runs `scripts/release/authorize_release.sh --doc <file> --pubkey <legal-decision public key>` at their own terminal. It copies the document to a private directory and works only on that copy, re-checks it against the commit, shows the digest and the key fingerprint, asks the owner to type both and to type the signing name (which must equal the name in section 11), signs the copy once with `ssh-keygen -Y sign` (namespace `redevops-legal-decision@v1`, agent-held key), then writes the document, its signature, the signer list and the gate entries of the bundled decisions, asks the release gate itself to verify them, and commits exactly those four paths (rolling everything back if the gate refuses). It signs nothing else and pushes nothing.
4. `packaging/check-distribution-gate.py` verifies the signature and maps each decision of the signed header to its gate entry; an entry that is not listed, that names counsel (a set frozen inside the gate), or whose option differs is refused.
5. The owner records the fingerprint of the legal-decision key from an independent channel and passes it to the orchestrator (`--expect-signer-fingerprint`). Without that value the orchestrator cannot tell the owner's signature from a signature by whatever key the release commit lists. To make a process that holds your agent socket unable to sign silently, load the key with `ssh-add -c` (confirmation on every use).

## 11. Signature block (prefilled DRAFT)

The values below are prefilled from the owner's earlier signed form so that the format is the established one. **They are not an act of the owner and not a signature.** The date is never prefilled; `authorize_release.sh` records the date when the owner runs it.

* Owner name as signing (from the 2026-11-10 form): `____Alex Rybachek____`  *(prefilled, DRAFT; `authorize_release.sh` makes the owner type exactly this name, so it is part of the signed bytes)*
* Initials: `_AR_`  *(prefilled, DRAFT)*
* Date: `____________`  *(left blank; set at the signing run)*
* Status: **DRAFT - NOT AUTHORIZED**

