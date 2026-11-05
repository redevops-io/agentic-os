# Distribution channels compared: signing, review, sandbox, conflicts, decisions (DRAFT)

Status: preparation only. Nothing here was submitted or signed; no store account was touched. Written 2026-11-05 against
`rc3/finalfix` e2f2fa9. Details: `docs/release/MAC_APP_STORE.md`, `docs/release/MICROSOFT_STORE.md`,
`docs/release/WINDOWS_BUILD_BLOCKERS.md`. The Ubuntu Snap Store part (`snap/snapcraft.yaml`, `docs/release/SNAP_STORE.md`) is owned
by the LINUX workstream; it is not present in this worktree and is NOT duplicated here. The Snap row below is a pointer plus
recalled facts that were not verified in this work. Developer ID and notarization are owned by the MACOS workstream runbook.
Licence wording: source-available (family licence: AGPL-3.0-or-later with the Commons Clause). 21 decisions in
`packaging/distribution-gate.json` are OPEN (18 block distribution); they apply to EVERY row below and are not repeated per row.

## 1. Facts about our app that drive every row

* Two executables: a Tauri launcher (control-panel window, 560x640) and a frozen Python sidecar (PyInstaller onefile).
  The sidecar listens on `127.0.0.1:<OS-chosen port>`; the user's BROWSER shows the UI (the webview never loads it).
* The launcher starts/stops the sidecar, runs an HMAC handshake over loopback, and opens the browser through `open`,
  `xdg-open` or `cmd /C start` with a path to a 0600 redirect page (SR-01).
* Optional Docker control (`docker compose`) and, in library code, installers that download/execute software (Ollama, PAIR).
* State: `~/.redevops/secrets`, `~/.agentic-os/...` (outside any app-private directory on Windows).
* There is no updater: `Cargo.toml` has no updater plugin and `tauri.conf.json` has no updater block, so every "auto-update"
  entry below is the channel's own mechanism, not ours.
* Today's artifacts are unsigned private candidates; signing is staged.

## 2. Matrix

| Channel | Signing identity | Who signs | Review | Auto-update | Sandbox / permission model | What in OUR app conflicts | Required architecture change | Owner decision |
|---|---|---|---|---|---|---|---|---|
| macOS DMG, Developer ID (MACOS workstream) | Developer ID Application (app) and optionally Developer ID Installer; notarytool credentials | Owner, with the Developer ID identity (not touched here) | Apple notarization scan (automated), no human review | None built in; Sparkle-style updater would be ours | Hardened runtime, no App Sandbox required; `disable-library-validation` used for the onefile sidecar (`packaging/macos/sidecar.entitlements.plist`) | None blocking | None for the current design | Owned by MACOS |
| Mac App Store | Apple Distribution or Mac App Distribution (app), Mac Installer Distribution (pkg), MAS provisioning profile | Owner, with an App Store certificate from the Developer account | Apple App Review (human), guidelines 2.4.5, 2.5.2, 5.1.1 (and 4.2, not read) | The store | App Sandbox mandatory; container `~/Library/Containers`; entitlements E1 to E5; nested sidecar `app-sandbox` + `inherit` | Docker control, `open` + file redirect, onefile sidecar extraction, PAIR/Ollama installers, real-home paths | Onedir sidecar as nested signed code; reduced flavour; LaunchServices opener; container data dir; new signing pipeline | D-MAS-1 to D-MAS-6 (MAC_APP_STORE.md) |
| Windows MSI/NSIS direct | Authenticode code-signing cert (OV/EV or Azure Artifact Signing) | Owner, with a certificate (none touched here) | None; SmartScreen reputation applies | None built in | Normal desktop app (admin/user install) | Antivirus false-positive risk of the onefile sidecar; `%USERPROFILE%` state left at uninstall | None required; optional onedir sidecar | Certificate choice (D-MS-4); not tested on Windows |
| Microsoft Store, EXE/MSI listing (S-A) | The same Authenticode cert; every PE signed (policy 10.2.9) | Owner (Store does NOT re-sign EXE/MSI) | Microsoft certification: malware scan, offline-installer and silent-install tests, content, privacy policy | Our responsibility (the listing is a URL to a versioned installer) | Normal desktop app | Offline WebView2 required (default is a downloader), silent flags, no remnants after uninstall, Docker/installer features vs 10.2.2/10.2.3/10.2.4 | Installer settings only; state to `%LOCALAPPDATA%` | D-MS-1 to D-MS-8 (MICROSOFT_STORE.md) |
| Microsoft Store, MSIX (S-B) | None needed from us for Store distribution (Microsoft signs); a test cert for sideload; Publisher = Store-issued CN | Microsoft (Store signing) | Microsoft certification plus the `runFullTrust` justification review | The Store | Full-trust (medium IL) Win32 in a package: not an AppContainer; read-only install tree; AppData/HKCU writes virtualised and removed at uninstall | Loopback behaviour unverified for packaged apps (W1); state outside AppData; onefile extraction; Docker control | New MSIX build step; onedir sidecar advisable; state migration | D-MS-2, D-MS-5, D-MS-7 |
| Ubuntu Snap Store (LINUX workstream) | Store-managed (no developer certificate); a Snap Store account and `snapcraft` login | Canonical's store signs the snap assertions (recalled, not verified here) | Automated review; `classic` confinement and some interfaces need manual review (recalled, not verified here) | snapd refreshes (recalled) | `strict` confinement with interfaces; `classic` needs approval (recalled) | See the LINUX workstream document | See `docs/release/SNAP_STORE.md` and `snap/snapcraft.yaml` | Owned by LINUX; cross-reference only. Reference: https://snapcraft.io/docs/snapcraft-yaml-reference (JavaScript-rendered, not readable in this work) |
| .deb / AppImage (direct) | Optional: a GPG signature or signed APT repository; AppImage embedded signature optional | Owner with a GPG key (not touched) | None | None built in (APT repository or AppImage update tooling would provide it) | Runs with the user's permissions | Hosts must have WebKitGTK; the launcher Docker feature works if Docker is installed | None | Whether to publish an APT repository / sign at all |
| OCI images / GHCR (service images for `apps/*`, not the desktop app) | Registry login; image signing (for example cosign) is optional and separate; the owner's recorded R0 decision is that no keyless attestation is used in R0 | Owner CI or registry token (not used here; pushing is not authorised) | None | None (pull by tag/digest; Watchtower-style tools are the user's choice) | Container isolation; rootless depends on the host | Package visibility, `image.source` label policy (never set it, per the staging decision), licence labelling | None | Promotion conditions of the private staging packages; D-LIC-* |

