# Mac App Store path: requirements, conflicts, architecture changes (DRAFT, preparation only)

Status: research and drafts. Nothing here was submitted, signed with a production identity, or built into a release.
Developer ID signing and notarization for the direct DMG are owned by the MACOS workstream runbook
(`packaging/signing/SIGNING-PROCEDURE-R0PRIME.md` and the MACOS report); this document covers only the Mac App Store (MAS).
Baseline: `rc3/finalfix` at e2f2fa9; every statement about the code is about that tree.
Sources were fetched on 2026-11-05; see section 9 for what could and could not be read.
Licence wording used throughout: source-available (family licence: AGPL-3.0-or-later with the Commons Clause).

## 1. Verdict

The current product cannot be submitted to the Mac App Store as it is. A reduced "MAS flavour" is technically plausible, but it
needs a nested-code re-packaging of the sidecar, removal of three features, a new way to open the browser, and a container-aware
data layout, and the review outcome stays uncertain because of two App Review guidelines (2.4.5 and 2.5.2, section 4).

* Feasibility: possible as a capability-reduced flavour; not possible as the full product.
* Effort estimate (judgement, not measured; one engineer who knows the code): about 4 to 8 weeks of engineering for the flavour
  and the signing pipeline, plus an unpredictable App Review calendar (assume at least one rejection cycle, 1 to 3 weeks).
  Breakdown in section 6.
* Nothing was proven on a sandboxed run: the experiment in section 7 only shows that the draft entitlements parse and attach.
* Recommendation: ship macOS through Developer ID + notarization (direct DMG) first; treat MAS as a separate product decision
  (owner decisions D-MAS-1 to D-MAS-4, section 8).

## 2. What the Mac App Store requires (from the sources)

| Requirement | Source and state |
|---|---|
| Apps must be sandboxed and follow the macOS File System guidance | Guideline 2.4.5(i), quoted: "They must be appropriately sandboxed, and follow macOS File System Documentation." (fetched, readable) |
| Packaged and submitted with Xcode-provided technologies, "self-contained, single app installation bundles", no third-party installers, "cannot install code or resources in shared locations" | Guideline 2.4.5(ii) (fetched) |
| No auto-launch without consent; no spawning of processes that keep running after the user quits | Guideline 2.4.5(iii) (fetched) |
| No download or install of "standalone apps, kexts, additional code, or resources to add functionality or significantly change the app from what we see during the review process" | Guideline 2.4.5(iv) (fetched) |
| "may not read or write data outside the designated container area, nor may they download, install, or execute code which introduces or changes features or functionality of the app" | Guideline 2.5.2 (fetched) |
| Privacy policy link in App Store Connect and in the app; consent for data collection | Guideline 5.1.1(i), (ii) (fetched) |
| `com.apple.security.app-sandbox` on every executable | Tauri App Store guide ("App Sandbox is required"); Apple DTS: "every nested executable must be signed with at least one entitlement because all App Store executables must be sandboxed" (forum thread 733942, fetched) |
| Signing identities | Apple certificates overview (fetched): "Mac App Distribution" or "Apple Distribution" signs the app for the Mac App Store; "Mac Installer Distribution" signs the installer package submitted to the Mac App Store. "Developer ID Application/Installer" are for outside the store only. The older names "3rd Party Mac Developer Application/Installer" were not on that page (not verified here). |
| Package | A signed `.pkg` built with `productbuild`; Tauri guide: `xcrun productbuild --sign "<identity>" --component "<App>.app" /Applications "<App>.pkg"` (fetched). The command shape was exercised unsigned in section 7. |
| Provisioning profile | A "Mac App Store Connect" profile embedded as `Contents/embedded.provisionprofile` (Tauri guide: `bundle.macOS.files`). Apple DTS (thread 733942): a Mac app needs a profile only if it uses restricted entitlements, but TestFlight requires one, and the entitlements file then needs `com.apple.application-identifier` and `com.apple.developer.team-identifier`; the entitlements file must go on the main app only; "Nested libraries (frameworks, dylibs): Never add entitlements to them"; nested executables need at least one entitlement; a nested executable with only `app-sandbox` and `inherit` needs no profile. |
| Upload | Transporter, Xcode, `xcrun altool --upload-app -t macos`, or the App Store Connect API (Apple "Upload builds" page, fetched). The Tauri guide uses `altool` with an API key. |
| Export compliance | `ITSAppUsesNonExemptEncryption` must be declared (Tauri guide). The value is an owner/counsel determination (the product uses TLS, HMAC-SHA256 and the `cryptography` wheel). |
| Hardened runtime and library validation | Hardened runtime is what notarization needs for Developer ID; for the MAS the sandbox is the mandatory control. The source for `disable-library-validation` itself (Apple documentation page) was JavaScript-rendered and could not be read; the statement that Apple restricts it for MAS apps was NOT verified. The draft avoids it, because (a) the brief forbids relying on it and (b) it is not needed once every Mach-O is signed with one identity. |

