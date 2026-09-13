# Native packaging — Ubuntu · Windows · macOS

One **ReDevOps suite** installer per OS that delivers *most* functionality with **no Docker, no
system Python, no Git** on the end-user path. Plan + locked decisions:
`~/Documents/AGENTIC_APPS_CROSS_PLATFORM_PACKAGING_PLAN.md`.

## Architecture

```
Native installer (.deb / AppImage / .msi / .dmg)
  └─ Tauri shell  (deploy/launcher — native window, native-first: spawns the sidecar; Docker is opt-in)
       └─ redevops-sidecar   (PyInstaller: frozen Python 3.12 + the whole control plane + brain)
             └─ built from the OFFLINE wheelhouse — git+https deps rewritten to plain wheels
```

## Build order (one reproducible recipe → all platforms)

```bash
./packaging/build-wheelhouse.sh   # Task 1: wheels + requirements.lock; proves offline --no-index install
./packaging/build-sidecar.sh      # Task 2: freeze → packaging/dist/redevops-sidecar
./packaging/stage-sidecar.sh      # place it as Tauri externalBin (per target triple)
cd deploy/launcher && cargo tauri build   # Task 4: emits the native installer(s) for the host OS
```

CI runs exactly this across ubuntu/windows/macos: `.github/workflows/native-packaging.yml`.

## What's verified vs pending a build host (honest status, 2026-09-13)

| Piece | Status |
|---|---|
| **Task 1 — wheelhouse / offline install / git+https gone** | ✅ **verified** — 24 wheels, fresh `--no-index` install of `agentic-os[projects,duckdb]`, `import projects_api + bootstrap` OK, **0 VCS-origin dists** |
| **Task 2 — PyInstaller sidecar** | ✅ **verified** — frozen `redevops-sidecar` builds (py3.12), runs the `bootstrap` brain AND `serve` (served the real UI `<title>Projects & Sidekick</title>`) |
| **Task 3 — native-first launcher** (Rust/Tauri) | ✅ **verified on evo-x2** — `cargo tauri build` compiles the native-first shell (Tauri 2.11.4) |
| **Task 4 — .deb / AppImage** (Tauri) | ✅ **verified on evo-x2 (Ubuntu 26.04)** — built `ReDevOps_0.1.0_amd64.deb` (49 MB) + `.AppImage` (125 MB), each bundling `redevops-launcher` + `redevops-sidecar` + icon + `.desktop` |
| **Task 5 — Ubuntu acceptance** | ✅ **install→serve verified** — `.deb` installs (`/usr/bin/redevops-sidecar`), serves the control plane natively (no Docker/Python/Git), removes cleanly. Full journey (OAuth→Mission→approve→reboot) still on a VM snapshot. |
| **Task 6 — CI matrix** | ✍️ workflow authored; Windows/macOS build the identical recipe (unsigned until signing staged in, decision 4) |

Verified end-to-end on **evo-x2 (Ubuntu 26.04)** with Rust 1.98.1 + WebKitGTK 4.1 + tauri-cli 2.11.4
+ CPython 3.12. Product/package name is **`agentic-apps`** (display "Agentic Apps"); the full icon set
(`.ico` + `.icns` + PNGs) is generated. Windows/macOS use the identical recipe on their build hosts —
see **`BUILD-hosts.md`** (Ubuntu → evo-x2 ✅, Windows → Proxmox Win11 image via `build-windows.ps1`,
macOS → AWS EC2 Mac dedicated host). Only signing/notarization + winget/Homebrew wiring remain (P2).

## Notes
- **Bundle target = CPython 3.12** (`$PYTHON_VERSION` overrides). The wheelhouse/lock pin exact versions.
- **Suite, not per-app:** one install; `PROJECTS_APPS=<ids>` selects which apps the UI offers.
- **Model on first run:** hosted key **or** local model (Ollama/vLLM) **or** guided free tier — via the
  brain's `default_llm`. Same contract as the parked try-container (`SIDEKICK_MODEL_*`).
- **Docker is advanced/opt-in:** the launcher's `advanced_docker_*` commands enable the container tier
  (observability combo + rootless code-exec membrane); nothing here requires it.
- Build artifacts (`wheelhouse/`, `dist/`, `build/`) are git-ignored; `requirements.lock` is committed.
