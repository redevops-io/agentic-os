# Authorization template (owner fills in; one block per approval)

This file is a TEMPLATE. Nothing here is an approval, and no filled-in copy belongs in the repository: keep your copy outside it, together with the scope files it names.
Never put a key, token, password, certificate password, Apple credential or ssh-agent socket into this form, a scope file, a chat message or an issue. The scripts want only
public facts (hashes, a Team ID, a certificate CN, a fingerprint, dates) and the NAMES of things you export in your own shell.

**The scope file is a TRIPWIRE against mistakes, not an authentication** (procedure section 3.4): anyone who can write a text file can make one that passes. Your own terminal, keychain identity,
notarytool profile, `az` login and ssh-agent are what authenticate a real run; no script and no agent holds them. Scope files are **version 2** (below): a UTC window of at most 24 hours, a one-time ledger,
commit ids, the evidence manifest digest and the hashes of the shared library, `identity.json` and the distribution gate are part of them.

How a scope file works: you write the facts below as `KEY=VALUE` lines in a plain text file (`#` comments allowed, no other syntax, unknown or repeated keys are refused). You give the SHA-256 of
that file to the script as `--i-have-owner-authorization <sha256>` and the file as `--scope-file`. The script compares every fact with what it is about to use and refuses on any difference, so an
authorization cannot be re-used for another file, another script version, another identity or another day. The scope file is written by you; an agent that has the template can draft one for your
review but cannot approve it.

---------------------------------------------------------------------------------------------------------------------------------------

## 0. General statements (sign once per signing session)

* I, the owner, authorize the approvals ticked below, and only those. Refusing one does not refuse another; signing the manifest does not sign the dmg and signing the dmg does not sign the manifest.
* Gates G1 to G10 of `SIGNING-PROCEDURE-R0PRIME.md` section 3.1 hold (initial each): G1 ___ G2 ___ G3 ___ G4 ___ G5 ___ G6 ___ G7 ___ G8 ___ G9 ___ G10 ___
* This is a PRIVATE candidate signing: [ ] yes (scope `distribution=no`; the run prints the open decisions and the statement that signing does not imply permission to distribute)  [ ] no, it is a DISTRIBUTION candidate (scope `distribution=yes` and `--distribution`: the script runs `check-distribution-gate.py --platform <p> --distribution` itself and refuses while any blocker is OPEN; decisions recorded in `docs/legal/OWNER_DECISION_FORM.md`).
* Signing does not publish, tag, release or upload anything except what a block below names. Where the signed files may be stored afterwards: ______________________
* Time window of this session (UTC): from ____-__-__T__:__:__Z to ____-__-__T__:__:__Z (at most 24 hours; the scripts refuse a longer one). Outside it nothing may be signed under this authorization.
* Ledger DIRECTORY (absolute path, outside the repository, created by the script; hash-chained events; a scope file already claimed in it is refused unless `--allow-reuse` is given and recorded): ______________________
* Build commit (40 hex, the commit the dmg/installers were built from) and procedure commit (40 hex, the commit of `packaging/signing`): ______________________ / ______________________ (recorded in the evidence, not verified by the scripts)
* Who runs the commands, at their own terminal: ______________________ . Who reviews the output (not the person who ran it): ______________________
* Evidence set the dmg belongs to (directory, manifest sha256 obtained out of band): ______________________ / ______________________________________________________________

---------------------------------------------------------------------------------------------------------------------------------------

## 1. macOS: sign the dmg (A-01 to A-04) and optionally notarize + staple (A-05, A-06)

What: sign sidecar (with the entitlement plist, hardened runtime, secure timestamp), launcher, bundle, dmg; verify. [ ] approved
Notarize: submit the signed dmg to the Apple notary service (THIS UPLOADS THE SIGNED DMG TO APPLE), then staple the dmg only.  [ ] approved  [ ] not approved (sign only)

| Fact | Value |
|---|---|
| Input dmg sha256 (recomputed on the signing machine) | |
| Input dmg name as built (the script derives the output name from `packaging/identity.json`) | |
| Script sha256 (`shasum -a 256 packaging/signing/sign_and_notarize_r0prime_macos_v3.sh`) and `signing-common.sh` sha256 | |
| `packaging/identity.json` sha256; `packaging/distribution-gate.json` sha256; `packaging/check-distribution-gate.py` sha256 | |
| RC3 About pins (scope_version 3): sha256 of `packaging/notices/check_about.py`, `packaging/notices/generate_about.py`, `agentic_os/about_record.py` and `packaging/legal-allowed-signers`; the build commit (40 hex) must be the `source.commit` of the about.json inside the artifact | |
| Evidence manifest sha256 of the evidence set this dmg belongs to (obtained out of band) | |
| Entitlement plist sha256 (`packaging/macos/sidecar.entitlements.plist`) | |
| Identity: sha256 of the EXACT identity string you will export (`printf '%s' "$R0P_CODESIGN_IDENTITY" \| shasum -a 256`) | |
| Notary profile NAME: sha256 of the exact name you will export as `R0P_NOTARY_PROFILE` (or `none` for a sign-only run) | |
| Team ID (10 characters; public) | |
| Notarization: yes/no; upload target | yes + apple-notary / no + none |
| Overall time-box for notarization (one working session suggested) | |
| Where the signed dmg may be kept | |
| Entitlement decision: the sidecar-only `com.apple.security.cs.disable-library-validation` is accepted for THIS named build, residual risk R1 read | [ ] accepted |

