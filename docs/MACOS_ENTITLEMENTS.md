# macOS entitlements of the Agentic Apps bundle

Purpose: state which entitlement the macOS build needs, where it applies, what it permits and the risk that
remains. Nothing in this repository signs or notarizes; the plist is an input for a later signing run that
happens outside the repository (see "Use").

## What is in the repository

`packaging/macos/sidecar.entitlements.plist` carries exactly one entitlement:

| Key | Value |
|---|---|
| `com.apple.security.cs.disable-library-validation` | `true` |

It is copied unchanged from the frozen v0.3.2 signing inputs (same single key; the frozen copy only differs in
whitespace and in a comment block). `allow-jit`, `allow-unsigned-executable-memory`, `allow-dyld-environment-variables`
and every other key are NOT set: the v0.3.2 matrix runs found them unnecessary for the frozen sidecar.

## Where it applies

Only the sidecar Mach-O, `Agentic Apps.app/Contents/MacOS/redevops-sidecar` (a PyInstaller onefile binary).
The launcher executable (`Contents/MacOS/agentic-apps`) and the app bundle carry no entitlements, and
`tauri.conf.json` has no `bundle.macOS.entitlements`, so the Tauri bundler never applies this plist to the launcher
or the bundle. `tests/test_macos_entitlements.py` checks the single key, the file's name and scope, that the launcher
configuration references no entitlements file, and that no other file in the repository names the key.

## Why the sidecar needs it

The onefile sidecar unpacks about 60 Mach-O libraries (libpython, DuckDB, cryptography, pydantic_core, OpenSSL, ...)
into `$TMPDIR/_MEI<random>` at start and `dlopen`s them. Under the hardened runtime, library validation accepts only
libraries signed by Apple or by the same Team ID as the process. Those libraries come from wheels and from the
interpreter build and are ad-hoc signed or unsigned, so without the entitlement `dlopen` of the embedded Python fails
("mapping process and mapped file (non-platform) have different Team IDs"; observed in the frozen v0.3.2 matrix runs).

## What it permits, and the residual risk

With library validation disabled the sidecar process can load any dylib that the dynamic loader is asked to load,
whoever signed it (or if nobody did). Residual risks that remain after signing and notarization:

1. **Library planting.** Code that can write into a directory the sidecar loads libraries from can get that code
   loaded into the sidecar. The directory is the per-launch extraction directory `_MEI*` under the user's temporary
   directory; it was observed with mode `drwx------` (owner only) in a local build, so the writer has to be the
   same user (or root). An attacker with the same user's file-write access could race or replace a file there. Same-user
   attackers are outside the threat model of `docs/LOCAL_API_SECURITY.md`; the entitlement is the reason that boundary
   matters more for the sidecar than for a fully library-validated process.
2. **Inherited reach.** Anything loaded into the sidecar runs with the sidecar's privileges: the user's files that the
   sidecar can read (it can walk any readable folder after an authenticated session, `docs/LOCAL_API_SECURITY.md` 7.4),
   its network access and its TCC grants.
3. **No other hardening is relaxed.** Notarization, the hardened runtime and Gatekeeper still apply; JIT and
   unsigned-executable-memory are not granted.

Ways to remove the need (not done in R0-prime; each changes the build and the signing run): sign every embedded Mach-O
with the same Developer ID so library validation can stay on (PyInstaller supports signing the collected binaries), or
ship an onedir layout where the libraries are inside the signed bundle instead of being extracted at run time. This is
an owner decision before any public release.

## Use (later, outside this repository)

The signing script lives outside the repository and is not run by any build here. It is expected to sign inside-out and to
pass this file to the sidecar only, for example `codesign --options runtime --entitlements packaging/macos/sidecar.entitlements.plist
<sidecar>`, then sign the launcher and the bundle without entitlements, and to read the resulting entitlements back and fail if
the sidecar carries anything else or any other binary carries any. Nothing has been signed from this file in R0-prime; the
local candidate is unsigned.

## Minimum system version

`tauri.conf.json` sets `bundle.macOS.minimumSystemVersion` to `11.0`: the launcher and the sidecar are arm64-only Mach-O
files with `minos 11.0`, and Tauri's default `LSMinimumSystemVersion` of `10.13` would promise macOS versions on which the
binaries cannot run (arm64 macOS starts at 11.0).
