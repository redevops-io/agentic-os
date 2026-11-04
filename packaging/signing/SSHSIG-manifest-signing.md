# Signing the evidence manifest with SSHSIG (approval M-01)

Purpose: bind, by one signature, the top-level `release-manifest.json` of an R0-prime evidence set (and through its sha256 list every file the manifest names) to the existing release key,
without adding any file to the evidence directory. Implementation: `sign_manifest_sshsig.sh` (version `r0prime-sshsig-2`).
Nothing has been signed. The owner's key was not touched to prepare or test this: tests use a throwaway key in a temporary directory, never in an agent.

## 1. What is signed, with which key

| Item | Value |
|---|---|
| Subject | the bytes of `release-manifest.json` of the evidence set that describes the dmg being released (never a superseded manifest) |
| Digest | sha256 of that file, obtained OUT OF BAND (evidence INDEX/review, delivery message), passed as `--expected-sha256`; the script refuses any other file |
| Format | OpenSSH SSHSIG (`ssh-keygen -Y sign`), Ed25519 |
| Namespace | `redevops-qa-release@v1` (fixed in the script; domain separation from every other use of the key, for example `redevops-qa-anchor@v1`) |
| Key | the EXISTING release key; its public fingerprint as printed in `SIGNING_REQUEST.md` is `SHA256:wUoE8xKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA`, which has 42 characters after the colon where an OpenSSH SHA256 fingerprint has 43 (truncated or mistyped: the owner gives the true one as `--expected-fingerprint`); the owner supplies the public key file (`.pub`) |
| Signer | the owner's ssh-agent only, after separate explicit authorization (template section 4); the script never opens a private key file |

## 2. How the signature is made (the frozen approach)

`ssh-keygen -Y sign -n redevops-qa-release@v1 -f <public key file> < release-manifest.json > <sig file>`

* The manifest is streamed on stdin; the signature is read from stdout. `ssh-keygen` writes no file next to the manifest. The script refuses a `--sig-out` inside the manifest's directory,
  so the evidence set is byte-for-byte unchanged (tests compare names, sizes and mtimes before and after).
* **Agent only, no fallback (round 4, R4-01).** OpenSSH, given `-f <name>.pub` and an agent that lacks the key, loads the private key file `<name>` beside it. The script therefore (1) copies the PUBLIC key into its private 0700 temp directory and gives THAT copy to `ssh-keygen -f`, so no private key sits beside it; (2) requires the key's fingerprint in `ssh-add -l` of the owner's agent and refuses otherwise; (3) signs with `-U` (use the agent), a throwaway `HOME`, `SSH_ASKPASS=/usr/bin/false`, `SSH_ASKPASS_REQUIRE=never` and no `DISPLAY`, so no `~/.ssh` is read and nothing is prompted. It refuses a `--pubkey` file that contains a private key or is not one `ssh-ed25519` line. Regression tests use the reviewer's fake-agent scenario (an agent without the key and a private key file next to the `.pub`) with the real `ssh-keygen` and `ssh-add`.
* The script copies the manifest to a private temporary directory, checks the sha256 of the copy, signs and verifies THAT copy, re-checks the original at the end, and removes its temporary files by name.
* `SSH_AUTH_SOCK` reaches `ssh-keygen` only in `--real` mode (the script re-executes itself under `env -i`); in every other mode it is dropped.
* Verification, before the signature is released: `ssh-keygen -Y verify -f <allowed_signers> -I redevops-qa-release -n redevops-qa-release@v1 -s <sig> < manifest`, with an `allowed_signers` file built from the SUPPLIED public key only (one principal, `namespaces="redevops-qa-release@v1"`). A key found anywhere else is never consulted.
* The record file (`--record-out`, outside the evidence directory) holds the script version and sha256, the manifest sha256, the namespace, the key fingerprint, the signature file's sha256, the verification line and the `ssh-keygen` identification.

## 3. Modes

| Mode | What it does | Secrets |
|---|---|---|
| `--dry-run` (default) | checks the digest, the key file and prints the two commands; contacts no agent, creates nothing | none |
| `--verify-only --manifest M --sig S --pubkey K [--expected-fingerprint F]` | verifies an existing signature; exit 0 verified, 5 not verified | none (what a reviewer or third party runs) |
| `--self-test [--test-pubkey-out F]` | mechanics with a THROWAWAY key that the script generates itself in a fresh private temp directory (comment `signproc-TEST-KEY`), uses and deletes; no key can be handed to it; refuses any key whose fingerprint starts with the published release fingerprint, and any agent | none (the key is made and removed by the run) |
| `--real --i-have-owner-authorization SHA --scope-file F --expected-fingerprint F` | signs through the owner's agent | the owner's agent (the script sees none of it) |

`--real` additionally requires: `SSH_AUTH_SOCK` set to a socket (the only mode in which it is passed on), the file name `release-manifest.json`, `--expected-fingerprint`, `--ledger FILE` (one-time use of the scope; `--allow-reuse` is recorded), and a scope file (version 2: UTC window of at most 24 h, script and `signing-common.sh` sha256, `identity.json` sha256, manifest sha256, key fingerprint, namespace, build and procedure commit ids) read once into a private copy whose sha256 equals the flag. The scope file is a tripwire against mistakes, not an authentication: the owner's agent is the authentication.

## 4. Verification by anyone, with public material only

```
ssh-keygen -Y verify -f allowed_signers -I redevops-qa-release -n redevops-qa-release@v1 -s release-manifest.json.sig < release-manifest.json
```
where `allowed_signers` holds one line: `redevops-qa-release namespaces="redevops-qa-release@v1" ssh-ed25519 AAAA...` with the public key as published by the owner
(`https://redevops.io/.well-known/redevops-signing-key.txt`, as stated in `SIGNING_REQUEST.md`; compare the fingerprint with `ssh-keygen -lf`). Or run `sign_manifest_sshsig.sh --verify-only`.
A signature is only as trustworthy as the channel through which the public key was obtained.

## 5. Where the signature file lives

The evidence set says: no signature file is part of it and none may be added (`VERIFY.md` section 6). The signature is therefore kept outside the set (for example in the release/signing record next to the
per-platform evidence). If the owner later wants it beside the manifest, that is a new, separately reviewed evidence-set version (a different manifest, hence a different digest).

## 6. Limits (state them wherever the signature is mentioned)

* An SSHSIG signature carries NO time. The date of signing is only what the record and the owner say; an old signed manifest can be presented as a current one.
* It shows that a holder of the private key signed these bytes. It does not show who that is, that the key was not exposed, that the agent or host was uncompromised, or that the artifacts the manifest lists are good.
* No revocation and no rotation exist in this scheme; one public key per verification.
* It signs the manifest digest, so it binds exactly the files the manifest lists; a file outside the manifest (for example the verifier logs) is not bound.
* A signature of the manifest of the UNSIGNED evidence set says nothing about signed artifacts; those get their own evidence set and their own approval.

## 7. Tests (no secret, no agent)

`tests/test_signing_procedure_r0prime.py`: dry run default; self-test with a throwaway ed25519 key (generated into a temporary directory, deleted by name after the test); no file added to the evidence directory;
verification with the public key; failure cases (wrong digest, tampered manifest, wrong namespace, signature by another key, wrong expected fingerprint, signature inside the evidence directory, private key given as public, non-test key in
self-test, existing output); `--real` against a STUB `ssh-keygen` and a bare socket file, proving that the public key file (not a private key) is what is handed over, that the agent socket is passed only in `--real`, and every scope-file refusal.
The real agent path with the real release key is UNVERIFIED by design.