## 3. Where the application conflicts with the sandbox

Evidence is the source at e2f2fa9. "Expected" means derived from how the sandbox behaves, not observed (no sandboxed run was possible).

| # | Feature (file) | Conflict | Consequence |
|---|---|---|---|
| C1 | Docker control: `docker_available`, `advanced_docker_up/down/purge` run `docker` and `docker compose` in `bundle_dir()` (`deploy/launcher/src-tauri/src/main.rs`, lines 195-222); the capability file grants them (`capabilities/default.json`); `DockerDeployer` and posture probes call `docker`/`podman` (`agentic_os/mission/installer.py`, `device_posture.py`) | The sandbox does not give access to the Docker socket and its CLI home, and 2.4.5(ii)/2.5.2 forbid depending on and driving shared-location code. Expected: the commands fail. Also `bundle_dir()` defaults to `../sidekick-devops`, a path outside the bundle. | Remove the feature in the MAS flavour (compile it out, and remove the four `allow-*docker*` permissions from the flavour's capability file). |
| C2 | Opening the UI: `open_in_browser` runs `open <file>` on a 0600 redirect page in the app cache dir (`main.rs` `open_ui`, `opener.rs`) | (a) The redirect file lives in the app's container; the user's browser is a different process and, if sandboxed itself, cannot read another app's container. (b) Spawning `/usr/bin/open` from a sandboxed process is not verified to be allowed; expected to depend on the profile. | Replace by LaunchServices (`NSWorkspace openURL:`) with the `http://127.0.0.1:<port>/#lc=<code>` URL. This removes the argv exposure that SR-01 designed the file redirect for (LaunchServices does not put the URL on a process command line), but it is a security-reviewed design change: re-review SR-01. Adding a Tauri opener plugin or any native call changes `capabilities/default.json` and Cargo.lock: owner decision, not applied. |
| C3 | Local server: the sidecar binds `127.0.0.1:<OS port>` (`launch.rs` `PROJECTS_PORT=0`), the launcher connects with raw TCP for the HMAC handshake, the user's browser then connects | Needs `network.server` (listen) and `network.client` (connect). Note: the UI is shown in the user's browser, not in the Tauri webview (`main.rs` comment: "the sidecar origin is never loaded into this webview"; the webview CSP has no http source). So no webview network entitlement beyond the above is implied. | Entitlements E2, E3. App Review may ask why a sandboxed app listens on a port; the answer (loopback only, per-launch secret, `docs/LOCAL_API_SECURITY.md`) must be in the review notes. |
| C4 | Sidecar is a PyInstaller ONEFILE binary that unpacks six Mach-O files plus data into `$TMPDIR/_MEIxxxx` at every start and `dlopen`s them (`packaging/macos/sidecar.entitlements.plist` comment; `packaging/redevops-sidecar.spec` builds one `EXE`, no `COLLECT`) | Code is created at run time outside the signed bundle (against "self-contained" 2.4.5(ii)/2.5.2 in spirit), and the libraries are ad-hoc signed. The Developer ID build needs `disable-library-validation` for exactly this reason. | Re-package as onedir inside the bundle, every Mach-O signed with the one MAS identity (section 5). |
| C5 | Arbitrary paths: state in `~/.redevops/secrets` (`mission/onboarding.py`), `~/.agentic-os/trusted-sources` (`mission/trust.py`), `.agentic-os` relative to cwd (`control_plane.py`, `cli.py`) | In a sandbox `~` is the container (`~/Library/Containers/<id>/Data`), so these relocate and stay inside the container without code change. Anything that expects a path in the real home (for example reading a user project directory, a tool in the user's `$PATH`) fails. | Audit every filesystem and `$PATH` assumption; for user-chosen folders use `files.user-selected.*` with security-scoped bookmarks (owner decision, only if the feature is wanted). Migration: a user of the direct build has data in the real home; the MAS build cannot read it (2.5.2). |
| C6 | Downloading and executing code: `agentic_os/mission/pair_real.py` (`curl -fsSL https://ollama.com/install.sh \| sh`, `installer -pkg ... -target /`, `sudo apt install`, `ollama pull`), `integrations/libreoffice_app.py`, `mission/membrane.py` (spawns `sys.executable -c` with rlimits) | Directly in the scope of 2.4.5(iv) and 2.5.2. Whether `serve` reaches the PAIR/Ollama installer from the frozen sidecar was not traced; the code exists in the frozen archive (collect_submodules of `agentic_os`). | Disable these modules in the flavour (build-time switch, not a runtime flag), and describe the mission feature set honestly in review notes. The agent "mission" model that calls connectors is not by itself code download, but expect questions. |
| C7 | Auto-start and lingering processes | None found in the launcher: the sidecar dies with the launcher (`--exit-on-stdin-eof` tether, process group). 2.4.5(iii) is therefore expected to be satisfied. | Keep; mention in review notes. |
| C8 | Minimum functionality | The launcher window is a small control panel (560x640) that starts a service and opens a browser. Guideline 4.2 (minimum functionality) was not extracted in this work (fetched text covered 2.4.x, 2.5.2, 5.1.1 only). | Risk flagged, not assessed. |
| C9 | Single-instance and ports | `legacy_listener_warning` probes a fixed legacy port (`launch.rs LEGACY_PORT`): a loopback connect, allowed with `network.client`. | None. |

## 4. App Review risk that no engineering removes

* 2.5.2 says apps "may not ... download, install, or execute code which introduces or changes features or functionality". A governed
  agent platform whose purpose is running missions against connectors and local models is close to that line. Reviewers decide.
* 2.4.5(ii) (self-contained, no code in shared locations) conflicts with any feature that installs Ollama, Docker, or a model runtime.
* The product's UI lives in the browser; a reviewer testing only the Tauri window sees a control panel (C8).

## 5. Entitlements a MAS build would need (EXACT list; all are OWNER DECISIONS, none applied)

Main executable (`packaging/stores/macos-mas/app.entitlements.DRAFT.plist`):

| Id | Key | Value | Why | Owner decision |
|---|---|---|---|---|
| E1 | `com.apple.security.app-sandbox` | true | mandatory for the Mac App Store | D-MAS-1 (whether to pursue MAS at all) |
| E2 | `com.apple.security.network.client` | true | launcher handshake to 127.0.0.1; outbound calls of the inheriting sidecar (model providers, connectors) | D-MAS-2 |
| E3 | `com.apple.security.network.server` | true | the sidecar listens on 127.0.0.1 | D-MAS-2 |
| E4 | `com.apple.application-identifier` | `TEAMID.<bundle id>` | needed with a provisioning profile; TEAMID is a placeholder | D-MAS-3 (account, App ID, bundle id) |
| E5 | `com.apple.developer.team-identifier` | `TEAMID` | same | D-MAS-3 |

Nested executable (`helper.entitlements.DRAFT.plist`, the sidecar): `com.apple.security.app-sandbox` = true and
`com.apple.security.inherit` = true, and nothing else (Apple DTS, thread 733942 and 798947). Nested libraries: no entitlements at all.

Not proposed, and why: `com.apple.security.cs.disable-library-validation` (not needed after the onedir re-packaging),
`cs.allow-jit` and `cs.allow-unsigned-executable-memory` (the Developer ID matrix in `packaging/macos/sidecar.entitlements.plist`
found them unnecessary), `files.user-selected.*` (only if a folder chooser is built), any `temporary-exception.*` (review
scrutiny), `files.downloads.*`, `application-groups`, keychain groups (unused).

The existing Developer ID file `packaging/macos/sidecar.entitlements.plist` (`disable-library-validation`) is the opposite
approach and must not be reused for a MAS build; it is untouched by this work.

Info.plist keys: `packaging/stores/macos-mas/Info.plist.additions.DRAFT.plist` (category, minimum system version, copyright;
`ITSAppUsesNonExemptEncryption` is left out because its value is a legal determination: D-MAS-4).

## 6. Required architecture changes and effort (judgement estimates)

| Step | Change | Estimate |
|---|---|---|
| A1 | Sidecar to PyInstaller onedir (`COLLECT`, `contents_directory` set), staged into the bundle as nested code. The Tauri `externalBin` mechanism places one file beside the main binary (Tauri sidecar docs); a directory needs `bundle.macOS.files` or a post-build copy into `Contents/MacOS` or `Contents/Frameworks` (the Apple location rule for nested executables was not verified; Apple DTS shows a helper in `Contents/MacOS`). `build-sidecar.sh`, `stage-sidecar.sh`, the SBOM and the reproducibility checks all assume one file. | 1 to 2 weeks |
| A2 | Sign inside-out: every Mach-O (libpython, `_duckdb`, `cryptography`, `pydantic_core`, `_cffi_backend`, `yaml`, ...) with the MAS identity and NO entitlements; the sidecar with helper entitlements; the app with app entitlements; embed the profile. PyInstaller's ad-hoc/`--deep` signing must be replaced by an explicit script. | 1 week |
| A3 | Flavour switch: compile out Docker commands and capability permissions, PAIR/Ollama installers, membrane and LibreOffice integration, plus an honest feature list for the store listing | 1 to 2 weeks |
| A4 | Native opener via LaunchServices, SR-01 re-review, tests | about 1 week |
| A5 | Container-aware data layout and a path/`$PATH` audit; define what happens to existing direct-build data | about 1 week |
| A6 | MAS pipeline: `productbuild` with the Mac Installer Distribution identity, profile, Transporter/altool upload, sandbox smoke test (`codesign -d --entitlements`, run under sandbox, Console log for `deny` lines) | about 1 week |
| A7 | App Review iterations, privacy policy, export compliance, screenshots, age rating | calendar 1 to 3 weeks, owner-driven |

Total: roughly 4 to 8 engineering weeks plus review time. The estimate does not include the licence decisions in
`packaging/distribution-gate.json` (section 8).

PyInstaller facts used (PyInstaller documentation, fetched): on macOS it ad-hoc re-signs collected binaries by default;
`codesign_identity` and `entitlements_file` are EXE options; with a real identity it enables the hardened runtime; onedir runs as a
single process while onefile has a parent launcher plus a child.

## 7. Check performed: do the draft entitlements parse and attach?

Throwaway experiment, ad-hoc identity only (`-s -`), in `$S/rel/stores-exp` (scratch, not in the repository):

* a fake bundle `T.app` made of two copies of `/usr/bin/true` (never executed), a minimal `Info.plist`;
* `codesign -s - -f --entitlements helper.entitlements.DRAFT.plist T.app/Contents/MacOS/redevops-sidecar`, then
  `codesign -s - -f --entitlements app.entitlements.DRAFT.plist T.app` (host: macOS 27.0.1 arm64);
* `codesign --verify --strict --verbose=2 T.app`: "valid on disk", "satisfies its Designated Requirement";
* `codesign -d --entitlements - --xml` on both showed exactly the five app keys and the two helper keys (read back through `plutil -p`);
* `productbuild --component T.app /Applications T-unsigned.pkg` succeeded unsigned (`pkgutil --check-signature`: no signature); the pkg was deleted.
* `plutil -lint` reports OK for all three draft plists.

What this does NOT show: that the sandbox accepts the entitlements at run time (an ad-hoc signed sandboxed bundle was not
launched), that App Store Connect validation passes (needs a real profile and identity: not touched), or that the PyInstaller
onedir layout signs and loads. The production identity, the `redevops` notarytool profile and the keychain were not used.

## 8. Owner decisions (none applied)

| Id | Decision | Default recommendation |
|---|---|---|
| D-MAS-1 | Pursue the Mac App Store at all, with a reduced flavour and no Docker/Ollama installer features? | No for the first release; Developer ID DMG first |
| D-MAS-2 | Accept the loopback entitlements E2/E3 and the review-notes explanation | Only together with D-MAS-1 |
| D-MAS-3 | Account, App ID/bundle id (reuse `io.redevops.agentic-apps.r0prime` or register a new one), MAS certificates and profile; who holds them. Never use the Developer ID identity for this. | Owner action in the Apple Developer account |
| D-MAS-4 | Export-compliance answer (`ITSAppUsesNonExemptEncryption`) | Owner and counsel |
| D-MAS-5 | Any widening of `capabilities/default.json` or Cargo.lock for a native opener (C2) | Only after SR-01 re-review |
| D-MAS-6 | Licence questions before ANY store distribution: `packaging/distribution-gate.json` lists 21 OPEN decisions (18 of them block distribution) (including D-LIC-1 wording and copyright holder, D-LIC-3 Commons Clause with other-licence code, D-LIC-5 AGPL section 13 for a loopback UI, D-LIC-10 acceptance pages, D-LIC-18 objc2 derived from Apple SDKs). The App Store EULA and the family licence interaction is a legal question this document does not answer. | Counsel |

## 9. Sources and what could not be verified

Fetched 2026-11-05 unless stated.

* App Review Guidelines (readable): https://developer.apple.com/app-store/review/guidelines/ (2.4.5, 2.5.2, 5.1.1 quoted; the page was truncated after 5.2.5, so 4.2 was not read).
* Certificates overview (readable): https://developer.apple.com/help/account/create-certificates/certificates-overview
* Upload builds (readable): https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds
* Apple DTS, provisioning profiles and nested code (readable): https://developer.apple.com/forums/thread/733942
* Apple DTS thread, embedding a tool in a sandboxed app (readable): https://developer.apple.com/forums/thread/798947 (it links "Embedding a helper tool in a sandboxed app").
* Tauri macOS App Store guide (readable): https://v2.tauri.app/distribute/app-store/ ; Tauri sidecar guide: https://v2.tauri.app/develop/sidecar/
* PyInstaller feature notes (macOS): https://pyinstaller.org/en/stable/feature-notes.html
* NOT readable (JavaScript-rendered, the fetch returned only the title; any statement attributed to them in tools' replies was NOT used as evidence):
  https://developer.apple.com/documentation/security/app_sandbox ,
  https://developer.apple.com/documentation/xcode/configuring-the-macos-app-sandbox ,
  https://developer.apple.com/documentation/xcode/embedding-a-helper-tool-in-a-sandboxed-app ,
  https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.disable-library-validation ,
  https://developer.apple.com/library/archive/documentation/Security/Conceptual/AppSandboxDesignGuide/AppSandboxInDepth/AppSandboxInDepth.html .
  Consequently the following are UNVERIFIED against Apple documentation: the exact semantics of `com.apple.security.inherit` for a child started with posix_spawn, `network.server` being sufficient for a loopback listener, whether `/usr/bin/open` may be executed from the sandbox, the permitted bundle locations for nested helpers, and Apple's position on `disable-library-validation` in MAS apps. They need a sandboxed trial run with a real MAS-profile build before any commitment.
