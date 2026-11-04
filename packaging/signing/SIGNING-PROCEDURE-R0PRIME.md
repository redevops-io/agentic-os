# Signing procedure for the Agentic Apps R0-prime candidate (macOS arm64 now, Windows x64 prepared)

Status: a PROCEDURE and its tools, prepared for the independent review required by owner decision 10 (2026-11-04). **Nothing has been signed,
notarized, stapled or uploaded.** No identity, key, Keychain item, Apple credential, Azure account or ssh-agent was used to prepare or test any of it.
Signing needs the owner's separate, explicit authorization (`AUTHORIZATION-TEMPLATE.md`); this document authorizes nothing.

Evidence levels used below: OBSERVED (seen in a run on this Mac), STATIC (read from source or documents), INFERRED, UNVERIFIED.

Revision: round 5 fixes applied (R5-01 to R5-06 and the INFO items; table in `reports/signproc.md`): the scripts start with an unconditional `bash -p` / `env -i` re-exec before any function or PATH-resolved command runs, the unit-test switch is refused in `--real`, the ledger is a hash-chained directory with an atomic claim, the literals have a source-verifying gate, Windows python packages are pinned by name and version.
Revision: round 4 independent review fixes applied (findings R4-01 to R4-11; table in `reports/signproc.md`). Scope files are now **version 2**
(`AUTHORIZATION-TEMPLATE.md`); the new shared library `signing-common.sh` is bound by hash like the scripts.

Files (all in `packaging/signing/`): `signing-common.sh`, `sign_and_notarize_r0prime_macos_v3.sh`, `sign_windows_r0prime_v3.sh`, `r0prime_win_signcommand.sh`,
`sign_manifest_sshsig.sh`, `verify_signature_v2.py` (the frozen verifier, byte-identical), `tools/make_adhoc_fixture.sh`, `DIFF-vs-v2.md`,
`SSHSIG-manifest-signing.md`, `AUTHORIZATION-TEMPLATE.md`, `README.md`; tests in `tests/test_signing_procedure_r0prime.py`.
Frozen references (read-only, unchanged): `sign_and_notarize_macos_v2.sh`, `sign_windows_artifact_signing_v2.sh`, their ERRATA and the two independent
reviews of the v0.3.2 runs (both APPROVE). The names used here come from `packaging/identity.json` (R0-prime line); no frozen name is reused.

---------------------------------------------------------------------------------------------------------------------------------------

## 1. What is signed, in which order, and what is NOT

| # | Platform | Object | How | Why this order |
|---|---|---|---|---|
| 1 | macOS | `Contents/MacOS/redevops-sidecar` (PyInstaller onefile Mach-O arm64) | `codesign --force --timestamp --options runtime --identifier io.redevops.agentic-apps.r0prime.sidecar --entitlements packaging/macos/sidecar.entitlements.plist` | nested code first |
| 2 | macOS | `Contents/MacOS/agentic-r0prime` (Tauri launcher Mach-O arm64) | `codesign --force --timestamp --options runtime`, no entitlements | |
| 3 | macOS | `Agentic Apps R0-prime.app` (bundle) | same flags, never `--deep`; seals `Info.plist`, `icon.icns` and every file of `Resources/Licenses/` | the bundle seal covers the two signed executables |
| 4 | macOS | the dmg (rebuilt by the script from the signed app) | `codesign --force --timestamp` (no runtime flag for a disk image) | a disk image is signed last |
| 5 | macOS | notarization of the signed dmg, then staple the dmg | `notarytool submit --wait --timeout`, `notarytool log`, `stapler staple`, `stapler validate` | only the dmg is stapled |
| 6 | Windows | installed `agentic-r0prime.exe`, `redevops-sidecar.exe` (stage `inner`) | Jsign 7.6 + Azure Artifact Signing, RFC 3161 timestamp | inner files BEFORE the installers are packaged |
| 7 | Windows | NSIS `uninstall.exe` (stage `inner`) | same | generated while the NSIS installer is built |
| 8 | Windows | MSI and NSIS setup exe (stage `container`) | same | containers last, after they were rebuilt from the signed inner files |
| 9 | evidence | top-level `release-manifest.json` of the matching evidence set | SSHSIG, namespace `redevops-qa-release@v1`, existing release key via the owner's ssh-agent | separate approval; see `SSHSIG-manifest-signing.md` |

Not signed, by design: the six Mach-O libraries the sidecar extracts to `$TMPDIR/_MEI*` at every start (ad-hoc signed, not part of the bundle, not covered by
the notarization ticket: see Risk R1); third-party DLLs of the Windows tooling (NSIS plugins, WiX extensions: never signed with the release identity); the
libraries inside the Windows PyInstaller onefile (unsigned content of the sidecar).

Signing is not publication, a signature is not a licence, and a notarization ticket is not permission to distribute (section 3).

---------------------------------------------------------------------------------------------------------------------------------------

## 2. Who does what

* An agent may prepare, review and test with **stubs and ad-hoc signatures of copies** only. An agent never holds, reads or is given an identity, a notary
  profile, a token, a key or an agent socket, and never runs a `--real` mode.