**RC3 (scope_version 3, macOS and Windows scope files only; the SSHSIG manifest script is unchanged and keeps scope_version 2).** The About check
that runs inside `--real` is pinned like everything else that executes: the scope binds `check_about_sha256`, `generate_about_sha256`,
`about_record_sha256` and `legal_signers_sha256` (and, as before, `gate_tool_sha256` for `check-distribution-gate.py`). The checker runs as
`python -I -B` from a private copy of those files made in the script's work directory and re-hashed after copying. The `source.commit` in the
about.json inside the artifact must equal the scope's `build_commit`; a mismatch refuses the run before anything is signed. `legal_signers_sha256`
binds the signer list only as repository data: the owner confirms the key fingerprints in `legal_signer_fingerprints` against an independent
channel (their published key page) before a distribution run.

Scope file `scope-macos.txt`:
```
scope_version=3
not_before=YYYY-MM-DDTHH:MM:SSZ
not_after=YYYY-MM-DDTHH:MM:SSZ
script_sha256=<script sha256>
common_sha256=<signing-common.sh sha256>
entitlements_sha256=<plist sha256>
identity_json_sha256=<packaging/identity.json sha256>
input_dmg_sha256=<input dmg sha256>
identity_sha256=<sha256 of the identity string>
notary_profile_sha256=<sha256 of the profile name, or none>
team_id=<Team ID>
notarize=yes
upload_to=apple-notary
distribution=no
gate_sha256=<packaging/distribution-gate.json sha256>
gate_tool_sha256=<packaging/check-distribution-gate.py sha256>
evidence_manifest_sha256=<manifest sha256>
build_commit=<40 hex>
procedure_commit=<40 hex>
check_about_sha256=<packaging/notices/check_about.py sha256>
generate_about_sha256=<packaging/notices/generate_about.py sha256>
about_record_sha256=<agentic_os/about_record.py sha256>
legal_signers_sha256=<packaging/legal-allowed-signers sha256>
```
(`notarize=no`, `upload_to=none` and `notary_profile_sha256=none` for a sign-only run; then do not pass `--notarize`. `distribution=yes` only together with `--distribution`.)

Command, run by you:
```
read -r R0P_CODESIGN_IDENTITY; read -r R0P_NOTARY_PROFILE; export R0P_CODESIGN_IDENTITY R0P_NOTARY_PROFILE
packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --real --notarize --i-have-owner-authorization "$(shasum -a 256 scope-macos.txt | awk '{print $1}')" \
    --scope-file scope-macos.txt --ledger <absolute ledger path> --input-dmg <copy of the dmg> --expected-sha256 <input dmg sha256> --out-dir <new directory>
```
I understand: one submission only; a failed or timed-out submission is never retried or re-uploaded without my explicit new OK; the output of a failed run is never used.

Signed: ______________________  Date: ____-__-__

---------------------------------------------------------------------------------------------------------------------------------------

## 2. Windows stage `inner`: launcher, sidecar, NSIS uninstaller (Artifact Signing, Public Trust)

What: Authenticode-sign the files listed below with the secure timestamp. [ ] approved  (sends file digests to Azure Artifact Signing; may count against the signature quota: expected calls = 1 + number of files)

| Fact | Value |
|---|---|
| Stage | inner |
| Input files and sha256 (each named `<role>-<sha256>.exe`) | |
| Script sha256 (`sign_windows_r0prime_v3.sh`) / verifier sha256 (`verify_signature_v2.py`, frozen: `e43b6f0f...7de3`) | |
| Endpoint / account / certificate profile (public coordinates you will export as `R0P_AZ_ENDPOINT`, `R0P_AZ_ACCOUNT`, `R0P_AZ_PROFILE`) | |
| Exact certificate CN (`R0P_AZ_EXPECTED_CN=CN=...`) | |
| Certificate validity window expected (3-day Public Trust certificates) | |
| I have confirmed the quota/charges are acceptable | [ ] |
| The dated frozen-signature check (on or after 2026-11-07T03:13Z) has been performed and its result recorded | [ ] |

