# Differences of the R0-prime scripts against the frozen v2 scripts

The frozen scripts (v0.3.2 line) are read-only and unchanged:
`macos-agentic-apps-v0.3.2/sign_and_notarize_macos_v2.sh` (sha256 `484a195e...3af7` as listed in `SIGNING_REQUEST.md` section 7) and
`windows-agentic-apps-v0.3.2/sign_windows_artifact_signing_v2.sh`, `script/verify_signature_v2.py` (copied here byte-identically, sha256 `e43b6f0f...7de3`).
To regenerate the raw diffs: `diff -u <frozen script> packaging/signing/<new script>`. Below, every difference that changes behaviour, by cause.

## A. `sign_and_notarize_r0prime_macos_v3.sh` against `sign_and_notarize_macos_v2.sh`

### A.1 Hard-coded frozen names (the reason for a new script)
| v2 (frozen, hard-coded) | v3 (derived from `packaging/identity.json` by `plutil`, same formulas as `packaging/tools/identity.py`) |
|---|---|
| `APP_NAME="Agentic Apps.app"` | `<product_name>.app` = `Agentic Apps R0-prime.app` |
| `MAIN_EXE="agentic-apps"` | `main_binary_name` = `agentic-r0prime` |
| `SIDECAR_EXE="redevops-sidecar"` | `sidecar_binary_name` (unchanged value) |
| `SIDECAR_ID="io.redevops.agentic-apps.sidecar"` | `sidecar_signing_identifier` = `io.redevops.agentic-apps.r0prime.sidecar` |
| bundle identifier not checked | `bundle_identifier` = `io.redevops.agentic-apps.r0prime`: checked in Info.plist, on the launcher and the bundle after signing |
| `VOLNAME="Agentic Apps"`, `--volname`, `--output-name` options | volume = product name; output name = `Agentic Apps R0-prime_0.1.1_aarch64.dmg`; both options removed (no override, so no collision with a frozen name) |
| header "Agentic Apps 0.1.0 ... v0.3.2 ... run 35353188692" | header from identity.json; the script refuses an identity that equals the frozen line |
| Info.plist not checked | identifier, executable, `CFBundleShortVersionString`, `CFBundleVersion`, `LSMinimumSystemVersion` 11.0, `RedevopsReleaseLine` must equal identity.json |
| a frozen dmg would be accepted | a dmg that holds `Agentic Apps.app` is refused ("not found in the input dmg") |

### A.2 New layout (R0-prime: `Resources/Licenses/`, sidecar as an external binary in `Contents/MacOS`)
* Bundle must hold exactly two entries in `Contents/MacOS` and none of `Frameworks`, `PlugIns`, `Helpers`, `XPCServices`, `_CodeSignature`, `SharedSupport`, `Library`.
* `Resources/Licenses/` must exist, hold at least one file, and no file there may be executable (v2 guarded executables globally; v3 keeps that guard).
* Added: a Mach-O magic scan of every file of the bundle (a library without `.dylib`/`.so` name or exec bit would be missed by the v2 find), `lipo -archs` must be exactly `arm64` for both binaries.
* Added: the dmg ROOT of the input must contain only the app, `.VolumeIcon.icns`, `Applications` (and the OS bookkeeping names); anything else would be silently dropped by the rebuilt dmg, so it is refused (for example a `Licenses/` folder at the dmg root, owner decision O-03).
* Added: after signing, the bundle's `Info.plist` must be bound and the sealed-resource count must be at least the number of `Licenses/` files (readiness section 2.1 of `SIGNING-READINESS.md`).

