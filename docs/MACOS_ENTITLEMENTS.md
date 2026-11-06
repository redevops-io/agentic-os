# macOS entitlements of the Agentic Apps R0-prime bundle

Purpose: state which entitlement the macOS build needs, where it applies, what it permits and the risk that
remains. Nothing in this repository signs or notarizes; the plist is an input for a later signing run that
happens outside the repository (see "Use").

## Status: provisional decision (owner decision 4, 2026-11-04)

* **Retained provisionally.** The R0-prime candidate keeps the sidecar entitlement `com.apple.security.cs.disable-library-validation`
  until the exit criteria below are met or the owner accepts the residual risk for a named release. It is a decision for the
  private engineering candidate, not an approval for any public release.
* **Sidecar only.** The entitlement is applied to `Contents/MacOS/redevops-sidecar` and to nothing else. The launcher
  (`Contents/MacOS/agentic-r0prime`) and the bundle carry no entitlements.
* **The hardened runtime is required for every Mach-O file that is signed** (the sidecar, the launcher, and the bundle seal; `codesign --options runtime`).
  Disabling library validation is the only relaxation; `allow-jit`, `allow-unsigned-executable-memory`,
  `allow-dyld-environment-variables` and every other key stay off. `packaging/acceptance/macos-r0prime/verify_dmg.sh --expect signed` reads the
  entitlements back and fails if any binary carries a different set (rows `E-*`).
* **Residual risk** is the one described below ("What it permits, and the residual risk"): library planting into the per-launch
  `_MEI*` directory by a same-user writer, and inherited reach of anything loaded into the sidecar. Same-user malware is outside the
  threat model of `docs/LOCAL_API_SECURITY.md`; this entitlement is the reason that boundary matters more for the sidecar.
* **Exit criteria** (any one removes the provisional status):
  1. an onedir sidecar layout, or a build that signs every embedded Mach-O with the release Developer ID, runs under the hardened
     runtime with library validation ON (the entitlement removed), shown on a signed build by the checks of `MANUAL-CHECKLIST.md`
     parts E to G and by `verify_dmg.sh --expect signed` with an empty sidecar entitlement set; or
  2. the owner records, for a named release, that the residual risk is accepted with the entitlement in place.
* **Evidence (OBSERVED by agent `gui`, `reports/gui.md` section 8, harness `packaging/acceptance/macos-r0prime/gui/scenarios/hr_matrix.sh`,
  transcript `gui-data/hardened-runtime.txt`; all on COPIES of the candidate's sidecar/app with ad-hoc `codesign -s -`: no identity, no keychain,
  no Developer ID, no notarization):**

  | Variant | Result |
  |---|---|
  | as shipped (ad-hoc, no hardened runtime, no entitlement) | runs |
  | hardened runtime (`--options runtime`), **no** entitlement | **does not start**: `code signature ... not valid for use in process: mapping process and mapped file (non-platform) have different Team IDs` (the extracted `libpython3.12.dylib` is refused) |
  | hardened runtime + **only** `disable-library-validation` (this plist) | **runs** (hand-off in about 0.8 s) |
  | the whole `.app` copy signed inner-first (sidecar with the entitlement, launcher and bundle `--options runtime`, no `--deep`) | `codesign --verify --deep --strict` valid; the signed sidecar started through the launcher's start code, handshake, sign-in, API 200, Open, Stop all worked; the hardened launcher's window appeared and its page scripts ran (`window.__TAURI__` works with no entitlement on the launcher); ended cleanly on SIGTERM |

  A toy host (`gui/tools/hr_loader.c`) confirms the rule independently: a hardened host without the entitlement refuses even an ad-hoc signed
  library; with the entitlement it accepts it; a library with **no** signature at all is refused in every case (arm64), so the entitlement does
  not mean "load anything". Developer-ID/Team-ID signing, notarization and Gatekeeper were **not** tested (UNVERIFIED; with a real Team ID the
  sidecar behaves like the second row without the entitlement, INFERRED from the same mechanism).
