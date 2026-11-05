# R0-prime identity: names, versions and identifiers that differ from the frozen v0.3.2 deliverable

Purpose: the R0-prime candidate is built from the frozen v0.3.2 tree plus a small set of fixes. It must never be mistaken for, installed over,
or share per-user data with the frozen deliverable (`Agentic.Apps_0.1.0_aarch64.dmg`, `Agentic Apps.app`, `io.redevops.agentic-apps`, version 0.1.0).
This document records which identifiers are distinct, which are intentionally unchanged, the compatibility risks that remain, and how to tell
an artifact of one line from the other. Owner decision 8 (2026-11-04).

Source of truth: `packaging/identity.json` (read by `packaging/tools/identity.py`; `python3 -I packaging/tools/identity.py all` prints every
value). `deploy/launcher/src-tauri/tauri.conf.json`, `Cargo.toml`, `Cargo.lock`, `Info.plist`, the build scripts and both acceptance kits are
checked against it by `tests/test_r0prime_identity.py`. The frozen identity is recorded in the same file and must never change.

## 1. The identity table

| Item | Frozen v0.3.2 | R0-prime | Where it is set |
|---|---|---|---|
| Product name (window title, menu, Finder name, dmg volume, Windows ARP name, Start Menu entry) | `Agentic Apps` | `Agentic Apps R0-prime` | `tauri.conf.json` `productName`, window `title` |
| Version (Cargo, Tauri, `CFBundleShortVersionString`, MSI/NSIS display) | `0.1.0` | `0.1.1` | `tauri.conf.json` `version`, `Cargo.toml`, `Cargo.lock` |
| `CFBundleVersion` | `0.1.0` (Tauri default = version) | `0.1.1` (explicit) | `tauri.conf.json` `bundle.macOS.bundleVersion` |
| MSI `ProductVersion` | `0.1.0` | `0.1.1` (a.b.c, no build field, no pre-release) | derived by tauri-bundler from `version` |
| NSIS `VIProductVersion` | `0.1.0.0` | `0.1.1.0` | derived |
| Bundle identifier | `io.redevops.agentic-apps` | `io.redevops.agentic-apps.r0prime` | `tauri.conf.json` `identifier` |
| Sidecar code-signing identifier (for the later signing run) | `io.redevops.agentic-apps.sidecar` | `io.redevops.agentic-apps.r0prime.sidecar` | `packaging/identity.json`, `SIGNING-READINESS.md` |
| macOS app bundle | `Agentic Apps.app` | `Agentic Apps R0-prime.app` | derived from `productName` |
| macOS disk image | `Agentic Apps_0.1.0_aarch64.dmg` (released as `Agentic.Apps_0.1.0_aarch64.dmg`) | `Agentic Apps R0-prime_0.1.1_aarch64.dmg` (a release host that replaces spaces would show `Agentic.Apps.R0-prime_0.1.1_aarch64.dmg`) | derived |
| dmg volume name | `Agentic Apps` | `Agentic Apps R0-prime` | derived |
| Windows MSI | `Agentic Apps_0.1.0_x64_en-US.msi` | `Agentic Apps R0-prime_0.1.1_x64_en-US.msi` | derived |
| Windows NSIS installer | `Agentic Apps_0.1.0_x64-setup.exe` | `Agentic Apps R0-prime_0.1.1_x64-setup.exe` | derived |
| Launcher executable (`CFBundleExecutable`, `Contents/MacOS/...`, installed `.exe`) | `agentic-apps` / `agentic-apps.exe` | `agentic-r0prime` / `agentic-r0prime.exe` (15 characters: fits the Linux process-name limit used by `pgrep -x`) | `tauri.conf.json` `mainBinaryName` |
| MSI `UpgradeCode` | `79fccc4d-fe4b-5bed-9113-69daba98ac8f` (derived by Tauri from the product name) | `f13fc625-f77d-5c30-b308-a3c3ec1045fc` (pinned; the value Tauri would derive from the new name, written down so a later rename cannot silently change it) | `tauri.conf.json` `bundle.windows.wix.upgradeCode` |
| MSI install directory name; NSIS install directory | `...\Agentic Apps` (Program Files for MSI, `%LOCALAPPDATA%` for NSIS) | `...\Agentic Apps R0-prime` | derived from `productName` |
| Windows uninstall registry key (ARP) | `...\Uninstall\Agentic Apps` (NSIS) / product code (MSI) | `...\Uninstall\Agentic Apps R0-prime` (NSIS) / a different product code (MSI) | derived |
| Install-location registry key | `HKCU\Software\redevops\Agentic Apps` | `HKCU\Software\redevops\Agentic Apps R0-prime` | derived (publisher `redevops` is unchanged) |
| Shortcuts (Start Menu, Desktop) | `Agentic Apps` | `Agentic Apps R0-prime` | derived |
| MSI component GUIDs for the path and shortcut components | v5 of the bundle identifier | v5 of the new identifier (different) | derived by tauri-bundler |
| Launcher cache directory (redirect pages `launch/open-*.html`) | `~/Library/Caches/io.redevops.agentic-apps/launch/` (frozen v0.3.2 wrote no redirect pages) | `~/Library/Caches/io.redevops.agentic-apps.r0prime/launch/`; Windows `%LOCALAPPDATA%\io.redevops.agentic-apps.r0prime\...` | `app_cache_dir()`, keyed by the identifier compiled into the launcher |
| WebKit data, HTTPStorages, Saved Application State, Preferences plist (macOS) | keyed by `io.redevops.agentic-apps` | keyed by `io.redevops.agentic-apps.r0prime` | by the OS from the bundle identifier |
| WebView2 user-data folder (Windows) | `%LOCALAPPDATA%\io.redevops.agentic-apps\EBWebView` | `%LOCALAPPDATA%\io.redevops.agentic-apps.r0prime\EBWebView` | by Tauri from the identifier |
| Keychain / Credential Manager entries | none are created | none are created | not applicable |
| `Info.plist` marker | absent | `RedevopsReleaseLine = r0-prime` | `deploy/launcher/src-tauri/Info.plist` (merged by tauri-cli) |
| Build manifest | no field | `release_line: "r0-prime"` and `identity{...}` | `packaging/tools/build_manifest.py` |
| Launcher window text | heading "ReDevOps" | heading and `<title>` "Agentic Apps R0-prime" (RC3; the "Re" mark is not used in the page, nor in the icon files, which are TEMPORARY neutral artwork while D-LIC-11 is open), second sub-line "Private candidate, release line r0-prime." | `deploy/launcher/ui/index.html` (static text, no behaviour); window title in `tauri.conf.json` |
| SBOM root component; notices title; source-offer build identity | `Agentic Apps (native build)`; `Agentic Apps (<platform>)` | `Agentic Apps R0-prime (native build)`; `Agentic Apps R0-prime (<platform>)`; a "product ... release line" line | `generate_sbom.py`, `build-licenses.sh`, `generate_source_offer.py` |

