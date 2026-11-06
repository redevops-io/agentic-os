# Microsoft Store path: packaging options, policy, conflicts (DRAFT, preparation only)

Status: research and drafts. Nothing was built, signed or submitted. No Windows tool ran: the host is macOS 27 arm64, so
`makeappx.exe`, `signtool.exe`, the Windows App Certification Kit, WiX, NSIS and MSIX tooling were NOT run, and no Windows result
is claimed anywhere in this document. Sources were fetched 2026-11-05 (section 9).
Baseline: `rc3/finalfix` e2f2fa9. Licence wording: source-available (family licence: AGPL-3.0-or-later with the Commons Clause).
Static review of the Windows build and the Win11 VM scripts, and what is blocked on this Mac, is in `docs/release/WINDOWS_BUILD_BLOCKERS.md`.

## 1. Verdict

Two routes exist; the cheaper one needs no new package format.

| Route | What it is | Architecture change | Signing | Estimate (judgement) |
|---|---|---|---|---|
| S-A "EXE or MSI app" in the Store | Partner Center lists a versioned HTTPS URL to your existing `.msi` or NSIS `-setup.exe`. Tauri documents exactly this route (it produces EXE/MSI only). | None to the app. Installer changes: offline WebView2 mode, silent install, publisher name != product name. | YOU sign the installer and every PE inside it with a cert that chains to the Microsoft Trusted Root Program (policy 10.2.9). The Store does not re-sign. | 1 to 2 weeks plus certificate procurement |
| S-B MSIX | A new `.msix` (full-trust Win32 app, `runFullTrust`), uploaded to Partner Center | A new packaging step (none exists in the repo); state and uninstall semantics change; sidecar sits in a read-only package | Store signs on publication ("You don't need to provide your own code signing certificate for Store distribution"); a local self-signed or Artifact-signed copy is only for sideload tests | 2 to 4 weeks |

Recommendation: S-A first (no code change; it is the shipped installer plus signing and a few installer settings); S-B only if the
owner wants the Store-managed install/update/uninstall experience and accepts the `runFullTrust` review. Both are blocked by the
licence decisions in section 8 and by an unsigned build today. Neither can be qualified on this Mac.

## 2. Microsoft Store requirements that apply (from the sources)

