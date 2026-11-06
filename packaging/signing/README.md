# packaging/signing: prepared (not executed) signing procedure for the R0-prime candidate

Purpose: scripts and documents for the independent review (owner decision 10) of how the R0-prime candidate would be signed. Nothing in this directory signs anything by itself: every real mode needs the
owner's authorization (`AUTHORIZATION-TEMPLATE.md`), the owner's own identity/profile/agent in the owner's own shell, and a scope file whose hash the owner passes on the command line. No secret is stored here.

| File | What it is |
|---|---|
| `SIGNING-PROCEDURE-R0PRIME.md` | the procedure: order, gates (private candidate versus distribution), authorization scope, macOS and Windows steps, evidence, abort rules, secrets handling, risks, UNVERIFIED |
| `AUTHORIZATION-TEMPLATE.md` | the form the owner fills in; scope-file formats |
| `signing-common.sh` | shared safety functions sourced by the three scripts (environment allowlist, fixed PATH, scope read once, window, ledger, distribution gate); its sha256 is bound by every scope file |
| `sign_and_notarize_r0prime_macos_v3.sh` | macOS: sidecar, launcher, bundle, dmg; notarize and staple the dmg. Modes `--dry-run` (default, plan only), `--adhoc-test` (ad-hoc `-` on a copy, no secret), `--real` |
| `sign_windows_r0prime_v3.sh` | Windows (macOS host): Jsign 7.6 + Azure Artifact Signing; stages `inner` and `container`. Modes `--dry-run` (default), `--throwaway-test`, `--real` |
| `r0prime_win_signcommand.sh` | the `signCommand` for the Windows build host: collects files to sign / puts signed files back by hash; no credential |
| `verify_signature_v2.py` | the frozen Authenticode verifier, byte-identical to the reviewed one |
| `sign_manifest_sshsig.sh`, `SSHSIG-manifest-signing.md` | evidence-manifest SSHSIG (namespace `redevops-qa-release@v1`); owner's agent only; verify-only mode for anyone |
| `tools/make_adhoc_fixture.sh` | builds an unsigned test dmg from an `.app` (renamed to the R0-prime identity when needed) for `--adhoc-test`; reads the source only |
| `DIFF-vs-v2.md` | every difference against the frozen v2 scripts |

Related: `packaging/macos/sidecar.entitlements.plist`, `docs/MACOS_ENTITLEMENTS.md`, `packaging/acceptance/macos-r0prime/SIGNING-READINESS.md`, `packaging/identity.json`, `docs/R0_PRIME_IDENTITY.md`, `packaging/check-distribution-gate.py`.

Tests (stubs; no secret, no network, no real identity, no keychain, no Azure, no agent):
```
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=<throwaway dir> <python with pytest> -m pytest -p no:cacheprovider tests/test_signing_procedure_r0prime.py
```
The macOS file needs macOS (plutil, hdiutil, ditto, ssh-keygen); `shellcheck` is used if present (not installed by the tests). One test runs the whole sequence with the real macOS tools and the ad-hoc identity on a tiny arm64 app (needs clang).

Rehearsal without any secret: `sign_and_notarize_r0prime_macos_v3.sh --adhoc-test ...` after `tools/make_adhoc_fixture.sh <app> <new dir>`; `sign_windows_r0prime_v3.sh --throwaway-test` with the frozen tool set and a throwaway keystore.