## 2. Why version 0.1.1 and not a pre-release form

* Apple wants `CFBundleShortVersionString` and `CFBundleVersion` to be one to three period-separated integers. Tauri copies `version` into
  `CFBundleShortVersionString` and, unless `bundle.macOS.bundleVersion` is set, into `CFBundleVersion`. A semver pre-release such as `0.1.1-r0prime.1` or build
  metadata such as `0.1.1+r0prime` would put a hyphen or a plus sign into both keys.
* The MSI `ProductVersion` must be numeric `major.minor.patch[.build]` (major and minor at most 255, build at most 65535). tauri-bundler (2.9.4 and 2.10.4 source)
  rejects a non-numeric pre-release for the msi target and turns a numeric one into the fourth field. A plain `0.1.1` satisfies Apple, MSI and NSIS
  (`VIProductVersion` `0.1.1.0`) with no special case.
* The distinction between the two lines therefore lives in the product name, bundle identifier, executable name, `UpgradeCode`, the `RedevopsReleaseLine` marker and
  the build manifest, not in a version suffix. `0.1.1` sorts after `0.1.0`; that has no effect because the two lines are different products to the OS (different bundle
  identifier and UpgradeCode), and neither line ships an updater (`createUpdaterArtifacts` is unset).
* `CFBundleVersion` is set explicitly to `0.1.1` so a later change of `version` does not silently change it. If a monotonic build counter is wanted later, it belongs in
  `bundleVersion` only (Apple allows three integers, for example `0.1.1` -> `1.0.0`); that is an owner decision.