* **Residual risk, measured (OBSERVED PoC, `gui/scenarios/hr_plant.py`):** a same-user process that can write into the per-run `$TMPDIR/_MEI*`
  directory replaced an extracted extension module (`yaml/_yaml.cpython-312-darwin.so`) with its own ad-hoc signed dylib 100 to 190 ms after the
  sidecar started; its constructor ran inside the sidecar's Python process. **This works identically on the current unhardened candidate and on
  the hardened variant with the entitlement.** `$TMPDIR` (`/var/folders/.../T`) and `_MEI*` are mode 0700, so it takes code running as the same user
  (or root): a window of a few hundred milliseconds per Start. The hardened runtime without the entitlement would refuse the planted library, but
  that configuration cannot run the onefile sidecar at all. What the hardened variant adds over the current candidate: `DYLD_INSERT_LIBRARIES`
  injection is ignored (OBSERVED: it ran in the unhardened variant and was ignored in the hardened one) and, by Apple's documented rules,
  debugger/task-port attach is denied (INFERRED, not tested). The app is not sandboxed.
* **The onedir exit criterion (criterion 1 above) is what removes the planting risk:** no extraction (extraction costs about 200 to 260 ms of a
  roughly 550 ms Start, OBSERVED), no `$TMPDIR` trust, no leaked `_MEI` directories, no entitlement. Its cost: Tauri's `externalBin` expects a
  single file (a wrapper or a resource launch is needed), about 60 more files to sign inside-out, and a changed frozen layout (a new evidence set).
  Keeping onefile with an app-private extraction root gains nothing against the same user.
* **Required by the signing procedure (OBSERVED on the ad-hoc test):** the bundle is signed inner-first (sidecar with the entitlement, then the launcher,
  then the bundle, each `--options runtime`, no `--deep`); an unsigned `.app` fails `codesign --verify --strict` until then.

## What is in the repository

`packaging/macos/sidecar.entitlements.plist` carries exactly one entitlement:

| Key | Value |
|---|---|
| `com.apple.security.cs.disable-library-validation` | `true` |

The key content is copied unchanged from the frozen v0.3.2 signing inputs (same single key); only the comment text differs (it now states
the six libraries this candidate extracts). `allow-jit`, `allow-unsigned-executable-memory`, `allow-dyld-environment-variables`
and every other key are NOT set: the v0.3.2 matrix runs found them unnecessary for the frozen sidecar.

## Where it applies

Only the sidecar Mach-O, `Agentic Apps R0-prime.app/Contents/MacOS/redevops-sidecar` (a PyInstaller onefile binary).
The launcher executable (`Contents/MacOS/agentic-r0prime`) and the app bundle carry no entitlements, and
`tauri.conf.json` has no `bundle.macOS.entitlements`, so the Tauri bundler never applies this plist to the launcher
or the bundle. `tests/test_macos_entitlements.py` checks the single key, the file's name and scope, that the launcher
configuration references no entitlements file, and that no other file in the repository names the key.

## Why the sidecar needs it

The onefile sidecar of this candidate extracts **six** ad-hoc-signed Mach-O files (80 MB: `libpython3.12.dylib`, `_duckdb`, the
`cryptography` Rust extension, `pydantic_core`, `_cffi_backend`, `yaml/_yaml`; the frozen v0.3.2 CPython build extracted 61) into
`$TMPDIR/_MEI<random>` at every start and `dlopen`s them. Under the hardened runtime, library validation accepts only
libraries signed by Apple or by the same Team ID as the process. Those libraries come from wheels and from the
interpreter build and are ad-hoc signed or unsigned, so without the entitlement `dlopen` of the embedded Python fails
("mapping process and mapped file (non-platform) have different Team IDs"; observed in the frozen v0.3.2 matrix runs and again on this candidate by agent `gui`).

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
