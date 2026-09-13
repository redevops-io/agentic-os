# Build hosts — one recipe, three OSes

Every OS runs the same pipeline: **offline wheelhouse → PyInstaller sidecar → stage → `cargo tauri
build`**. Only the host and the final bundle format differ. Ubuntu is proven; Windows/macOS reuse the
identical recipe.

| OS | Host | Command | Output | Status |
|---|---|---|---|---|
| **Ubuntu / Linux** | **evo-x2** (Ubuntu 26.04) | `build-wheelhouse.sh` → `build-sidecar.sh` → `stage-sidecar.sh` → `cargo tauri build` | `agentic-apps_0.1.0_amd64.deb` + `.AppImage` | ✅ built + install/serve verified |
| **Windows** | **Proxmox Win11 image** | `packaging\build-windows.ps1` (PowerShell mirror of the bash flow) | `.msi` + `.nsis` | authored; run on the image |
| **macOS** | **AWS EC2 Mac dedicated host** | `build-wheelhouse.sh` → `build-sidecar.sh` → `stage-sidecar.sh` → `cargo tauri build --bundles dmg` (the bash scripts run as-is on macOS) | `.dmg` / `.app` | authored; run on the Mac host |

## Ubuntu (evo-x2) — reference, verified
```bash
./packaging/build-wheelhouse.sh
./packaging/build-sidecar.sh
./packaging/stage-sidecar.sh
cd deploy/launcher && cargo tauri build            # → .deb + .AppImage
```
Toolchain (one-time): `sudo apt-get install libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev
libayatana-appindicator3-dev patchelf build-essential`; rustup; `cargo install tauri-cli --version ^2`.

## Windows (Proxmox Win11 image)
No SSH to the image (driven via QEMU sendkey) — run the build interactively or via a provisioning
script. One-time: Rust (msvc) + VS Build Tools C++ workload, uv, Git, `cargo install tauri-cli`.
Then: `powershell -File packaging\build-windows.ps1`. WebView2 ships with Win11.
Signing is **staged** (decision 4): sign the `.msi` with an OV/EV code-signing cert before release
(otherwise SmartScreen warns). winget manifest → P2.

## macOS (AWS EC2 Mac dedicated host)
Only a **dedicated host** is available (EC2 Mac requires one; ~24h min allocation). The Unix bash
scripts run unchanged; the icon set already includes `icon.icns`.
```bash
./packaging/build-wheelhouse.sh && ./packaging/build-sidecar.sh && ./packaging/stage-sidecar.sh
cd deploy/launcher && cargo tauri build --bundles dmg      # → .dmg (+ .app)
```
Signing/notarization **staged**: without an Apple **Developer ID** + `notarytool`, Gatekeeper blocks
the download. Build unsigned first to prove the bundle, then sign+notarize as macOS enters release.
Build both arches (Apple-silicon host + `--target x86_64-apple-darwin`) or a universal binary.

## CI
`.github/workflows/native-packaging.yml` runs this matrix on GitHub runners too (ubuntu/windows/
macos) — useful for unsigned smoke builds; the dedicated hosts above are for signed release builds.