* The Cargo crate version (`redevops-launcher`) follows the product version so `CARGO_PKG_VERSION`, `Cargo.lock` and the compiled-crate lists agree with it.

## 3. What is intentionally unchanged

| Item | Value | Reason |
|---|---|---|
| Sidecar executable name | `redevops-sidecar` (`.exe`) | internal; the launcher finds it next to itself; build, SBOM and signing inputs are keyed on it; the install directories are separate |
| Python distribution | `agentic-os 0.2.4` (`pyproject.toml`, `agentic_os.__version__`) | the Python package line is not the app version; changing it alters wheel names and the SBOM's first-party component and does nothing for identification (the sidecar's sha256, the commit and `release_line` in the build manifest identify it) |
| Per-user product data | `~/.agentic-os`, `~/.redevops/secrets`, `~/.agentic-os/trusted-sources` (and `AGENTIC_OS_HOME`, `REDEVOPS_SECRET_DIR`) | these are the user's data, keyed by the home directory, not by the application; both lines read and write the same files. Changing them would be a behaviour change |
| Publisher / manufacturer | `redevops` (second element of the identifier) | only the registry key's last component and the product name need to differ |
| Licence, copyright, category, minimum macOS (`11.0`), window size | unchanged | not identity |
| Icon files | RC3: TEMPORARY neutral artwork (`deploy/launcher/src-tauri/icons/`, generated by `scripts/make_neutral_icons.py`); the "Re" mark is not retained while D-LIC-11 is open | a stand-in, not a brand decision; same file names and sizes, so nothing else changes |
| Entitlements | `packaging/macos/sidecar.entitlements.plist`, unchanged | `docs/MACOS_ENTITLEMENTS.md` |
| Protocol names and environment variables | `agentic-os-local-capability/1`, `AGENTIC_OS_*`, header `X-Agentic-Session` | wire contract (`docs/LOCAL_API_SECURITY.md`) |
| Windows/Linux packaging recipe | same targets in `tauri.conf.json` | Linux `deb`/`AppImage` names follow the new product name automatically; no Linux R0-prime artifact is built or claimed |

## 4. Compatibility risks that remain

1. **Shared per-user data.** Running both lines as the same user shares `~/.agentic-os`, `~/.redevops` and the trust file. This is intended (the user's data survives a change of
   app), but a test that must start from nothing has to use a clean account or a throwaway HOME (the acceptance kits do).
2. **Both lines at once.** The frozen sidecar listens on fixed port 8787 without authentication; the R0-prime launcher uses an OS-chosen port and, after a start,
   shows `legacy_service_warning` if something unauthenticated still answers on 8787. Two launchers of different lines can run side by side, but it is not a tested configuration
   and the kits tell the tester to close the other line first.
3. **Signing and notarization are per identity.** The new bundle identifier and sidecar identifier need their own signing run; the frozen `sign_and_notarize_macos_v2.sh` hard-codes the
   frozen names and must be re-reviewed (`SIGNING-READINESS.md`). Nothing is signed here.
4. **Evidence tooling outside the repository.** The evidence generator and verifier used for the unsigned manifest (`evid-work/tools`: `common_evidence.py` `DMG_NAME`,
   `generate_r0prime_evidence.py`, `verify_evidence.py`, `run_negative_tests.sh`, `regenerate_evidence.sh`, `make_release_manifest.py` product block) hard-code `Agentic Apps.app`, `agentic-apps` and
   `Agentic Apps_0.1.0_aarch64.dmg`. They must read `packaging/identity.json` (or be edited) before the next evidence run; the existing final3 evidence describes the old names and must not be reused for a candidate built from this identity.