Scope file `scope-win-inner.txt`:
```
scope_version=3
not_before=YYYY-MM-DDTHH:MM:SSZ
not_after=YYYY-MM-DDTHH:MM:SSZ
script_sha256=<script sha256>
common_sha256=<signing-common.sh sha256>
verifier_sha256=<verifier sha256>
identity_json_sha256=<packaging/identity.json sha256>
stage=inner
inputs_sha256=<the input sha256 values, sorted ascending, comma separated, no spaces>
az_endpoint=<endpoint>
az_account=<account>
az_profile=<profile>
expected_cn=CN=<exact common name>
gate_sha256=<packaging/distribution-gate.json sha256>
gate_tool_sha256=<packaging/check-distribution-gate.py sha256>
distribution=no
evidence_manifest_sha256=<manifest sha256>
build_commit=<40 hex>
procedure_commit=<40 hex>
check_about_sha256=<packaging/notices/check_about.py sha256>
generate_about_sha256=<packaging/notices/generate_about.py sha256>
about_record_sha256=<agentic_os/about_record.py sha256>
legal_signers_sha256=<packaging/legal-allowed-signers sha256>
tool_az_sha256=<sha256 of the az executable the fixed PATH + R0P_TOOLS_DIR resolves to>
tool_java_sha256=<sha256 of the java executable>
tool_python_sha256=<sha256 of the python executable given to --python>
tool_python_packages_sha256=<sha256 of the sorted "name==version" list of the python distributions, one per line, with a final newline>
tool_python_pth_sha256=<sha256 of the sorted "<relative path> <sha256>" lines of every .pth file under <venv>/lib, one per line, with a final newline; the hash of a single newline when there is none>
tool_osslsigncode_sha256=<sha256 of osslsigncode, or none>
container_listing_sha256=none
```
(Build the python venv fresh from the pinned wheels (with hashes) on the day of the run; an old venv is not reused. These pins cover executables and the python package NAMES and VERSIONS only; library files are not hashed: procedure R12.) Also supply `R0P_TOOLS_DIR` (absolute, yours, not writable by others) when az/java/python live outside the system directories; pass `--ledger <absolute path of a DIRECTORY; the script creates it>`.
You are logged in with `az login` yourself before the run; the script asks `az` for one access token, keeps it in a 0600 file for the duration of the Jsign call and removes it.

Signed: ______________________  Date: ____-__-__

---------------------------------------------------------------------------------------------------------------------------------------

## 3. Windows stage `container`: MSI and NSIS setup exe

Same table and scope file as section 2 with `stage=container`, the two installer names exactly as derived from `packaging/identity.json`, and `inputs_sha256` the hashes of the REBUILT installers (rebuilt from the signed inner files).
`container_listing_sha256` is the sha256 of the file list of the rebuilt installers made on the Windows build host (pass it with `--container-listing FILE`). The container signing does NOT inspect the installers; you or the reviewer check that
list (inner file hashes equal to the signed ones) before the run.
[ ] approved. This stage is run inside the same certificate period and authorization window as stage `inner` where possible.

Signed: ______________________  Date: ____-__-__

---------------------------------------------------------------------------------------------------------------------------------------

## 4. Evidence manifest SSHSIG (M-01)

What: sign the top-level `release-manifest.json` of the evidence set named in section 0 with the existing release key, namespace `redevops-qa-release@v1`, through MY ssh-agent. [ ] approved
The signature is written outside the evidence directory; the evidence directory is not changed.

| Fact | Value |
|---|---|
| Manifest sha256 (obtained out of band; the script refuses any other file) | |
| Release-key fingerprint (public) | |
| Public key file used for verification (`.pub`) | |
| Script sha256 (`sign_manifest_sshsig.sh`) | |

Scope file `scope-sshsig.txt`:
```
scope_version=2
not_before=YYYY-MM-DDTHH:MM:SSZ
not_after=YYYY-MM-DDTHH:MM:SSZ
script_sha256=<script sha256>
common_sha256=<signing-common.sh sha256>
identity_json_sha256=<packaging/identity.json sha256>
manifest_sha256=<manifest sha256>
key_fingerprint=SHA256:<43 characters>
namespace=redevops-qa-release@v1
build_commit=<40 hex>
procedure_commit=<40 hex>
```
(The published release fingerprint is `SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA`; confirm it with `ssh-keygen -lf <public key>`.) Pass `--ledger <absolute path>`.
I load the key into MY agent myself; no script reads the private key; the key is never exported. The script refuses unless my agent lists the key's fingerprint, and never falls back to a key file.

Signed: ______________________  Date: ____-__-__

---------------------------------------------------------------------------------------------------------------------------------------

## 5. What this authorization does NOT cover

Publication, tags, releases, uploads other than the notarization named above, CI runs with signing secrets, any other file or hash, any other identity, any other day, any use by an agent of a
credential, a legal conclusion about any licence, and a decision about distribution.
