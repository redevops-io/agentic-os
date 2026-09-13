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
| **Task 1 — wheelhouse / offline install / git+https gone** | ✅ **verified here** — 24 wheels, fresh `--no-index` install of `agentic-os[projects,duckdb]`, `import projects_api + bootstrap` OK, **0 VCS-origin dists** |
| **Task 2 — PyInstaller sidecar** | ✅ **verified here** — frozen `redevops-sidecar` builds on Linux/py3.12 and runs the `bootstrap` brain (device report + LLM) |
| **Task 3 — native-first launcher** (Rust) | ✍️ authored; needs `cargo tauri build` on a Rust+GUI host (no toolchain here) |
| **Task 4 — .deb / installers** (Tauri) | ✍️ config authored (`deb,appimage,msi,nsis,dmg`); build on a host per OS |
| **Task 5 — Ubuntu acceptance** | ✍️ `ACCEPTANCE-ubuntu.md` + `acceptance-ubuntu.sh` (CI-runnable slice); full journey on a VM |
| **Task 6 — CI matrix** | ✍️ workflow authored; runs on push of a tag / dispatch |

Rust/Tauri and PyInstaller aren't installed in the authoring environment, so anything past Task 2
is authored + syntax/JSON-validated, not build-run here. Ubuntu is the proving OS (no signing);
Windows/macOS build the identical recipe unsigned until signing is staged in (decision 4).

## Notes
- **Bundle target = CPython 3.12** (`$PYTHON_VERSION` overrides). The wheelhouse/lock pin exact versions.
- **Suite, not per-app:** one install; `PROJECTS_APPS=<ids>` selects which apps the UI offers.
- **Model on first run:** hosted key **or** local model (Ollama/vLLM) **or** guided free tier — via the
  brain's `default_llm`. Same contract as the parked try-container (`SIDEKICK_MODEL_*`).
- **Docker is advanced/opt-in:** the launcher's `advanced_docker_*` commands enable the container tier
  (observability combo + rootless code-exec membrane); nothing here requires it.
- Build artifacts (`wheelhouse/`, `dist/`, `build/`) are git-ignored; `requirements.lock` is committed.