| Topic | Finding | Source |
|---|---|---|
| Account | Individual or Company account in Partner Center; "no registration fees" through https://storedeveloper.microsoft.com; ID verification (individual) or D-U-N-S/business documents plus a work email on the company domain (company); manual review 2 to 5 business days | open-a-developer-account (fetched) |
| Certification | Security tests (malware scan), technical compliance (Windows App Certification Kit), content compliance; up to three business days | MSIX app certification process (fetched) |
| Store signing | "The Microsoft Store automatically signs all MSIX/AppX packages with a Microsoft certificate ... You don't need to provide your own code signing certificate for Store distribution" | MSIX app certification process; Sign an MSIX package (fetched) |
| Package identity | `Identity/@Publisher` "must match the publisher subject information of the certificate used to sign a package". In a Store submission the Name and Publisher come from Partner Center (Product identity), not from our choice. | Identity element reference (fetched); Partner Center behaviour from the same pages |
| `runFullTrust` | Restricted capability: "a Medium IL app needs to declare the runFullTrust restricted capability". Partner Center detects it on upload and requires "details about how your product uses each capability" on the Submission options page; may delay certification; approval is generally not repeated for updates. | app-capability-declarations (fetched) |
| Dynamic code | Policy 10.2.2: the product "must not attempt to fundamentally change or extend its described functionality ... through any form of dynamic inclusion of code. ... should not, for example, download a remote script and subsequently execute that script in a manner that is not consistent with the described functionality". 10.2.3: no malware and no offering of "secondary software that is not developed by you and does not enhance the functionality of your product". 10.2.4: dependency on non-integrated software must be disclosed at the start of the description. | Store policies v7.20 (fetched) |
| Uninstall | 10.2.7: the product must "clearly communicate and enable a user's ability to cleanly uninstall and remove your product". MSI/EXE certification tests "Can uninstall cleanly without leaving remnants of files, folder, and registry entries". | Store policies; MSI/EXE certification (fetched) |
| Personal information | 10.5.1: a privacy policy URL is required if the product accesses, collects or transmits Personal Information; "Product types that inherently have access to Personal Information must always have privacy policies. These include ... Desktop Bridge and Win32 products." | Store policies (fetched) |
| EXE/MSI route rules | 10.2.9: only `.msi` or `.exe`; binary and all PE files digitally signed (CA in the Microsoft Trusted Root Program); versioned URL that never changes; silent install (a UAC dialog is allowed); "a standalone installer and is not a downloader stub/web installer"; PC devices only. The certification page: security tests include malware scan, standalone/offline installer, bundleware, drivers/NT services dependency disclosure. | Store policies; MSI/EXE certification (fetched) |
| Tauri specifics for S-A | Store build must use the Offline Installer WebView2 option; silent switches `/S` (NSIS) and `/quiet` (MSI); `bundle.publisher` must differ from `productName`; auto-update is the app's job | https://v2.tauri.app/distribute/microsoft-store/ (fetched) |
| WebView2 modes | Tauri default is `downloadBootstrapper` (needs internet at install); `embedBootstrapper` about +1.8 MB; `offlineInstaller` about +127 MB; `fixedRuntime` about +180 MB; `skip` not recommended | https://v2.tauri.app/distribute/windows-installer/ (fetched). `tauri.conf.json` sets none, so the default `downloadBootstrapper` applies today: it would fail the "not a downloader" rule for S-A. |
| ARM64 | Tauri supports `aarch64-pc-windows-msvc`; the NSIS installer then still runs x86 under emulation while the app is native ARM64 | windows-installer page (fetched) |
| Age rating | Content-rating questionnaire in the submission; answers must be truthful | MSI/EXE certification FAQ (fetched) |
| MakeAppx | `MakeAppx pack /d <dir> /p <out.msix>`; it validates that referenced files exist and some protocols but "Packages built by MakeAppx.exe are not guaranteed to be installable"; it does NOT create the `.msixupload`/`.appxupload` file recommended for Store upload | create-app-package-with-makeappx-tool (fetched) |

## 3. MSIX packaging options (S-B)

