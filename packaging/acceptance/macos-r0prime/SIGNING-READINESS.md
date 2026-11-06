# macOS signing and notarization readiness (R0-prime candidate): checklist, NOT executed

Status: a plan. Nothing here has been run for R0-prime. **Signing and notarization need the owner's separate, explicit authorization**
(the identity, the Apple notarization account and the notarytool keychain profile belong to the owner; this document names none of them
and no agent may use them without that authorization). This file contains no credentials and no keychain item names: placeholders in
angle brackets are filled in by the authorized person at run time and are not stored in the repository.

Reference procedure: the frozen script `sign_and_notarize_macos_v2.sh` (script version 2, written for the v0.3.2 candidate and
independently reviewed, verdict APPROVE, in `review-macos-agentic-apps/REVIEW.md`). The frozen script and the v0.3.2 evidence are
read-only and must stay unchanged; any change for R0-prime goes into a **new, separately named script** that gets its own review.

## 1. What has to be signed, and in which order

Inside-out, never `--deep` for signing (it is used only for verification):

| # | Artifact | How (as in v2) | Entitlements | Why this order |
|---|---|---|---|---|
| 1 | `Contents/MacOS/redevops-sidecar` (PyInstaller onefile Mach-O, arm64) | `codesign -s <Developer ID Application identity> --force --timestamp --options runtime --identifier io.redevops.agentic-apps.r0prime.sidecar --entitlements <plist>` | **exactly one key**: `com.apple.security.cs.disable-library-validation = true` | nested code is signed before the code that contains it |
| 2 | `Contents/MacOS/agentic-r0prime` (the Tauri launcher Mach-O, arm64; the frozen v0.3.2 launcher is `agentic-apps`) | `codesign -s <identity> --force --timestamp --options runtime` | none | |
| 3 | `Agentic Apps R0-prime.app` (the bundle, bundle id `io.redevops.agentic-apps.r0prime`) | same flags, on the bundle path, after 1 and 2 | none (the bundle signature carries the launcher's) | seals `Info.plist` and **every file under `Contents/Resources/`**, including the new `Licenses/` folder; re-signs the main executable with the bundle identifier |
| 4 | the `.dmg` | `codesign -s <identity> --force --timestamp <dmg>` (no `--options runtime` for a disk image) | n/a | a disk image is signed last; notarization is submitted for the dmg |
| 5 | notarize the dmg, then **staple the dmg** | `xcrun notarytool submit <dmg> --keychain-profile <profile name> --wait`, `notarytool log`, `xcrun stapler staple <dmg>`, `stapler validate` | n/a | only the dmg is stapled (see 3.3) |

There are no other Mach-O files in the current layout (`verify_dmg.sh` lists them: two). The 61 libraries that the sidecar extracts to `$TMPDIR/_MEI*` at every start are
**not** part of the bundle and are ad-hoc signed; they are not covered by the notarization ticket. This is why the sidecar needs `disable-library-validation`.
A signed candidate must therefore not be described as "hardened" without that caveat; the structural fix (a one-directory sidecar with every library signed inside the bundle, no entitlement) is an R1 item.

## 2. Exact differences from the v0.3.2 procedure

1. **Layout.** The bundle now contains `Contents/Resources/Licenses/` (licence texts, `THIRD_PARTY_NOTICES.txt`, `NOTICES_MISSING.*`, `LICENSES-MANIFEST.json`). They are
   data, so no signing step is added, but the bundle signature must **seal** them: `verify_dmg.sh --expect signed` row `S-11` requires the sealed-file count to be at least the number of
   files in `Licenses/` (the v0.3.2 bundle sealed 2 files). The v2 script's "unexpected executable or library" guard (`find ... -perm -u+x`) must still pass: the licence files must not be executable.
2. **Re-review of the script for the new layout.** v2 hard-codes the FROZEN identity: `APP_NAME="Agentic Apps.app"`, `MAIN_EXE=agentic-apps`, `SIDECAR_EXE=redevops-sidecar`, `SIDECAR_ID=io.redevops.agentic-apps.sidecar`; for R0-prime they must become
   `APP_NAME="Agentic Apps R0-prime.app"`, `MAIN_EXE=agentic-r0prime`, `SIDECAR_EXE=redevops-sidecar` (unchanged), `SIDECAR_ID=io.redevops.agentic-apps.r0prime.sidecar` (see `docs/R0_PRIME_IDENTITY.md`; `python3 -I packaging/tools/identity.py all` prints them),
   the expected entitlement JSON, the header text "Agentic Apps 0.1.0 ... release v0.3.2 ... run 35353188692", the dmg staging set (app, `.VolumeIcon.icns`, `Applications` symlink) and the volume name. Check each against the
   R0-prime candidate, and re-read the whole script once more: any new binary (a second helper, an updater, a CLI) makes the guard abort until it is added to the signing plan.
   If anything other than those three items must sit at the dmg root (for example a `Licenses/` folder next to the app), the staging step has to add it (the v2 dmg is rebuilt from a staged copy, so extra root files are dropped).
3. **Stapling is unchanged and must stay accurately described.** Only the dmg is stapled; the app inside has no ticket and relies on Gatekeeper's online lookup. Offline first launch of an app copied out of the dmg is UNVERIFIED (`MANUAL-CHECKLIST.md`, part H).
   The optional improvement (notarize and staple the app before building the dmg) is not part of this plan unless the owner asks.
4. **Entitlements need re-validation, not copying.** The sidecar bytes changed (capability handshake, lifecycle tether, UTF-8 stdout). Re-run the empirical check that the single key is still necessary and sufficient
   (the v0.3.2 matrix is in `macos-agentic-apps-v0.3.2/ent/`), and that `allow-unsigned-executable-memory` / `allow-jit` are still not needed. The launcher now also spawns `/usr/bin/open` and the sidecar tree
   and holds pipes; no extra entitlement is expected (INFERRED). **Only a signed build can show that "Start & open" still works under the hardened runtime**: that is a check for `MANUAL-CHECKLIST.md` parts E to G on the signed artifact (UNVERIFIED).
5. **Build-side items to settle BEFORE signing (they change the signed bytes):** `Info.plist` `LSMinimumSystemVersion` is 10.13 while the binaries have `minos` 11.0 (review finding 2; Tauri's `bundle.macOS.minimumSystemVersion`, default 10.13, would
   set it); RESOLVED by decision 8: the R0-prime bundle is version `0.1.1`, product `Agentic Apps R0-prime`, bundle id `io.redevops.agentic-apps.r0prime`, so its dmg is `Agentic Apps R0-prime_0.1.1_aarch64.dmg` and cannot collide with the frozen `Agentic Apps_0.1.0_aarch64.dmg`
   (`verify_dmg.sh` rows P-01, P-06 to P-09 and `packaging/tools/check_bundle_identity.py` check it). The v2 script must also be given the new dmg name and volume name (`Agentic Apps R0-prime`); the header text "Agentic Apps 0.1.0 ... v0.3.2" must be rewritten.
6. **Findings of the v0.3.2 review that v2 already absorbed** (keep them): fail-closed entitlement reading (`--entitlements - --xml`), a private copy of the input dmg, tidy out-dir, exact-path containment, wording "only the dmg is stapled".
   **Findings the new build changes:** the orphaned sidecar (finding 4) and `/openapi.json` 500 (finding 5) are fixed in the sources (reports/defects.md); finding 6 (provenance: tag commit versus CI head SHA) needs a fresh statement for R0-prime.
7. **Hash of the script used.** Record the sha256 of the exact script file (as v2 does in EVIDENCE.txt) and of the entitlements plist. The v2 header notes that the first script version is kept unchanged as the record of what ran; do the same.
8. **Input.** The input of the signing step is the **unsigned** dmg produced by the `build` agent/CI for R0-prime, with an expected sha256 computed on the build machine. Run `verify_dmg.sh --expect unsigned` on that input first (Licenses/ present, arm64 only, `minos`, no unexpected Mach-O).
   A local development build (toolchain, versions and pins not identical to CI) is not a candidate artifact.

## 3. Evidence to produce (and keep, in this order)

1. `verify_dmg.sh --expect unsigned` transcript of the input dmg, with the expected-hash file.
2. The signing script's `EVIDENCE.txt`, `codesign/*.txt` (`-dvvv` of sidecar, launcher, bundle, dmg; the entitlement plists as read), `notary/submit.json`, `notary/log.json` (and its sha256), `staple.txt`, `staple-validate.txt`, `verify/*.txt`, and `FAILED.txt` if a run failed (a failed run is never reused).
3. `verify_dmg.sh --expect signed --team-id <Team ID> ...` transcript of the **output** dmg, run from a different account than the signing account where possible (hash, authority chain, hardened-runtime flags, timestamps, one Team ID,
   exactly the one sidecar entitlement, sealed resources covering `Licenses/`, `spctl` = Notarized Developer ID, `stapler validate` on the dmg, no `get-task-allow`).
4. Content equality of input and output (the review's method): signature-stripped copies of both executables equal; `Info.plist`, icon and every `Licenses/` file byte-identical; cdhashes of dmg, launcher/bundle and sidecar present in the notary log's `ticketContents`.
5. The sha256 of every artifact in one table (input dmg, output dmg, signed and stripped binaries, script, plist, notary log, EVIDENCE) and the `collect_evidence.sh` bundle hash.
6. Statements that must be present and accurate: what is stapled (the dmg only), what Gatekeeper accepted online, and what is **UNVERIFIED** (offline first launch, GUI behaviour of the signed build, Intel, older macOS).
7. An independent review of the output by someone who did not run the signing, as for v0.3.2.

## 4. Preconditions and authorizations (owner decisions)

* Authorization to sign and to notarize, named separately. Notarization **uploads the dmg to Apple's notary service**: confirm that the private candidate may leave the machine.
* Which identity and which notarization profile (supplied at run time by the owner; not recorded here).
* Whether the signing account is the owner's everyday account. The v0.3.2 evidence shows the keychain profile approach; any other approach (a dedicated signing account, a CI secret) is the owner's call.
* The signing run itself must not be started by an agent that has not been given this authorization; it must not print secrets and must keep to the script's `--out-dir` containment.
* Timing: a new certificate may be minutes old; the secure timestamp keeps signatures valid after the certificate expires, but a re-sign is needed for every release.

## 5. Not part of this document

No signing, notarization, stapling, keychain access or network call to Apple has been made for R0-prime.
