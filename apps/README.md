# Reference apps

The reference application implementations that run on the Mission Runtime — one focused
agentic system per business function. (Each `apps/*/LICENSE` is a plain AGPL-3.0 text, while the repository
root [LICENSE.md](../LICENSE.md) is titled "source-available, not open source".) Each app is the *implementation* (agents +
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
(Python) and [`../go/mission/`](../go/mission/) (Go).

## Apps

`billing` · `books` · `compliance` · `support` · `control-tower` · `edge-sentinel` ·
`growth-engine` · `growth-assistant` · `market-radar` · `social-autopilot` · `outreach-engine` ·
`lifecycle` · `guide` · `agentic-crm` · `agentic-privacy` — plus `infra` (deploy / teardown /
cost-audit / drift) and `sky` (SkyPilot elastic-compute), the operators that turn a deployment
into a governed mission.

## Upgrading images

App images run as an unprivileged user (uid 10001). `/data` is owned by that user inside the image, and the
container refuses to start with a clear message if `/data` is not writable.

- Existing state volumes created by an earlier (root) image must be re-owned once. For a named volume
  (agentic-privacy and growth-assistant keep state in `/data`; any other app given a state volume is the same):
  `docker run --rm --user 0 -v <volume>:/data python:3.12-slim chown -R 10001:10001 /data`
  Bind mounts need the same `chown` on the host path.
- Apps that talk to the Docker socket need the socket's group added to the container, e.g. compose
  `group_add: ["<docker gid>"]` (find it with `stat -c %g /var/run/docker.sock`):
  - edge-sentinel (docker CLI used for `cscli`, Trivy/Syft/Cosign inspection)
  - compliance (only when `scan.sh` runs in its docker mode)