## 3. Cross-cutting owner decisions

| Id | Decision |
|---|---|
| X-1 | Which channels ship first. Recommendation from this analysis: direct signed channels (macOS Developer ID, Windows signed MSI/NSIS, .deb/AppImage) before any store; stores only after the licence decisions and with reduced flavours. |
| X-2 | One "store flavour" build switch (Docker control, installers that download/execute software, membrane/LibreOffice off; data dir in the app-private location) shared by MAS and Microsoft Store, rather than two divergent forks. |
| X-3 | Onedir sidecar: required for MAS, advisable for MSIX, optional elsewhere. One change in `redevops-sidecar.spec`, `build-sidecar.sh`, `stage-sidecar.sh`, SBOM and reproducibility checks, serving three channels. |
| X-4 | Updater: none exists. Direct channels need one (or accept manual downloads); stores provide theirs. Adding an updater adds a network capability and a signing key: a separate decision. |
| X-5 | Store-text rules: no "open source" wording (D-LIC-15), no claim of compliance or certification in listings, an accurate feature list for reviewers. |

## 4. What was and was not verified

| Item | State |
|---|---|
| Draft entitlements parse, attach to ad-hoc signed nested code, `productbuild` accepts the bundle | Verified locally (MAC_APP_STORE.md section 7) |
| Draft plists | `plutil -lint` OK |
| Draft `AppxManifest.DRAFT.xml` | Well-formed (`xmllint --noout`); schema validity and `makeappx` NOT run (tool unavailable on macOS) |
| Any sandboxed macOS run, any App Store Connect validation, any Windows run, any Store submission | NOT done |
| Apple documentation pages (`/documentation/...`) | NOT readable (JavaScript-rendered); Apple claims rest on the review guidelines, the certificates and upload help pages, Apple DTS forum threads and Tauri/PyInstaller documents |
| Microsoft Learn pages | Readable and quoted in MICROSOFT_STORE.md; loopback behaviour of a full-trust package is an inference from two pages and needs a Windows test |
| Snap and Linux package facts | Not verified; owned by the LINUX workstream |

Dates: all web pages in the three documents were fetched on 2026-11-05.
