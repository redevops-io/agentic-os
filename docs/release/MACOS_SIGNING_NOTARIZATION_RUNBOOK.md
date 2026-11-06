# macOS signing and notarization runbook (Agentic Apps R0-prime, arm64) - PREPARED, NOT EXECUTED

Purpose: how the private unsigned macOS arm64 package becomes a Developer ID signed, notarized and stapled dmg, and how a person runs the unsigned build locally in the meantime.
Implementation: the commands below are what `packaging/signing/sign_and_notarize_r0prime_macos_v3.sh` runs; the script and its authorization flow are the reviewed procedure, this page does not replace them.

**Status.** Nothing in this runbook has been run with a real identity or against Apple. It authorizes nothing. A real run requires the separate, reviewed authorization flow of
`packaging/signing/SIGNING-PROCEDURE-R0PRIME.md` (scope format v3, gates G1 to G10, a scope file per approval, the owner's own terminal, one ledger). This page contains no secret and none must be
added to it: the Developer ID identity string, the notarytool profile name, Apple credentials, keys and tokens are typed by the owner at their own terminal and never written to a file, a chat or a log.

## 1. The input artifact

| | |
|---|---|
| Package | `Agentic Apps R0-prime_0.1.1_aarch64.dmg` (arm64 only; product `Agentic Apps R0-prime`, bundle id `io.redevops.agentic-apps.r0prime`, version 0.1.1; `packaging/identity.json`) |
| How it is built | `packaging/candidate/build-local-macos-candidate.sh OUT_DIR` (wheelhouse, PyInstaller sidecar twice, notices, stage, `tauri build --bundles app,dmg`); see `packaging/BUILD-hosts.md` |
| State of the input | UNSIGNED for release. The build seals the `.app` with the AD-HOC identity only (`LAUNCHER_ADHOC_SIGN=1`, `packaging/candidate/check_adhoc_seal.py`): no Developer ID, no hardened runtime, no entitlement, no timestamp. Signing below replaces every signature (`--force`). |
| Binding facts | the dmg sha256, the build commit (the `source.commit` in `Contents/Resources/Licenses/about.json`), `packaging/identity.json`, the entitlement plist and the script hashes go into the scope file; the exact values of a given build are in its build report and `SHA256SUMS`, not here |

Gate G6 (`packaging/acceptance/macos-r0prime/verify_dmg.sh --expect unsigned` passes) accepts the ad-hoc sealed bundle: unsigned mode asserts only that no Developer ID signature is present.
The signing script's layout check accepts the build's ad-hoc seal (`Contents/_CodeSignature` holding exactly one `CodeResources`, bundle `Signature=adhoc` without a certificate authority), removes it from its STAGED copy and replaces it with the real signing; any other content of `_CodeSignature`, or a certificate-backed existing signature, is still refused. This edit to a reviewed signing file belongs in the new independent review round that the RC3 signing files already need before any signing.

## 2. What is signed, in which order, with which flags

All with the owner's Developer ID Application identity from the owner's login keychain (`$ID` below is the identity string read from the environment by the script; never typed into a file).

| # | Item | Command (flags as in the script) |
|---|---|---|
| 1 | `Contents/MacOS/redevops-sidecar` | `codesign --force --timestamp --options runtime --identifier io.redevops.agentic-apps.r0prime.sidecar --entitlements packaging/macos/sidecar.entitlements.plist --sign "$ID" <app>/Contents/MacOS/redevops-sidecar` |
| 2 | `Contents/MacOS/agentic-r0prime` | `codesign --force --timestamp --options runtime --sign "$ID" <app>/Contents/MacOS/agentic-r0prime` (no entitlements) |
| 3 | the bundle | `codesign --force --timestamp --options runtime --sign "$ID" <app>` (never `--deep`; seals `Info.plist`, `icon.icns` and every file of `Resources/Licenses/`) |
| 4 | the dmg (rebuilt from the signed app: `hdiutil` UDRW then UDZO, `Applications` link, volume icon) | `codesign --force --timestamp --sign "$ID" <dmg>` (no runtime flag for a disk image) |
| 5 | notarization | `xcrun notarytool submit <signed dmg> --keychain-profile "$PROFILE" --wait --timeout 30m --output-format json`, then `xcrun notarytool log <id> --keychain-profile "$PROFILE"` |
| 6 | staple (only if the status is `Accepted`) | `xcrun stapler staple <dmg>` then `xcrun stapler validate <dmg>` |

* **Hardened runtime** (`--options runtime`) on the sidecar, the launcher and the bundle; not on the dmg.
* **The one entitlement**: the sidecar carries exactly `com.apple.security.cs.disable-library-validation` (`packaging/macos/sidecar.entitlements.plist`; reason and residual risk R1 in `docs/MACOS_ENTITLEMENTS.md`: the onefile sidecar extracts six ad-hoc-signed libraries to `$TMPDIR/_MEI*` at every start and loads them). The launcher and the bundle carry none; nobody carries `get-task-allow`. The script reads the entitlements back fail-closed.
* Step 5 UPLOADS the signed dmg (about 40 MB) to the Apple notary service; it needs network access and the notarytool keychain profile created beforehand by the owner (`xcrun notarytool store-credentials`, done by the owner; only its NAME is used).
* Only the dmg is stapled; the app inside has no ticket and relies on Gatekeeper's online lookup (owner decision O-04 in the procedure).

## 3. How to run it (the reviewed script, at the owner's terminal)

```
# once per run: a fresh signing directory that is NOT a candidate/evidence directory
mkdir -p "$SIGN/in" && cp -p "<candidate dir>/Agentic Apps R0-prime_0.1.1_aarch64.dmg" "$SIGN/in/" && shasum -a 256 "$SIGN/in/"*.dmg

# rehearsal, no secret (reviewers can run these): plan only, then the whole sequence with the ad-hoc identity on a copy
./packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --input-dmg "$SIGN/in/<dmg>" --expected-sha256 <hex> --out-dir "$SIGN/out-plan"
./packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --adhoc-test --input-dmg "$SIGN/in/<dmg>" --expected-sha256 <hex> --out-dir "$SIGN/out-adhoc"

# the real run, only after the authorization flow (scope file, gates, review): identity and profile typed by the owner, never stored
read -r R0P_CODESIGN_IDENTITY; read -r R0P_NOTARY_PROFILE; export R0P_CODESIGN_IDENTITY R0P_NOTARY_PROFILE
./packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --real --notarize \
    --i-have-owner-authorization "$(shasum -a 256 scope-macos.txt | awk '{print $1}')" --scope-file scope-macos.txt \
    --ledger "$HOME/r0prime-signing-ledger" --input-dmg "$SIGN/in/<dmg>" --expected-sha256 <hex> --out-dir "$SIGN/out-real"
```

Start the script as `./script` (never `bash script`); one ledger; scope files per approval; `AUTHORIZATION-TEMPLATE.md` lists every key. A distribution build (`--distribution`) additionally needs every
distribution-blocking decision in `packaging/distribution-gate.json` DECIDED; a private candidate signing prints them and says that signing does not imply permission to distribute.

## 4. Verification after signing (the evidence the run must produce)

```
codesign --verify --strict --verbose=2 <app>/Contents/MacOS/redevops-sidecar        # and the launcher, and the bundle: valid, hardened runtime flag, Team ID, secure timestamp
codesign -dvvv <app>; codesign -d --entitlements - --xml <app>/Contents/MacOS/redevops-sidecar   # exactly the one key; launcher and bundle: none
codesign --verify --strict --verbose=2 <dmg>
xcrun stapler validate <dmg>                                                         # ticket stapled to the dmg
spctl --assess --type open --context context:primary-signature -vv <dmg>            # accepted, source=Notarized Developer ID
spctl --assess --type execute -vv <app copy>                                         # accepted (online lookup for the unstapled app)
packaging/acceptance/macos-r0prime/verify_dmg.sh --expect signed --team-id <TEAM> <dmg> <sha file>   # from a different account where possible
```
Then the offline-copy test and the content-equality review of section 5.4 of the procedure, and a NEW evidence set for the signed artifacts (the unsigned set is not edited). Failure modes to expect and record:
notarization `Invalid` (read `notarytool log`: usually a nested binary without hardened runtime or timestamp, or an unsigned library; the extracted `_MEI*` libraries are not part of the bundle), `spctl` rejection of the app
copy when offline (not stapled), and a sidecar that does not start under the hardened runtime without the entitlement ("different Team IDs").

## 5. Running the unsigned private build locally (for the owner and testers, before any signing)

The package is not notarized, so Gatekeeper REFUSES it on a Mac where it carries the quarantine attribute (anything downloaded or copied out of a quarantined dmg). With the current build (ad-hoc sealed; `codesign --verify --deep --strict` passes)
the refusal is the normal "Apple could not verify ... is free of malware" / "unidentified developer" path. Builds made before the ad-hoc seal was added (the RC2 candidate, the first rel/macos build) fail verification ("code has no resources but signature indicates they
must be present") and Gatekeeper reports them as **"is damaged and can't be opened"**; use the first route below for those.

1. **Remove the quarantine from your own copy (the route that was verified here).** Copy the `.app` out of the mounted dmg to a folder you own, then
   `xattr -dr com.apple.quarantine "/path/Agentic Apps R0-prime.app"` and open it normally. This clears the flag on every file, including the sidecar. Only do this for a copy whose sha256 you compared with the build report.
2. **System Settings > Privacy & Security > Open Anyway.** Open the app once (it is refused), then System Settings > Privacy & Security, scroll to the message about "Agentic Apps R0-prime" and choose "Open Anyway"; authenticate. On current macOS a right-click > Open
   no longer overrides the refusal for such an app. NOT tested here (it needs a person at the console); the risk is that only the launcher is approved while the quarantined sidecar it starts is assessed separately: if Start & open times out after 30 s, use route 1.
3. Terminal only, without LaunchServices: running `Contents/MacOS/agentic-r0prime` directly from a shell skips the Finder/Gatekeeper path (the way the native validation harness runs it).

Do not run the unsigned app from the mounted dmg; it keeps the quarantine of the dmg.

**Where state lives, and how to remove everything.** Nothing is installed outside the folder you chose. The launcher writes only `~/Library/Caches/io.redevops.agentic-apps.r0prime/launch` (directory 0700, one-time redirect pages 0600, deleted on a normal exit and by a later start),
its web view's own data under `~/Library/WebKit/io.redevops.agentic-apps.r0prime` and `~/Library/Caches/io.redevops.agentic-apps.r0prime/WebKit`. The sidecar writes no data of its own in the desktop path (the Projects UI serves in-memory sample data unless `OUTCOME_STORE_PATH` is set, which the launcher does not do; its
`_MEI*` extraction folder in `$TMPDIR` is removed on a normal stop). There is no launch agent, login item, preference file or helper. To uninstall: quit the app, delete the `.app`, and delete those two folders. A `kill -9` of the sidecar leaves one `_MEI*` folder (about 80 MB) in `$TMPDIR`, safe to delete when no `redevops-sidecar` runs.