* The owner (or a person the owner supervises at the owner's own terminal) runs every real command. Identity, notary profile name and Azure coordinates are
  typed or exported by the owner in that shell at run time and are never put in a file of the repository, a CI variable, a chat message or a transcript.
* A person who did not run the signing reviews the output (section 8).

---------------------------------------------------------------------------------------------------------------------------------------

## 3. Gates before ANY real signing

### 3.1 Always (private candidate or not)

| Gate | How it is checked | Owner of the check |
|---|---|---|
| G1 Authorization | the filled-in `AUTHORIZATION-TEMPLATE.md` exists, one approval per item (macOS sign, macOS notarize/upload, Windows inner, Windows container, manifest SSHSIG); its scope file hashes are what the scripts check | owner |
| G2 Independent review done | the final independent review (owner decision 10) of the exact evidence set AND of this procedure and its scripts has a verdict that names the sha256 of every script, the plist, `identity.json` and the evidence manifest | reviewer |
| G3 Exact bytes, built from a clean checkout | the input was built from a clean checkout of the exact commit (`git status` clean, `git rev-parse HEAD` and the tree hash recorded in the evidence); a local development build is not a candidate | build agent / owner |
| G4 Hashes recomputed from the exact evidence set | `tools/verify_evidence.sh --candidate-dir <candidate> --expect-manifest-sha256 <digest>` of that set passes strictly; the dmg's sha256 is recomputed on the machine that signs and compared with the evidence (the scripts also compare it with `--expected-sha256`) | signer |
| G5 Manifest digest obtained out of band | the digest to be signed comes from the evidence INDEX/review and the delivery message, not from the machine being signed on; never sign a superseded manifest | owner |
| G6 Input is verified unsigned | `packaging/acceptance/macos-r0prime/verify_dmg.sh --expect unsigned` passes on the input dmg (Licenses present, arm64 only, no unexpected Mach-O, `minos`) | signer |
| G7 Scripts are the reviewed ones | `shasum -a 256` of the signing script, plist, verifier and `identity.json` equal the values in G2 (the scope file binds the script and plist hashes) | signer |
| G8 Names are R0-prime | the dry run prints `Agentic Apps R0-prime.app`, `agentic-r0prime`, `io.redevops.agentic-apps.r0prime`, `io.redevops.agentic-apps.r0prime.sidecar` and the dmg name `Agentic Apps R0-prime_0.1.1_aarch64.dmg`; a frozen name aborts the run | signer |
| G9 Sidecar entitlement decision | `docs/MACOS_ENTITLEMENTS.md`: the provisional decision 4 (keep `disable-library-validation` on the sidecar only) is the stated basis; the owner accepts the residual risk R1 for this named build | owner |
| G11 Ledger | a real run needs `--ledger DIR` (absolute, a directory outside the repository, created 0700): the scope is CLAIMED atomically (`mkdir` of `claims/<scope sha>`) right before the first use of the owner's session, so of several parallel runs of one scope exactly one proceeds; every event is a separate file created with O_EXCL (a planted symlink is never followed) carrying the sha256 of the previous one, and `LENGTH` records the count and last hash: a broken chain, a gap or a truncated tail is refused. The same scope is refused again unless `--allow-reuse` is passed (recorded). A failed run burns its scope on purpose. Limit: the owner's own account can still delete the whole directory | signer |
| G12 Literals verified against their sources | `tools/check-literals-against-sources.sh <sources root>` exits 0 (every digest and fingerprint typed into `packaging/signing/**` re-verified against the frozen scripts, the evidence set and the committed release PUBLIC key; a missing source is a FAILURE). The unit test without a source root is a self-consistency check only and says so. **A signer MUST run this gate with the real sources root before the first real run** and attach its output | signer |
| G10 The evidence set matches the identity | the evidence set that describes the dmg being signed is for the R0-prime identity (see 3.3) | owner |

### 3.2 Private candidate versus DISTRIBUTION candidate

* **Signing a private candidate** (the signed dmg/installers stay on the owner's machines; nothing is published, uploaded to a release, linked, mirrored or sent to a
  third party) needs gates G1 to G10. The licence decisions of `packaging/distribution-gate.json` may be OPEN. The signature states origin of the bytes
  ("built and signed by the company identity"); for that reason `D-LIC-1` (product licence wording and copyright line) and `build-05` (the entitlement) should be decided
  before the owner signs under the company identity (`SIGNING_REQUEST.md` section 6 makes the same proposal; it is a recommendation, not a script check).
* **A signed DISTRIBUTION candidate** (anything that leaves the owner's control: download page, release asset, package index, customer, public repo, store) needs, in addition,
  `python3 -I packaging/check-distribution-gate.py --platform <macos|windows> --distribution` to exit 0, i.e. every distribution-blocking decision DECIDED in writing
  (`docs/legal/OWNER_DECISION_FORM.md`, recorded in `packaging/distribution-gate.json` with hash, decider and date, counsel reference where counsel decides).
  At the date of this document (checked on the integration tip) the gate REFUSES both platforms:
  * macOS: 15 OPEN: D-LIC-1, -2, -3, -4, -5, -6, -8, -10, -11, -12, -13, -15, -18, pins-03, pins-04.
  * Windows: 16 OPEN: D-LIC-1, -2, -3, -4, -5, -6, -8, -9, -10, -11, -12, -13, -15, pins-03, pins-04, pins-08.
  The gate does not judge licences; it checks that written decisions exist. Nothing in this procedure, in a signature, in a notarization ticket or in a passing
  script says that anything is cleared, compliant or permitted.
* **Signing a private candidate does not imply permission to distribute it.** Notarization uploads the signed dmg to Apple (an Apple-held copy); that is not a distribution
  to users but it is a disclosure to a third party, so it needs its own approval (A-05 of `SIGNING_REQUEST.md`).
* The scripts are distribution-neutral: they never publish, tag, release or upload except the one explicit notarization submission.

* **The scripts enforce this** (round 4, R4-09). In `--real`, `r0p_gate` prints the gate's list of open decisions for the platform and then: with `--distribution` it runs the gate with `--distribution` and REFUSES while any blocker is OPEN or the gate file is malformed (before any signing or upload); without it, it prints "PRIVATE-CANDIDATE SIGNING: signing and notarization do NOT imply permission to distribute" and records it in the evidence. The scope file says `distribution=yes|no` and must agree with the flag. The gate file (`packaging/distribution-gate.json`) and the gate tool are bound by sha256 in the scope (`gate_sha256`, `gate_tool_sha256`); the scripts always run the repository copy (no `--gate`). The gate itself now fails closed: any `REDEVOPS_DISTRIBUTION_BUILD` value other than empty or exactly `0` means distribution, and a DECIDED entry must reference a file of the repository whose sha256 matches.

### 3.3 Identity and evidence-set consistency (open point)

The evidence set `evidence-macos-arm64-unsigned-final3` (manifest sha256 `a16c3687...`) describes the candidate `macos-arm64-unsigned-final2`, which carries the FROZEN names
(`Agentic Apps.app`, `io.redevops.agentic-apps`, version 0.1.0): its `SIGNING_REQUEST.md` lists the old identifiers. The R0-prime identity (rc2) changed the names; a candidate rebuilt
with them needs its own evidence set. The scripts here sign only the R0-prime names and **refuse a dmg that holds `Agentic Apps.app`**. Before any signature the owner must decide
which dmg is signed: the rebuilt R0-prime candidate with a matching, reviewed, unsigned evidence set. Never sign a manifest whose set describes another dmg (G10).

---------------------------------------------------------------------------------------------------------------------------------------

### 3.4 What the scope file is, and is not (plainly)

The scope file plus `--i-have-owner-authorization <sha256>` is a **tripwire against mistakes**: the wrong file, the wrong script version, the wrong day, the wrong identity, a repeated run. It is
**not an authentication.** Anyone who can write a text file can produce a scope file that passes (the round 4 reviewer did, with public hashes and a fake identity string). What authenticates a real
run is that it happens in the owner's own terminal, with the owner's keychain identity, notarytool profile, `az` login or ssh-agent, which no script and no agent here holds. The scripts therefore bind
as much as they can into the scope (script, shared library, entitlement plist, `identity.json`, input hashes, identity and profile hashes, Team ID, gate file and tool, evidence-manifest digest, build and
procedure commit ids, tool hashes on Windows, a UTC window of at most 24 hours) and keep a one-time ledger, but none of that replaces the owner's own action. The commit ids and the manifest digest are
recorded in the evidence and are not verified by the scripts (they cannot see the repository history); the reviewer compares them.

## 4. Authorization scope statements the owner must give

`AUTHORIZATION-TEMPLATE.md` has the fill-in form. Every approval states, in words and as a scope file the script checks:

1. WHAT (sign / notarize and upload / staple / Windows inner / Windows container / manifest SSHSIG), each separately.
2. WHICH bytes: the sha256 of every input (dmg; Windows request files; installers; manifest) and of the script and entitlement plist.
3. WHICH identity: macOS Developer ID Application identity (the scope holds `sha256(identity string)` and the Team ID); Windows Artifact Signing endpoint, account, profile and exact certificate CN; SSH release-key fingerprint.
4. WHICH time: a UTC window (`not_before`, `not_after`, at most 24 hours; a longer window is refused). For Windows the 3-day certificates and the timestamp make this a hard requirement (section 6.5).
5. WHERE data goes: macOS notarization uploads the signed dmg to the Apple notary service; Windows signing sends file digests to the Artifact Signing service through Jsign (what exactly Jsign sends to the service, the digest or more, was not observed here: UNVERIFIED); SSHSIG sends nothing anywhere.
6. WHAT is not authorized: publication, tags, releases, CI use of the secrets, re-use of the authorization for another hash or date.

---------------------------------------------------------------------------------------------------------------------------------------

## 5. macOS: step by step

Script: `sign_and_notarize_r0prime_macos_v3.sh` (bash 3.2 compatible, macOS only, never as root). Modes: `--dry-run` (default), `--adhoc-test`, `--real`.

### 5.1 Prepare (owner, once per run)

1. A fresh signing directory that is NOT a candidate/evidence directory. Copy the input dmg into it and check the hash:
   `mkdir -p "$SIGN/in" && cp -p "<candidate>/Agentic Apps R0-prime_0.1.1_aarch64.dmg" "$SIGN/in/" && shasum -a 256 "$SIGN/in/"*.dmg`
   The script refuses an input inside a directory that looks like a candidate or evidence set (names `macos-arm64-unsigned*`, `evidence-macos-arm64*`, `candidate/`, or files such as
   `README-LOCAL-BUILD.md`, `build-manifest.json`, `release-manifest.json`), and any directory named by `--protected-dir`.
2. The Developer ID Application certificate and its key are in the owner's login keychain (the script never reads the keychain; `codesign` does, with the identity named in the environment).
   The `notarytool` keychain profile was created beforehand by the owner (`xcrun notarytool store-credentials ...`); only its NAME is used.
3. Fill the scope file (`AUTHORIZATION-TEMPLATE.md`) and keep its sha256.

### 5.2 Rehearse without any secret (reviewers can run these too)

```
# 1. plan only (default): prints every command, creates nothing, mounts nothing
packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --input-dmg "$SIGN/in/<dmg>" --expected-sha256 <hex> --out-dir "$SIGN/out-plan"
# 2. the whole sequence with the ad-hoc identity '-' on a COPY (no timestamp, no Apple, no keychain); output is ADHOC-TEST-ONLY_<dmg>
packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --adhoc-test --input-dmg "$SIGN/in/<dmg>" --expected-sha256 <hex> --out-dir "$SIGN/out-adhoc"
```
`--adhoc-test` fails if R0P_CODESIGN_IDENTITY or R0P_NOTARY_PROFILE is set. Its spctl results are EXPECTED to be rejections.

### 5.3 The real run (owner, at the owner's terminal)

```
read -r R0P_CODESIGN_IDENTITY         # typed by the owner; exported below; never echoed into a file or a chat
read -r R0P_NOTARY_PROFILE
export R0P_CODESIGN_IDENTITY R0P_NOTARY_PROFILE
packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --real --notarize \
    --i-have-owner-authorization "$(shasum -a 256 scope-macos.txt | awk '{print $1}')" --scope-file scope-macos.txt \
    --ledger "$HOME/r0prime-signing-ledger.txt" \
    --input-dmg "$SIGN/in/<dmg>" --expected-sha256 <hex> --out-dir "$SIGN/out-real"
```
(Without `--notarize` the run signs and verifies only; the scope file must then say `notarize=no`.)

What the script does, in order, and what it refuses:

0. **Starts safely (round 5):** the first executable lines run no function and no PATH-resolved command (reserved words, parameter expansion and `builtin exec` only) and re-execute the script through `/usr/bin/env -i` as `/bin/bash -p` (privileged mode: exported shell functions, `BASH_ENV`/`ENV`, `SHELLOPTS`/`BASHOPTS`/`CDPATH`/`GLOBIGNORE` are not honoured) with fixed PATH, the allowlisted variables and file descriptors 3 to 9 closed; the shebang is `#!/bin/bash -p` as well, so a direct `./script` never reads `BASH_ENV`. Residual: `bash script` (without `-p`) lets a hostile caller environment run a `BASH_ENV` file once in the first shell, and a function named `builtin` would run before the block; both need control of the operator's own shell environment. The re-executed copy then verifies it is that copy (privileged mode, marker, an environment that is exactly the allowlist, no `BASH_FUNC_*`) and closes file descriptors 10 to 63.
1. Requires the environment to BE the allowlist (PATH, HOME, LANG, TERM and three named `R0P_*` variables): if it is not, it re-executes itself once under `env -i`; no marker variable can skip this (round 4: the old `R0P_SCRUBBED` shortcut is gone). Sets PATH to the system directories (`/usr/bin:/bin:/usr/sbin:/sbin`; stubs only with the unit-test switch), unsets the identity and profile variables after reading them so no child inherits them, refuses xtrace and root, and requires system tool locations in `--real`.
2. Reads the names from `packaging/identity.json`; refuses a line that is not distinct from the frozen one.
3. Reads the scope file ONCE into a private copy, hashes the copy against the authorization argument and parses the copy; checks the UTC window (at most 24 h), the script, `signing-common.sh`, plist, `identity.json`, input, identity and profile hashes, the Team ID, that notarize/upload and `distribution` agree with the flags, the ledger (single use) and runs the distribution gate (3.2). The output directory name may contain only letters, digits, `.`, `_`, `-`, `/` and spaces and must not be a symlink.
4. Verifies the input dmg sha256 against `--expected-sha256` (a read), copies it to `out/work/input-copy.dmg` (sha256 re-checked) and attaches THAT copy read-only. The input is never opened for writing.
5. Refuses unexpected content: dmg root items other than the app, `.VolumeIcon.icns` and `Applications`; bundle directories other than `Contents/MacOS` and `Resources`; any file other than the launcher and the sidecar that is executable, a library or has a Mach-O magic; non-arm64 binaries; an Info.plist that differs from identity.json (identifier, executable, versions, `LSMinimumSystemVersion` 11.0, `RedevopsReleaseLine`); a missing or executable file in `Resources/Licenses`.
6. Signs inside-out: sidecar (identifier, entitlement plist, `--options runtime`, `--timestamp`), launcher, bundle, never `--deep`.
7. Verifies per binary and for the bundle (`codesign --verify --strict`, no `--deep`): hardened-runtime flag, identifier, entitlements read back FAIL-CLOSED (`--entitlements - --xml`, fallback `:-`, cross-check with the human-readable dump; the sidecar must carry exactly `{"com.apple.security.cs.disable-library-validation":true}`, the launcher and bundle none, nobody `get-task-allow`), the Developer ID Application / Developer ID Certification Authority / Apple Root CA chain, a secure timestamp, one Team ID equal to the scope's, `Info.plist` bound, sealed-resource count at least the number of `Licenses/` files.
8. Builds the dmg (`hdiutil` UDRW then UDZO, `Applications` link, volume icon flag), signs it, verifies it.
9. With `--notarize`: **one** `xcrun notarytool submit <signed dmg> --keychain-profile <name> --wait --timeout 30m --output-format json` (this UPLOADS the signed dmg, about 40 MB, to the Apple notary service; the ledger gets a `notary-submit` line and `notary/SUBMISSIONS.txt` records the submission before and after; the script also ends the call itself 90 s after the timeout), `notarytool log`, then, only if `Accepted`, `stapler staple` and `stapler validate` on the dmg.
10. Re-mounts the output, copies the app out, verifies its signature and that the binaries equal the signed ones, runs `spctl` on the dmg and the app copy, runs `stapler validate` on the app copy (EXPECTED: no ticket), writes `EVIDENCE.txt`, `OUTPUT.sha256`, `TRANSCRIPT.txt` and `TRANSCRIPT.sha256` (printed too), removes the staged copies file by file.

Output modes: umask 077, so the output directory is 0700 and every file in it, including the signed dmg, is 0600; the owner changes the mode when handing the dmg on.

What is uploaded to Apple, exactly: the signed dmg file (`out/<dmg>`; contents: the signed app with launcher, sidecar and the eight `Licenses/` files, the volume icon, the `Applications` link). No source, no evidence set, no credentials (the profile is resolved by `notarytool` from the keychain).
What is stapled: the dmg only. The app inside has no ticket; an app copied out relies on Gatekeeper's online lookup (optional improvement, owner decision O-04: notarize and staple the app before building the dmg; not done by the script).

### 5.4 After the run (owner and reviewer)

1. Independent verification from a different account where possible: `packaging/acceptance/macos-r0prime/verify_dmg.sh --expect signed --team-id <Team ID> ...` (authority chain, runtime flags, timestamps, entitlement set, `spctl`, `stapler validate`).
2. Gatekeeper and stapler transcripts: `spctl -a -t open --context context:primary-signature -vv <dmg>`, `xcrun stapler validate <dmg>`, `spctl -a -vv <app copy>`.
3. **Offline-copy test** (UNVERIFIED until done; records, does not fix): on a clean test account of a Mac with the network switched off by hand, put a COPY of the stapled dmg through a quarantine flag
   (`xattr -w com.apple.quarantine "0081;$(printf %x "$(date +%s)");Safari;" <copy>`), mount it, drag the app to a disposable folder, run `spctl -a -vv` and open it from the Finder. Record what Gatekeeper says, with and without the network. Expectation: the dmg validates offline through its ticket; the unstapled app may need the online lookup.
4. Content equality of input and output (review method): signature-stripped binaries equal, `Info.plist`, icon and every `Licenses/` file byte-identical, cdhashes of dmg, launcher/bundle and sidecar present in the notary log's `ticketContents`.
5. Assemble a NEW evidence set for the signed artifacts (statement subjects = signed hashes, notary log, `codesign -dvvv`, stapler output); the unsigned set is not edited.

---------------------------------------------------------------------------------------------------------------------------------------

## 6. Windows: step by step

Scripts: `r0prime_win_signcommand.sh` (on the Windows build host, no secret) and `sign_windows_r0prime_v3.sh` (on the macOS signing host). Jsign 7.6 (`--storetype TRUSTEDSIGNING`) against Azure Artifact Signing, Public Trust.

### 6.1 Why two stages and a hand-off (STATIC, tauri-bundler 2.9.4 sources)

* The bundler signs, when `bundle.windows.signCommand` is set: the main binary (AFTER patching it in place with the package type, once per package type), every `externalBin` sidecar, the NSIS plugin DLLs and WiX extension DLLs (files of its tool cache), the NSIS uninstaller (a hook that runs the command while the installer is built), and the final installers.
  Consequences: (a) an executable cannot be signed before packaging and left alone, because the bundler patches the launcher afterwards; (b) the installers must be rebuilt after the inner files are signed; (c) the bundler would also sign third-party DLLs with whatever the command does, so the command must skip them; (d) the msi and nsis passes see different launcher bytes (explains the two different launcher hashes in the frozen v0.3.2 installers: INFERRED).
* The build host (Windows) has no secret. The signing host (the owner's Mac) holds access to Artifact Signing through the owner's `az` login. Files move by hand, hashes verified at every hop.

### 6.1a Tools, hashes and the scope on the signing host

The signing host resolves every external tool from the fixed system PATH plus an explicit owner-supplied tools directory (`R0P_TOOLS_DIR`, absolute, owned by the owner, not writable by group or others);
`az`, the java executable, the python executable and `osslsigncode` (or `none`) are pinned by sha256 in the scope (`tool_az_sha256`, `tool_java_sha256`, `tool_python_sha256`, `tool_osslsigncode_sha256`), and the Jsign jar and
the verifier are pinned as before. The Azure coordinates (`R0P_AZ_*`) are unset after they are read, so neither `az` nor Jsign inherits them. The inputs are hashed, copied into the private 0700 work directory, **re-hashed
immediately after the copy and again immediately before each Jsign call**; a file that differs from the authorized hash is refused and never signed (round 4, R4-02).

### 6.2 Round 1: collect the inner files (build host)

1. Clean checkout of the exact commit; build per `packaging/build-windows.ps1` up to the launcher and the staged sidecar (`cargo tauri build --no-bundle`). Keep a copy of the unpatched `target\release\agentic-r0prime.exe` (the bundler patches it in place per package type).
2. Write a config override OUTSIDE the repository, e.g. `override.json`: `{"bundle":{"windows":{"signCommand":{"cmd":"bash","args":["<abs>/r0prime_win_signcommand.sh","%1"]}}}}`.
3. For each package type, restoring the unpatched launcher first: `set R0P_HANDOFF_MODE=collect`, `set R0P_HANDOFF_DIR=<abs>\round1`, `cargo tauri bundle --bundles msi --config override.json`, then the same for `nsis`.
   (UNVERIFIED: `tauri bundle` with a config override and a pre-built binary was not run; if it behaves differently, use `cargo tauri build --config override.json` in a fresh checkout per package type.)
4. Result: `round1\requests-inner\<role>-<sha256>.exe` for launcher, sidecar, uninstaller, and `round1\REQUESTS.sha256`, THIS build host's own record of what it asked to have signed (keep it; apply needs it); the unsigned installers are throw-away; unknown executables appear as `unknown-<sha256>.exe` (the signing script refuses that name: review the file, extend the helper, review again).

### 6.3 Stage inner (signing host)

```
export R0P_AZ_ENDPOINT=... R0P_AZ_ACCOUNT=... R0P_AZ_PROFILE=... R0P_AZ_EXPECTED_CN='CN=...'   # typed by the owner
packaging/signing/sign_windows_r0prime_v3.sh --stage inner --input <file> --sha256 <hex> [--input ... --sha256 ...] --outdir "$SIGN/win-inner"            # plan (default)
packaging/signing/sign_windows_r0prime_v3.sh --stage inner ... --real --i-have-owner-authorization <sha256 of scope-win-inner.txt> --scope-file scope-win-inner.txt
```
All files of the stage go in ONE invocation (Jsign makes one extra call per run to fetch the chain). The owner must already be logged in with `az login` as an identity that holds the "Artifact Signing Certificate Profile Signer" role; the token is requested once, into a 0600 file, and read by Jsign through `--storepass file:`.
Jsign settings (unchanged from the frozen v1 run that worked and from v2): `--storetype TRUSTEDSIGNING --keystore <endpoint> --alias <account>/<profile> --alg SHA-256 --tsmode RFC3161 --tsaurl http://timestamp.acs.microsoft.com --tsretries 3 --tsretrywait 10 --name "Agentic Apps R0-prime"`; jar pinned by sha256 (`6cf58aa0...`).
Verification before release (inside `work/`): `verify_signature_v2.py --require-timestamp --expect-cn <exact CN> --expect-eku 1.3.6.1.5.5.7.3.3 --expect-eku 1.3.6.1.4.1.311.97.1.0`; the signed bytes must differ from the input; `osslsigncode` with the Microsoft Identity Verification Root CA 2020 as an informational second opinion (read the `Timestamp Server Signature verification` line, not the exit code: frozen finding F6).

### 6.4 Round 2: rebuild the containers with the signed inner files (build host), then stage container

1. Copy the signed files and the `SHA256SUMS` of the stage-inner run into `round2\signed-inner\` (new directory; hashes re-checked by the helper).
2. Restore the unpatched launcher; `set R0P_HANDOFF_MODE=apply`, `set R0P_HANDOFF_DIR=<abs>\round2`, `set R0P_HANDOFF_REQUESTS=<abs>\round1\REQUESTS.sha256`, `set R0P_EXPECT_SIGNER_CN=<exact CN>`, `set R0P_EXPECT_ISSUER_SUBSTR=<issuer text from the authorization>`, `cargo tauri bundle --bundles msi --config override.json`; again for `nsis`. The helper replaces each inner file by its signed counterpart ONLY when the incoming file's unsigned hash is in THIS host's `REQUESTS.sha256`, `signed-inner/<role>-<sha256 of the incoming file>.exe` exists, matches `SHA256SUMS`, and `powershell.exe Get-AuthenticodeSignature` on it reports Status `Valid`, a signer subject whose first CN is exactly `R0P_EXPECT_SIGNER_CN`, an issuer containing `R0P_EXPECT_ISSUER_SUBSTR` and a timestamper certificate (round 4, R4-07; UNVERIFIED on a real Windows host); a file that is already one of the signed files is accepted; anything else FAILS the build (it never lets an unsigned inner file through); containers are copied to `round2\requests-container\` unsigned.
   (UNVERIFIED: that the NSIS uninstaller and the patched launcher have identical bytes in both rounds. If they do not, the lookup misses and the build fails: fail-closed, then use the online alternative below.)
3. On the build host record the **file list of each rebuilt installer** (for example `msiexec /a <msi> /qn TARGETDIR=<temp>` then a sorted listing with sha256, and the same for the NSIS exe with 7-Zip) into one text file. The container stage in `--real` REQUIRES it (`--container-listing FILE`, hash in the scope as `container_listing_sha256`) and records its hash and line count in the evidence. **The container signing does not inspect the installer contents**: the signature vouches for whatever the hand-off delivered, so the listing (inner file hashes equal to the signed `SHA256SUMS`) must be reviewed before the run, and the Windows acceptance kit must check the installed files afterwards.
4. Check the container names equal the derived ones (`Agentic Apps R0-prime_0.1.1_x64_en-US.msi`, `Agentic Apps R0-prime_0.1.1_x64-setup.exe`), hand them to the signing host and run `--stage container` (same flags, own scope file).
5. On a Windows host: `Get-AuthenticodeSignature` on both containers and on the installed `agentic-r0prime.exe`, `redevops-sidecar.exe`, `uninstall.exe` (Windows acceptance kit step 11). The signing script cannot see inside the containers.

Online alternative (not the default): run the build on a host the owner controls and let `signCommand` call a script outside the repository that signs with the owner's `az` session. That puts Azure access on the build host; never in CI without a separate decision.
Alternative of v0.3.2: sign only the two containers (no inner signing). Weaker: the installed executables stay unsigned.

### 6.5 3-day certificates, timestamps, windows of time

* Public Trust certificates issued by Artifact Signing are valid for 3 days (the frozen leaf: 2026-11-04 03:12:51Z to 2026-11-07 03:12:51Z). Sign inside the authorized window and inside one certificate period, run stage inner and stage container close together, and **always apply the RFC 3161 timestamp**: after the leaf expires, a signature is acceptable only because of its timestamp countersignature. The scripts refuse a run without `--require-timestamp` verification in `--real`.
* The dated frozen-signature check (owner decision 9, on or after 2026-11-07T03:13Z: `packaging/acceptance/windows-r0prime/DATED-CHECK-RUNBOOK.md`) shows whether a real Windows host honours the timestamp after the leaf expired. **Do not rely on Windows signing for R0-prime before that check has been performed and recorded** (recommended gate; UNVERIFIED until the check exists).
* Quota: Basic SKU; expected `1 + number of files` sign-API calls per run (from Jsign's source, not observed). Two stages mean two runs.

### 6.6 Caveats carried over from the frozen ERRATA (still true)

* **MsiDigitalSignatureEx**: Jsign 7.6 does not write that stream into an MSI (osslsigncode warns). The frozen MSI was `Valid` and installed/removed on Windows Server 2022 without it (observed once); Windows 10/11, interactive installs, SmartScreen and `signtool verify /pa` were not tested.
* Azure sign calls and the leaf's CRL/OCSP behaviour are not observed by the scripts; the offline verifier does not check revocation.
* A new publisher identity (a new product name and signer) starts without SmartScreen reputation; Defender can flag an unsigned PyInstaller onefile.
* The signatures cover files, not behaviour: "signed" is not "works" is not "supported".

---------------------------------------------------------------------------------------------------------------------------------------

## 7. The evidence manifest (SSHSIG)

`SSHSIG-manifest-signing.md` and `sign_manifest_sshsig.sh`: namespace `redevops-qa-release@v1`, the EXISTING release key (public fingerprint in `SIGNING_REQUEST.md`), used only through the owner's ssh-agent
after separate explicit authorization; the signature is streamed (stdin to stdout) into a file outside the evidence directory so that no file is added to the set; verification with the public key only.
The key is never read or exported by any script, and it is never placed in an agent for a test. Since round 4 the script copies the PUBLIC key into a private directory, requires the fingerprint in `ssh-add -l` of the owner's agent, signs with `-U` under a throwaway HOME and `SSH_ASKPASS=/usr/bin/false`, and refuses otherwise: it can no longer fall back to a private key file beside the `.pub` (R4-01). The published release fingerprint is `SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA`; the self-test refuses a key whose fingerprint equals it, and `--real` records whether the key matches it.

---------------------------------------------------------------------------------------------------------------------------------------

## 8. Evidence to produce after signing (kept together, hashes in one table)

macOS: `EVIDENCE.txt`, `TRANSCRIPT.txt` + `TRANSCRIPT.sha256`, `OUTPUT.sha256`, `codesign/*.dvvv.txt` (sidecar, launcher, bundle, dmg), the entitlement dumps (`*.entitlements.plist` and `.human.txt`: an empty launcher plist is the proof of "none"), `codesign/SIGNED-BINARIES.sha256` (post-signing hashes), `notary/submit.json`, `notary/log.json` (+ sha256), `notary/staple.txt`, `notary/staple-validate.txt`, `verify/*.txt`, `FAILED.txt` if a run failed (a failed run is never reused), the independent `verify_dmg.sh --expect signed` transcript, the offline-copy test record, the spctl/stapler outputs.
Windows: `EVIDENCE.txt`, `SHA256SUMS` of each stage (pre- and post-signing hashes of every file), the `signcommand.log` of each round, verifier output and the osslsigncode summary, the Windows-side `Get-AuthenticodeSignature` of containers and installed files.
Manifest: the `.sig` file, the record file of `sign_manifest_sshsig.sh` and the verification line.
Plus: an ERRATA file for any statement in the evidence that turned out wrong (the frozen set's practice); the sha256 of the signing scripts, plist and verifier; which statements are UNVERIFIED. Public material only: no token, key, profile name or identity string is in any of them (the certificate's own name appears in `codesign -dvvv` Authority lines, as in the frozen evidence).

---------------------------------------------------------------------------------------------------------------------------------------

## 9. Abort and rollback rules

* Any gate fails or any doubt: stop. Nothing is retried, nothing is "fixed up" in place; use a NEW `--out-dir`/`--outdir` for the next attempt.
* **Notarization**: one submission per authorization. If the status is not `Accepted` (Invalid, Rejected, timeout, network error): do not re-sign, re-upload or resubmit without the owner's explicit OK; keep `notary/submit.json` and the log; never reuse a failed submission or its output dmg; if the status is unknown after the timeout, ask Apple's service about the submission id with `xcrun notarytool info <id> --keychain-profile ...` instead of uploading again.
  Time-box: `--notary-timeout` (default 30m); the owner decides in advance how long to wait overall (suggested: one working session) and records it in the authorization.
* A failed run leaves `FAILED.txt` (macOS) or `FAILED` (Windows) with the reason and nothing signed in the Windows outdir; macOS keeps `work/` for diagnosis; neither is a deliverable.
* If a signature was made but a later check fails: the signed file is quarantined (kept for diagnosis, never distributed), the cause is written in an ERRATA/decision note, and the next attempt starts from the unsigned input again.
* A certificate or key problem (suspected exposure, wrong identity used, wrong file signed): stop, tell the owner at once, and let the owner decide about revocation with the issuer (Apple, Azure); the scripts cannot revoke anything.
* Interrupts (INT/TERM/HUP) end the child tool, remove what the Windows script wrote and write the failure marker; mounts made by the macOS script are detached by its trap.

---------------------------------------------------------------------------------------------------------------------------------------

## 10. Secrets handling

* No credential is a command-line argument, a file in the repository, a CI secret, a chat message or an environment variable of a child process: the identity and profile come from `R0P_*` variables of the owner's own shell and are unset right after they are read; the environment of every script must be the allowlist (the scripts re-execute under `env -i` otherwise, and nothing can skip that); PATH is fixed.
* The macOS scripts print a placeholder instead of the identity and the profile; the transcript records `sha256(identity)` only. The Windows script writes the Azure token with `az` straight into a 0600 file in a 0700 directory, hands Jsign `file:<path>`, truncates and removes the file as soon as Jsign returns and on every failure path, and never echoes it.
* Transcripts, evidence and CI artifacts are reviewed for secrets before they leave the machine; the scripts never use `set -x`, and refuse to run under xtrace.
* Keys: no script reads a private key. The SSHSIG key is used through the owner's agent only. Test keys are throwaway files in temporary directories, deleted by name, never in an agent.
* Nothing in `~/.ssh`, `~/.gnupg`, the Keychain or `~/.azure` is read by this package; `az` and `codesign` read their own state when the owner runs them.

---------------------------------------------------------------------------------------------------------------------------------------

## 11. Risks (residual, to be accepted knowingly)

* **R1 Hardened runtime with `disable-library-validation` on the sidecar.** The onefile sidecar extracts six ad-hoc-signed Mach-O files to `$TMPDIR/_MEI*` at every start and loads them. Under the hardened runtime without the entitlement it does not start (OBSERVED, ad-hoc test: "different Team IDs"); with it, library validation is off for that process. Residual: a same-user writer (or root) can replace an extracted module between extraction and import and run code inside the sidecar, which holds the launch secret, the API session and the user's file access (OBSERVED proof of concept; it works identically on the current unhardened candidate). The hardened runtime without `get-task-allow` still blocks `DYLD_*` injection (OBSERVED) and debugger attach (INFERRED). The six libraries are not covered by the notarization ticket. Removal path: a onedir sidecar with every library signed by the Team ID, no entitlement. Behaviour with a REAL Developer ID, notarization and Gatekeeper is UNVERIFIED (INFERRED from the ad-hoc matrix).
* **R2 Same-user dylib planting** is the concrete form of R1; it is outside the threat model of `docs/LOCAL_API_SECURITY.md` (same-user malware) and is documented, not mitigated, by signing.
* **R3 3-day Windows certificates.** A signature without a verifiable RFC 3161 timestamp is useless after three days; re-signing is needed for every release; a failed or skipped timestamp must fail the run (it does, in `--real`).
* **R4 Timestamp-service dependency.** Both platforms depend on a third-party timestamp service at signing time (Apple's, `timestamp.acs.microsoft.com`); an outage blocks or degrades signing and the scripts do not work around it. Windows verification here is offline and does not prove revocation status.
* **R5 SSHSIG has no time and no identity.** The signature proves a holder of the key signed these bytes; it carries no time, no signer identity and no revocation; the date comes only from the record the owner keeps.
* **R6 Unreviewed real paths.** The `--real` branches of both scripts have never run (the frozen v2 production branches had not either); the first run is a first run. Notarization behaviour with the new bundle identifier and entitlements, and Azure behaviour with the new product name, are UNVERIFIED.
* **R7 Hand-off fragility (Windows).** The apply step depends on byte-identical unsigned inputs in both rounds (STATIC reasoning about the bundler); a miss fails closed but costs a round.
* **R8 Provenance.** A signature binds bytes to the identity; it does not show how the bytes were built. The evidence set and the clean-checkout build are the provenance; tag/commit history of the frozen line was rewritten (frozen ERRATA 3) and pins-04 (the exact commit public before it is referenced) is open.
* **R9 Quota and cost.** Artifact Signing is billed per signature beyond the included quota; the owner confirms before a Windows run (the script prints the expected call count).

---------------------------------------------------------------------------------------------------------------------------------------

* **R12 Tool pins cover executables only.** On Windows the hashes of `az`, java, the python executable and osslsigncode, plus the sorted list of installed python distributions with versions (names and versions of `signify` and its dependencies), are bound by the scope; the FILES of the libraries, the JDK tree and az's modules are not hashed, and a tool is hashed once and run later (owner-only directories). Pin executables, not trees: this is a documented residual (a manifest of tool trees would close it). `--tools-dir` (jsign/jdk location) is a separate unvalidated option; the Jsign jar is pinned by hash.
* **R13 Visible names.** The identity string and the notary profile name appear in the argv of `codesign` and `xcrun notarytool` (as the Authority line appears in `codesign -dvvv`); they are names, not credentials. The Windows hand-off helper takes `powershell.exe` from `%SystemRoot%\System32` (absolute path, not PATH).
* **R10 The scope file is not an authentication** (3.4). A passing scope check proves only that the stated hashes, window and flags agree; the owner's terminal and credentials are the authentication; the ledger stops accidental repeats, not a deliberate one by someone who can edit the ledger.
* **R11 Gate semantics.** The gate proves that written decisions exist in the repository copy bound by hash; it is not a legal conclusion and a repository writer can still edit the gate file (the hash binding makes that visible to the reviewer, not impossible).

## 12. UNVERIFIED (consolidated)

Real Developer ID signing and notarization of the new bundle id/entitlement; `codesign -d --entitlements - --xml` output on a Developer-ID-signed, timestamped binary; offline first launch of an app copied from a stapled dmg; hardened-runtime behaviour of the signed sidecar through "Start & open" with a Team ID; every Azure call; Jsign output for the new names; `tauri bundle` with `signCommand` and pre-built binaries; the PowerShell signature check of the hand-off helper (no Windows host); whether `ssh-keygen -Y sign -U` with a real agent behaves as the fake-agent tests show; byte-identity of the uninstaller and launcher across rounds; whether files inside the containers carry the signatures; Windows 10/11 behaviour of a Jsign-signed MSI without `MsiDigitalSignatureEx`; behaviour of the dated frozen-signature check; bash other than 3.2.57; Linux.
