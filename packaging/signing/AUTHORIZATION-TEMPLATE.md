# Authorization template (owner fills in; one block per approval)

This file is a TEMPLATE. Nothing here is an approval, and no filled-in copy belongs in the repository: keep your copy outside it, together with the scope files it names.
Never put a key, token, password, certificate password, Apple credential or ssh-agent socket into this form, a scope file, a chat message or an issue. The scripts want only
public facts (hashes, a Team ID, a certificate CN, a fingerprint, dates) and the NAMES of things you export in your own shell.

How a scope file works: you write the facts below as `KEY=VALUE` lines in a plain text file (`#` comments allowed, no other syntax, unknown or repeated keys are refused). You give the SHA-256 of
that file to the script as `--i-have-owner-authorization <sha256>` and the file as `--scope-file`. The script compares every fact with what it is about to use and refuses on any difference, so an
authorization cannot be re-used for another file, another script version, another identity or another day. The scope file is written by you; an agent that has the template can draft one for your
review but cannot approve it.

---------------------------------------------------------------------------------------------------------------------------------------

## 0. General statements (sign once per signing session)

* I, the owner, authorize the approvals ticked below, and only those. Refusing one does not refuse another; signing the manifest does not sign the dmg and signing the dmg does not sign the manifest.
* Gates G1 to G10 of `SIGNING-PROCEDURE-R0PRIME.md` section 3.1 hold (initial each): G1 ___ G2 ___ G3 ___ G4 ___ G5 ___ G6 ___ G7 ___ G8 ___ G9 ___ G10 ___
* This is a PRIVATE candidate signing: [ ] yes  [ ] no, it is a DISTRIBUTION candidate (then `check-distribution-gate.py --platform <p> --distribution` has exit status 0; output attached; decisions recorded in `docs/legal/OWNER_DECISION_FORM.md`).
* Signing does not publish, tag, release or upload anything except what a block below names. Where the signed files may be stored afterwards: ______________________
* Date window of this session (UTC): from ____-__-__ to ____-__-__ . Outside it nothing may be signed under this authorization.
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
| Script sha256 (`shasum -a 256 packaging/signing/sign_and_notarize_r0prime_macos_v3.sh`) | |
| Entitlement plist sha256 (`packaging/macos/sidecar.entitlements.plist`) | |
| Identity: sha256 of the EXACT identity string you will export (`printf '%s' "$R0P_CODESIGN_IDENTITY" \| shasum -a 256`) | |
| Team ID (10 characters; public) | |
| Notarization: yes/no; upload target | yes + apple-notary / no + none |
| Overall time-box for notarization (one working session suggested) | |
| Where the signed dmg may be kept | |
| Entitlement decision: the sidecar-only `com.apple.security.cs.disable-library-validation` is accepted for THIS named build, residual risk R1 read | [ ] accepted |

Scope file `scope-macos.txt`:
```
scope_version=1
valid_from=YYYY-MM-DD
valid_until=YYYY-MM-DD
script_sha256=<script sha256>
entitlements_sha256=<plist sha256>
input_dmg_sha256=<input dmg sha256>
identity_sha256=<sha256 of the identity string>
team_id=<Team ID>
notarize=yes
upload_to=apple-notary
```
(`notarize=no` and `upload_to=none` for a sign-only run; then do not pass `--notarize`.)

Command, run by you:
```
read -r R0P_CODESIGN_IDENTITY; read -r R0P_NOTARY_PROFILE; export R0P_CODESIGN_IDENTITY R0P_NOTARY_PROFILE
packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --real --notarize --i-have-owner-authorization "$(shasum -a 256 scope-macos.txt | awk '{print $1}')" \
    --scope-file scope-macos.txt --input-dmg <copy of the dmg> --expected-sha256 <input dmg sha256> --out-dir <new directory>
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
scope_version=1
valid_from=YYYY-MM-DD
valid_until=YYYY-MM-DD
script_sha256=<script sha256>
verifier_sha256=<verifier sha256>
stage=inner
inputs_sha256=<the input sha256 values, sorted ascending, comma separated, no spaces>
az_endpoint=<endpoint>
az_account=<account>
az_profile=<profile>
expected_cn=CN=<exact common name>
```
You are logged in with `az login` yourself before the run; the script asks `az` for one access token, keeps it in a 0600 file for the duration of the Jsign call and removes it.

Signed: ______________________  Date: ____-__-__

---------------------------------------------------------------------------------------------------------------------------------------

## 3. Windows stage `container`: MSI and NSIS setup exe

Same table and scope file as section 2 with `stage=container`, the two installer names exactly as derived from `packaging/identity.json`, and `inputs_sha256` the hashes of the REBUILT installers (rebuilt from the signed inner files).
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
scope_version=1
valid_from=YYYY-MM-DD
valid_until=YYYY-MM-DD
script_sha256=<script sha256>
manifest_sha256=<manifest sha256>
key_fingerprint=SHA256:<43 characters>
namespace=redevops-qa-release@v1
```
I load the key into MY agent myself; no script reads the private key; the key is never exported.

Signed: ______________________  Date: ____-__-__

---------------------------------------------------------------------------------------------------------------------------------------

## 5. What this authorization does NOT cover

Publication, tags, releases, uploads other than the notarization named above, CI runs with signing secrets, any other file or hash, any other identity, any other day, any use by an agent of a
credential, a legal conclusion about any licence, and a decision about distribution.
