# Reference apps

The reference application implementations that run on the Mission Runtime — one focused
agentic system per business function. (**Licence: every app in this directory is licensed under GNU AGPL-3.0-only, without the Commons Clause.** Each `apps/<name>/LICENSE` is the GNU AGPL
version 3 text; `SPDX-License-Identifier: AGPL-3.0-only`. The control plane in the repository root is a different category: its
[LICENSE.md](../LICENSE.md) is "source-available, not open source" (AGPL-3.0-or-later with the Commons Clause). Third-party cores that the apps wrap keep their own
licences. How the apps relate to the core they import is under legal review, D-LIC-3 in [LICENSING.md](../LICENSING.md).) Each app is the *implementation* (agents +
operators + core logic + seed data); the **deployment** is kept separate — a self-contained compose
demo lives in [`../deploy/`](../deploy/), while the full Terraform / Ansible / k3s machinery is
maintained separately — so you can run these apps under your own infrastructure.

Each app follows the shared operator pattern:

| File | Role |
|---|---|
| `core.py` | pure business logic, no side effects — unit-testable |
| `operator.py` | the policy-guarded, gated mutation surface (a Mission Runtime operator) |
| `app.py` | the service entrypoint (conversational agent: Context Runtime + redevops-rag grounding) |
| `seed.py` / `seed.rb` | demo fixtures |
| `test_operator.py` | operator tests |

The catalog that binds these to the fleet — repo, agents, and which tasks need approval — is
[`../modules.yaml`](../modules.yaml). The kernel they run on is [`../agentic_os/mission/`](../agentic_os/mission/)
(Python).

## Apps

`billing` · `books` · `compliance` · `support` · `control-tower` · `edge-sentinel` ·
`growth-engine` · `growth-assistant` · `market-radar` · `social-autopilot` · `outreach-engine` ·
`lifecycle` · `guide` · `agentic-crm` · `agentic-privacy` — plus `infra` (deploy / teardown /
cost-audit / drift) and `sky` (SkyPilot elastic-compute), the operators that turn a deployment
into a governed mission.

## Authorization setting (required for the images)

The app images refuse to start unless the authorization setting is explicit
(`AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING=1` is baked into them): pass `-e AGENTIC_OS_PERMISSIONS=1` to enforce
permissions (run `agentic-os permissions init` first; with no grants the app starts restricted), or
`-e AGENTIC_OS_PERMISSIONS=off` to run deliberately standalone (a start-up line says so). Run directly on a
loopback address (`uvicorn ... --host 127.0.0.1`) the setting may stay unset: the app starts as before and logs a notice
that authorization is not enforced. See `docs/PERMISSIONS_MIGRATION.md` section 19.

Note: basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write.

## Upgrading images

App images run as an unprivileged user (uid 10001). `/data` is owned by that user inside the image, and the
container refuses to start with a clear message if `/data` is not writable.

- Existing state volumes created by an earlier (root) image must be re-owned once. For a named volume
  (agentic-privacy and growth-assistant keep state in `/data`; any other app given a state volume is the same):
  `docker run --rm --user 0 -v <volume>:/data python:3.12-slim chown -R 10001:10001 /data`
  Bind mounts need the same `chown` on the host path.
- Apps that talk to the Docker socket need the socket's group added to the container, e.g. compose
  `group_add: ["<docker gid>"]` (find it with `stat -c %g /var/run/docker.sock`):
  - edge-sentinel: only if an operator enables a Docker-backed feature; see its README ("Docker access"): use a restricted, operator-supplied proxy or a separate scanner worker, not the raw socket
  - compliance (only when `scan.sh` runs in its docker mode)