5. **Distinct from frozen does not mean installable side by side on every platform.** macOS (two bundle identifiers) is checked statically and by a scratch build of the bundle; the MSI/NSIS behaviour
   (separate UpgradeCode, install directory, registry keys, shortcuts) is derived from tauri-bundler source (STATIC) and has not been exercised: no Windows artifact was built or installed.
   tauri-bundler 2.9.x matches a running NSIS app by executable name; 2.10.x by install path. The distinct executable name removes the clash in both.
6. **Replacing an R0-prime install by the eventual release.** Because the identity differs, the final release will not upgrade an R0-prime install in place (new bundle identifier, new UpgradeCode),
   and WebKit/WebView2 data and cached redirect pages do not carry over (they hold no product data). Whether the release keeps the production identity (`Agentic Apps`, `io.redevops.agentic-apps`,
   a version above 0.1.0) or this one is an owner decision (listed in `packaging/DECISIONS-REQUIRED.md`).
7. **Names with a space and a hyphen.** `Agentic Apps R0-prime` contains both; scripts must quote it (the kits do). A host that rewrites spaces to dots changes the file name (not the contents or the bundle).

## 5. How to tell a frozen artifact from an R0-prime one

| Artifact | Frozen v0.3.2 | R0-prime |
|---|---|---|
| File name | `Agentic.Apps_0.1.0_aarch64.dmg`, `Agentic.Apps_0.1.0_x64_en-US.msi`, `Agentic.Apps_0.1.0_x64-setup.exe` | names containing `R0-prime` and `_0.1.1_` |
| App bundle | `Agentic Apps.app` | `Agentic Apps R0-prime.app` |
| `Info.plist` | `CFBundleIdentifier` `io.redevops.agentic-apps`, versions `0.1.0`, no `RedevopsReleaseLine` | `CFBundleIdentifier` `io.redevops.agentic-apps.r0prime`, versions `0.1.1`, `RedevopsReleaseLine` `r0-prime` (`plutil -p ".../Contents/Info.plist"`) |
| Launcher process / file | `agentic-apps` | `agentic-r0prime` |
| Window | title `Agentic Apps` | title `Agentic Apps R0-prime` and the line "Private candidate, release line r0-prime." |
| Windows | ARP name `Agentic Apps`, folder `Agentic Apps` | ARP name `Agentic Apps R0-prime`, folder `Agentic Apps R0-prime`, MSI UpgradeCode `f13fc625-...` |
| Build manifest | no `release_line` | `"release_line": "r0-prime"` and an `identity` object |
| Local API behaviour | v0.3.2 sidecar answers the API without a session and sends `Access-Control-Allow-Origin: *` | every route needs a session (`docs/LOCAL_API_SECURITY.md`) |
| Checker | `python3 -I packaging/tools/check_bundle_identity.py --app <bundle> --dmg <dmg>` exits 0 only for the R0-prime identity (`--frozen-ok` checks the frozen one) | same |

## 6. Evidence

* OBSERVED: a scratch build in an isolated directory (tauri-cli 2.11.4, dummy sidecar, placeholder licences; not a candidate) produced `Agentic Apps R0-prime.app` with `CFBundleIdentifier`,
  `CFBundleShortVersionString`, `CFBundleVersion`, `CFBundleName`, `CFBundleDisplayName`, `CFBundleExecutable` and `RedevopsReleaseLine` as in the table, the dmg
  `Agentic Apps R0-prime_0.1.1_aarch64.dmg` with volume name `Agentic Apps R0-prime`, and `check_bundle_identity.py` passing on both.
* STATIC: Windows rows and the `UpgradeCode`/GUID/version derivation (tauri-bundler 2.9.4 and 2.10.4 source), and `app_cache_dir()` being keyed by the compiled-in identifier.
* UNVERIFIED: any Windows build or install; LaunchServices behaviour with both apps installed; behaviour of the signed bundle under the new identifiers.
* The candidate directories were not rebuilt for this change; the existing candidates and evidence sets carry the old identity.