| Option | Notes | Verdict |
|---|---|---|
| a. New build step with `makeappx pack` over a staged layout (launcher exe, sidecar exe, resources, `AppxManifest.xml`, `Assets\`) | Full control; the layout is trivial because the launcher already finds the sidecar next to `current_exe()` (`main.rs sidecar()`). Needs the Windows SDK on the Windows build host, then Store upload packaging (`.msixupload`, can be made by hand per Microsoft's packaging page that MakeAppx points to; not read in this work). | Preferred; draft manifest in `packaging/stores/windows-msix/AppxManifest.DRAFT.xml` |
| b. Convert the existing WiX `.msi` with the MSIX Packaging Tool | Interactive/tool-driven capture of an installer; hides what lands in the package; the MSI contains WiX-specific actions (upgrade code in `tauri.conf.json`) that the conversion must drop. Not evaluated (tool not read, not available here). | Not recommended: weaker provenance than (a) |
| c. `cargo-packager` | The repo uses `cargo tauri build`; this tool was not evaluated and no claim is made that it emits MSIX. | Not evaluated |
| d. Tauri itself | Tauri's Store guide documents EXE/MSI only and says there is "no MSIX guidance" | Not available |

Whichever option is used: the reproducible-build, SBOM, notices and About checks in `packaging/build-all.sh` are written for the
current bundle; an MSIX adds a new artifact that none of them covers (Licenses folder inside the package, `registry.dat`/block map).

## 4. What in OUR app conflicts with a packaged Windows app

| # | Topic | Finding | Action |
|---|---|---|---|
| W1 | Loopback (127.0.0.1 server used by the browser) | Microsoft Learn (IPC page): "loopback connections for IPC are blocked by default for packaged applications"; `privateNetworkClientServer` and `LoopbackAccessRules` apply between packaged apps; `CheckNetIsolation.exe LoopbackExempt` is "only possible for sideload or debugging scenarios" and needs administrator rights, so it is not a distribution mechanism. BUT the capabilities page says full-trust (medium IL, `uap10:TrustLevel="mediumIL"`) apps "don't run in an AppContainer", and that `internetClient`/`enterpriseAuthentication` are examples of capabilities "that apply only to AppContainer apps". The launcher and the sidecar are normal Win32 processes (the sidecar is started by `CreateProcess` from the full-trust launcher; the browser is an unpackaged process). Reading the two pages together, loopback isolation should not apply to a full-trust package. Neither page states that sentence about loopback directly. | UNVERIFIED on Windows. First test of any MSIX build: launcher starts sidecar, handshake succeeds, installed browser opens `http://127.0.0.1:<port>/`. If blocked, that is a finding for the owner. Do NOT add `LoopbackExempt` instructions to user docs. Alternatives if it ever fails (all architecture changes): named pipe plus a native UI, an in-process server, or switching the UI into the webview. Draft manifest declares no network capability for this reason. |
| W2 | Install location read-only | Package files live under `C:\Program Files\WindowsApps\<package_full_name>`, read-only and locked down by the OS. | The sidecar must write nothing next to itself. The PyInstaller onefile bootloader extracts to `%TEMP%\_MEI*` (allowed), but a package-relative default such as `.agentic-os` resolved against the current directory would hit the read-only tree: `control_plane.py`/`cli.py` default to a cwd-relative `.agentic-os` (not shown to be reached from `serve`; check). |
| W3 | State location and uninstall | Per Microsoft Learn (behind-the-scenes): writes under `AppData\Local`/`Roaming` are redirected to a per-user, per-package location and removed on uninstall; HKCU writes likewise. Writes elsewhere "are allowed as long as the user has permission" and are NOT removed. Our state is in `%USERPROFILE%\.redevops\secrets` (`mission/onboarding.py`) and `%USERPROFILE%\.agentic-os\trusted-sources` (`mission/trust.py`): outside AppData, so not virtualised and left behind at uninstall, including secret material. Policy 10.2.7 and the MSI/EXE "no remnants" test are exactly about this. The same remnants issue exists for the direct MSI/NSIS installers. | Move state to `%LOCALAPPDATA%` (set `REDEVOPS_SECRET_DIR` and `AGENTIC_OS_HOME` from the launcher), decide a migration for existing installs, and define uninstall behaviour for secrets: owner decision D-MS-5. |
| W4 | Sidecar in the package (PyInstaller onefile) | The sidecar unpacks to `%TEMP%\_MEI*` at every start and runs from there: dynamic code outside the signed package, plus the well-known antivirus false-positive profile of PyInstaller onefile executables (launcher comment: "a first start on Windows with antivirus scanning can take tens of seconds"; `READY_TIMEOUT` is 90 s). Microsoft's malware scan and WACK run on the package, not on what a running onefile writes. Policy 10.2.2 is about downloading remote code, which this is not, but the extraction is a reviewer question. | A onedir sidecar inside the package avoids both extraction and the temp-dir scan surface; it is an architecture change (build-sidecar, stage, SBOM). Submit the package to Microsoft Defender before the Store scan. Estimate for onedir 1 week. |
| W5 | Docker Desktop control | `advanced_docker_*` run `docker compose` from a full-trust process: technically possible (medium IL), but 10.2.4 requires disclosing a non-integrated dependency in the description, and 10.2.3 forbids offering to install secondary software not by you that does not enhance the product. The WSL2/Docker prerequisites in `deploy/launcher/ACCEPTANCE.md` say the launcher guides installing them. | For the Store flavour keep Docker as a disclosed optional dependency or remove the feature; never download/install it from inside the app (10.2.2/10.2.3). The capability permissions in `capabilities/default.json` are untouched by this work. |
| W6 | Code that downloads and executes (policy 10.2.2) | `agentic_os/mission/pair_real.py` on Windows only raises an instruction to install Ollama; `msiexec /i <installer> /quiet` is present for PAIR; the POSIX `curl ... \| sh` does not apply on Windows. Reachability from the frozen `serve` was not traced. The default WebView2 `downloadBootstrapper` also downloads code at install time (route S-A forbids it). | Offline WebView2 for S-A. For MSIX, WebView2 is a dependency the package cannot bundle as an MSIX framework in this draft: Windows 11 ships the runtime; Windows 10 devices may need the Evergreen runtime (the manifest has no WebView2 dependency element; open point). |
| W7 | Upgrade semantics | MSIX updates are applied by the Store/App Installer; HKCU/AppData writes are kept on upgrade ("All writes are kept during package upgrade, and deleted only when the app is removed entirely"). The WiX `upgradeCode` `f13fc625-f77d-5c30-b308-a3c3ec1045fc` does not exist for MSIX: a user with the MSI installed and the MSIX installed would have two products side by side. | Define coexistence and the migration of the direct-install data (owner decision D-MS-5). |
| W8 | Architecture and version matrix | The pinned build is x64 only: the Python locks are `win_amd64`, the SBOM target and the sidecar name follow `x86_64-pc-windows-msvc`, and both `build-windows.ps1` and `build-launcher.sh` refuse any other host. An ARM64 Windows PC would run the x64 package under emulation. Native ARM64 needs new wheel locks (availability of ARM64 wheels for duckdb, cryptography, pydantic_core not checked), an ARM64 PyInstaller sidecar, `aarch64-pc-windows-msvc` Tauri build, and an MSIX bundle of two packages (or an `.msixbundle`). Minimum OS in the draft is Windows 10 1809 (`10.0.17763.0`), an open owner decision: Windows 11 only is what the acceptance VM tests. | Owner decision D-MS-3. |
| W9 | Privacy policy | Required for Win32/Desktop Bridge products (10.5.1). The product contacts model providers and connectors with user-supplied keys; the policy text must describe that. | Owner and counsel. |
| W10 | Licence | The product is source-available under the family licence (AGPL-3.0-or-later with the Commons Clause); `packaging/distribution-gate.json` lists 21 OPEN decisions (18 block distribution), including D-LIC-1 (wording, copyright holder), D-LIC-3 (Commons Clause with plain-AGPL/permissive code), D-LIC-5 (AGPL section 13 for a loopback UI), D-LIC-9 (NSIS, WiX and WebView2 notices, Windows), D-LIC-10 (acceptance pages) and pins-08 (Microsoft redistributable DLLs in the Windows sidecar). Store policy interaction to check with counsel: the Store listing must "accurately ... reflect the source" of the product (10.1.1; a statement like "open source" must not be used, D-LIC-15), and the Store's own distribution terms alongside the AGPL and Commons Clause are not analysed here. No legal conclusion is made. | Counsel (D-MS-6) |

## 5. Proposed `runFullTrust` justification (text for Partner Center; owner to approve)

"Agentic Apps R0-prime is a classic Win32 desktop application (a Tauri launcher plus a local helper process). It needs the
runFullTrust capability because it starts a bundled helper executable, which serves a local control plane on 127.0.0.1 that the
user's browser connects to. It does not install drivers or services, does not change system settings, and does not download code."
Whether each sentence is true for the shipped build must be re-checked before submission (for example the Docker control feature).

## 6. Drafts in the repository

* `packaging/stores/windows-msix/AppxManifest.DRAFT.xml` and `README.md`: XML well-formedness checked with `xmllint --noout` (OK).
  Schema validity, `makeappx` packing, signing and WACK were NOT run (no Windows host).
* Not created: image assets (`Assets\*.png`; `tauri icon` can generate Store icons per the Tauri Store page), the packaging script,
  `.msixupload`, any signing step.

## 7. Cross-checks of the existing Windows build against Store needs

`packaging/build-windows.ps1` wraps `packaging/build-all.sh` via Git-for-Windows bash and produces `.msi` and NSIS `.exe` under
`deploy\launcher\src-tauri\target\release\bundle\`. Nothing there signs; signing is staged (`packaging/signing/sign_windows_r0prime_v3.sh`
exists; the production signing credentials were not touched in this work). The Store S-A route additionally needs: every PE
signed (launcher, sidecar, the extracted DLLs are inside the frozen sidecar and cannot be signed individually, which is another
argument for onedir), offline WebView2, `/S` or `/quiet` verified, a versioned HTTPS host for the installer.

## 8. Owner decisions (none applied)

| Id | Decision | Recommendation |
|---|---|---|
| D-MS-1 | Open a Partner Center account (individual or company) and reserve the name; which legal person publishes | Owner; company account if the publisher is RedevOps |
| D-MS-2 | Route S-A (EXE/MSI listing), S-B (MSIX), or both; and whether to pursue the Store at all before the first direct release | S-A first, after signing and the licence decisions |
| D-MS-3 | x64 only, or add ARM64; minimum Windows version (10 1809 vs 11) | x64 only for the first Store listing |
| D-MS-4 | A code-signing certificate for S-A (OV from a CA, or Azure Artifact Signing which is eligibility-limited: organisations in USA, Canada, EU, UK with 3+ years tax history, individuals in USA/Canada, per the signing page); do NOT use the existing release credentials for this without the owner's explicit action | Owner |
| D-MS-5 | State location, migration and uninstall behaviour of secrets (W3, W7) | Move to `%LOCALAPPDATA%`; owner defines secret retention |
| D-MS-6 | The licence decisions in W10 and the privacy policy text before any Store submission | Counsel |
| D-MS-7 | Capabilities: accept `runFullTrust` and its justification (section 5); do not add `internetClient`, `internetClientServer` or `privateNetworkClientServer` unless a Windows test shows a need | Owner |
| D-MS-8 | Docker control and the WSL2/Docker guidance: keep as a disclosed optional dependency or remove from the Store flavour (W5) | Remove from Store flavour |

## 9. Sources (all fetched 2026-11-05 and readable unless noted)

* Open a developer account: https://learn.microsoft.com/en-us/windows/apps/publish/partner-center/open-a-developer-account (page date 2026-10-29)
* Microsoft Store Policies v7.20: https://learn.microsoft.com/en-us/windows/apps/publish/store-policies (10.1.1, 10.2.1 to 10.2.9, 10.5.1)
* MSIX app certification process: https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-certification-process
* MSI/EXE app certification process: https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msi/app-certification-process
* App capability declarations (full-trust vs AppContainer, `runFullTrust`, approval process): https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/app-capability-declarations
* Understanding how packaged desktop apps run (virtualisation, read-only install, uninstall): https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes
* IPC for packaged apps, loopback and `CheckNetIsolation`: https://learn.microsoft.com/en-us/windows/uwp/communication/interprocess-communication
* Sign an MSIX package: https://learn.microsoft.com/en-us/windows/msix/package/signing-package-overview
* Identity element: https://learn.microsoft.com/en-us/uwp/schemas/appxpackage/uapmanifestschema/element-identity (note: Learn shows the canonical URL `.../element-f-identity`)
* MakeAppx: https://learn.microsoft.com/en-us/windows/msix/package/create-app-package-with-makeappx-tool
* Tauri Windows installer (WebView2 modes, ARM64): https://v2.tauri.app/distribute/windows-installer/ ; Tauri Microsoft Store: https://v2.tauri.app/distribute/microsoft-store/
* Not readable: `https://learn.microsoft.com/en-us/windows/msix/package/create-app-package` (404). Not evaluated: the MSIX Packaging Tool, cargo-packager, the Store Developer CLI (`msstore`), WebView2 as an MSIX dependency, the exact `.msixupload` creation steps, the Store rule that the revision field of the version must be 0 (recalled, not re-verified), and current Store age-rating questionnaire details.