### A.3 Modes, identity and authorization
| v2 | v3 |
|---|---|
| `--dry-run` = the whole sequence with ad-hoc `-` | `--adhoc-test` (same sequence, output named `ADHOC-TEST-ONLY_*`, marker file, no notarization) |
| no plan mode | `--dry-run` is the DEFAULT and only prints the `--real --notarize` command sequence; creates and mounts nothing |
| `--identity "Developer ID Application: ..."` on the command line | identity ONLY from `R0P_CODESIGN_IDENTITY` (never an argument, never printed; the transcript has a placeholder, the evidence `sha256(identity)`) |
| `--notary-profile NAME` | `R0P_NOTARY_PROFILE` (name only, never recorded) |
| real mode = `--identity` (+ `--notarize`) | `--real` + `--i-have-owner-authorization <sha256 of scope file>` + `--scope-file` + `--expected-sha256` + env; the scope file binds script sha256, entitlement plist sha256, input sha256, identity sha256, Team ID, notarize/upload, UTC date window |
| `--dry-run` refused a real `--identity` | any `R0P_CODESIGN_IDENTITY`/`R0P_NOTARY_PROFILE` in a NON-real mode is refused |
| none | the Team ID of every signature must equal the scope's; the Developer ID Certification Authority and Apple Root CA must be in the chain |

### A.4 Safety additions
* `umask 077` (v2: none); `set +x` and a refusal when xtrace is on (checked before the re-exec); refusal when run as root (as v2).
* Re-execution under `env -i` with an allowlist (PATH, HOME, LANG, TERM, `R0P_*`); v2 only unset three variables.
* `--real`: tools must resolve to system locations (`/usr/bin`, `/usr/sbin`, `/bin`, `/sbin`, CLT, Xcode); `R0P_ALLOW_NONSYSTEM_TOOLS=stub-test` exists only for the unit tests and is printed in the evidence.
* The input must be a regular file, not a symlink, and must not sit in a directory that looks like a candidate/evidence set (names `macos-arm64-unsigned*`, `evidence-macos-arm64*`, `candidate/`, marker files `README-LOCAL-BUILD.md`, `build-manifest.json`, `release-manifest.json`, `SHA256SUMS.candidate`, `SIGNING_REQUEST.md`) or in a `--protected-dir`; the output directory must not look like one either.
* A transcript of everything printed: `TRANSCRIPT.txt`, with `TRANSCRIPT.sha256` written beside it and printed (v2: EVIDENCE.txt only).
* Output hash recorded in `OUTPUT.sha256`; post-signing hashes of both binaries and Info.plist in `codesign/SIGNED-BINARIES.sha256`; the app copied out of the output dmg must contain exactly those signed binaries (v2 only recorded their hashes).
* `FAILED.txt` records the reason (`fail` notes), states that a failed run is never reused and that a failed notarization is not re-uploaded without the owner's OK; INT/TERM/HUP exit 130 (the trap detaches mounts).
* `diskutil info` failure on the input mount no longer aborts the evidence step (it is only a label).

### A.5 Verification changes
* v2 verified the bundle with `codesign --verify --strict --deep`. v3 does NOT use `--deep` anywhere: it verifies each binary and the bundle with `--verify --strict` (OBSERVED on an ad-hoc copy: strict verification of the bundle validates the nested sidecar and fails with "a sealed resource is missing or invalid" when a `Licenses/` file is changed).
* The identifier of the sidecar, launcher and bundle is checked against identity.json (v2 did not check identifiers).
* The dmg's authority, timestamp and Team ID are checked in `--real` (v2 recorded them only).

