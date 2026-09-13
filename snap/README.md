# ReDevOps Projects — Snap package

Packages the **Projects control plane** as a strict-confinement snap so Ubuntu users can install it
from the App Center / Snap Store and try ReDevOps before deploying the full stack. Same scope as the
cross-platform [Try container](../deploy/try): the local web control plane + connectors + a model you
provide — **not** the host-infra Sidekick combo (Terraform/kubectl/monitoring), which strict
confinement can't sandbox and which stays in the Docker-Compose / Helm quickstart.

## Configure your model (hosted or local)

```bash
# Hosted / cloud — bring your key (base-url defaults to OpenAI):
snap set redevops-projects llm.api-key=sk-...            # optionally llm.base-url=… llm.model=…

# Local / self-hosted model — no key:
snap set redevops-projects llm.base-url=http://localhost:11434/v1   # Ollama; or vLLM :8000/v1
```
The `configure` hook restarts the service so changes take effect. Open the UI from the app menu
(**ReDevOps Projects**) or visit http://127.0.0.1:8787.

## Build, register, publish

> ⚠️ **Not yet built here.** These files are authored but a real `snapcraft` build needs LXD/Multipass,
> which this environment doesn't provide. Build + test on an Ubuntu box with `snapcraft` before
> releasing. The `override-build` recipe (git deps, the `[projects]` extra, bundling
> `agentic_os/projects_ui`) is the part most likely to need a tweak on first real build.

```bash
sudo snap install snapcraft --classic
snapcraft                                   # builds redevops-projects_0.1.0_amd64.snap (in LXD)

# local strict-confinement test:
sudo snap install --dangerous ./redevops-projects_0.1.0_amd64.snap
snap set redevops-projects llm.api-key=sk-...
snap connect redevops-projects:home         # if home access isn't auto-connected
xdg-open http://127.0.0.1:8787

# publish (needs your Canonical / Ubuntu One login):
snapcraft login
snapcraft register redevops-projects        # global name — claim it regardless
snapcraft upload --release=edge  redevops-projects_0.1.0_amd64.snap
# …then promote once verified:
snapcraft release redevops-projects <rev> stable
```

Add store metadata (screenshots, banner, category) in the Snapcraft web dashboard — that's what the
App Center shows when users browse.

## Files
- `snapcraft.yaml` — build + package definition (core24, strict, python plugin + `[projects]` extra).
- `local/launch.sh` — service wrapper: maps `snap set llm.*` → the runtime's `SIDEKICK_MODEL_*` env.
- `local/open-ui.sh` — the desktop action: opens the browser at the control plane.
- `hooks/configure` — restarts the service when config changes.
- `gui/redevops-projects.desktop` + `gui/redevops-projects.png` — App Center entry + icon.