### A.6 Notarization changes
* `notarytool submit` gets `--timeout` (default 30m, option `--notary-timeout`): time-boxing; v2 waited without limit.
* The evidence states what was uploaded (the signed dmg's name and sha256). A non-Accepted status aborts with advice not to resubmit; an unknown status after a timeout points to `notarytool info <id>` rather than a new upload.
* A pre-notarization check failure (any codesign, entitlement, Team ID check) aborts BEFORE any upload (tested).
* The scope file must say `notarize=yes`/`upload_to=apple-notary` for `--notarize` and `notarize=no` otherwise.

### A.7 Kept from v2 (the reviewed behaviours)
Private sha256-verified copy of the input attached read-only; inside-out signing order and flags (`--force --timestamp --options runtime`, `--identifier` and `--entitlements` on the sidecar only); fail-closed entitlement reading (`--entitlements - --xml`, fallback `:-`, human cross-check, `Executable=` required); expected entitlement JSON exactly the one key; no `get-task-allow`; dmg rebuilt with the Applications link and volume icon flag; only the dmg is stapled and `stapler validate` on the app copy is recorded as EXPECTED-no-ticket; `spctl` on the dmg and the app copy; tidy of work files file by file (`purge_tree`, never `rm -rf`); exact-path unsafe `--out-dir` check; no `sudo`, no `security`.

## B. `sign_windows_r0prime_v3.sh` against `sign_windows_artifact_signing_v2.sh`

| Topic | v2 | v3 |
|---|---|---|
| Names | any `.exe`/`.msi` | by role: stage `inner` = `<launcher|sidecar|uninstaller>-<sha256>.exe` (the sha256 in the name must be the file's); stage `container` = exactly the derived MSI / NSIS names; frozen names refused; `--stage` is mandatory |
| Modes | `--dry-run` = throwaway-key signing; `--confirm-production` | `--dry-run` DEFAULT = plan only (validates, prints, creates nothing); `--throwaway-test` = v2's dry run; `--real` + authorization replaces `--confirm-production` |
| Authorization | `--confirm-production` | `--i-have-owner-authorization <sha256>` + `--scope-file` (window, script sha256, verifier sha256, stage, sorted input hashes, endpoint, account, profile, expected CN) |
| Service coordinates | defaults in the script (`redevops`, `redevops-public`, `eus` endpoint, `CN=ReDevOps.io LLC`) and flags `--endpoint/--account/--profile/--expected-subject` | no defaults and no flags: `R0P_AZ_ENDPOINT`, `R0P_AZ_ACCOUNT`, `R0P_AZ_PROFILE`, `R0P_AZ_EXPECTED_CN` supplied by the owner; the same F3 regexes run first in every mode; the expected CN is mandatory in `--real` |
| Application name | `Agentic Apps` | product name from identity.json (`--name` still possible, validated) |
| Environment | `env -u` of four variables for the child tools | `env -i` allowlist re-exec for the whole script; xtrace refused |
| Hung signer | no limit | watchdog `R0P_SIGN_TIMEOUT_SECONDS` (default 900) ends Jsign; failure marker |
| "Jsign signed nothing" | caught only by the verifier | additionally: every signed copy must differ from its input |
| Evidence | files, tool versions, verification | adds stage, role per file, authorization hash, stage-specific limits (inner not yet in any installer; container contents unchecked), MsiDigitalSignatureEx and 3-day certificate notes |
| FAILED marker | `FAILED-ATTEMPT sign_windows_artifact_signing_v2` | `FAILED-ATTEMPT sign_windows_r0prime_v3`; says a new run asks the service again (quota) |
| Hand-off | none (files given directly) | new `r0prime_win_signcommand.sh` for the Windows build host (collect/apply, no secret); see the procedure section 6 |
| Verifier | `verify_signature_v2.py` | the same file, byte-identical (sha256 `e43b6f0f...7de3`), copied next to the script and bound in the scope file |

Kept: the Jsign jar sha256 pin, the Jsign/`az` command lines (`--storetype TRUSTEDSIGNING --keystore <endpoint> --storepass file:<token file> --alias <account>/<profile> --alg SHA-256 --tsmode RFC3161 --tsaurl http://timestamp.acs.microsoft.com --tsretries 3 --tsretrywait 10`), the token handling (0600 file, 0700 directory, wiped on exit and on every failure path), the unsigned-input pre-check (fail closed), the output rules (new outdir or a lone FAILED marker, files released only after verification, no rm -rf), signal handling, the osslsigncode informational summary (exit code ignored, timestamp line parsed), local-path scrubbing in the evidence, the bash 3.2 compatibility.
Not changed and still UNVERIFIED: the `--real` branch has never run against Azure (neither had v2's).

## C. New files without a frozen counterpart
`sign_manifest_sshsig.sh`, `r0prime_win_signcommand.sh`, `tools/make_adhoc_fixture.sh`, `AUTHORIZATION-TEMPLATE.md`, `SSHSIG-manifest-signing.md`, `SIGNING-PROCEDURE-R0PRIME.md`, this file, `README.md`, `tests/test_signing_procedure_r0prime.py`.
