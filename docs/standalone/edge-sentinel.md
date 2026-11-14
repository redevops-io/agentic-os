# Edge Sentinel (standalone)

Edge Sentinel is a security-operations dashboard and API over a CrowdSec core: it reads CrowdSec's alerts and ban decisions, turns each alert into a replayable case with its evidence, proposes a response, and runs a ban only after a named operator has approved it. This guide installs it on its own, on one host with Docker Compose or on Kubernetes/k3s, without any other ReDevOps product. The source is `apps/edge-sentinel` in this repository; licence: source-available under the family licence (AGPL-3.0-or-later with the Commons Clause).

The image is `ghcr.io/redevops-io/edge-sentinel`, for `linux/amd64` and `linux/arm64`, built from the `main` line of this repository (not from the `v0.6.x` release line). Tags are `<version>` and `<version>-<12-character commit>`; there is no `latest`. Verify a pull with the commands in [Verify the image](#verify-the-image).

## What it does

| Feature | How it works |
|---|---|
| Alert feed and KPIs | Reads active decisions (bouncer key) and alerts (watcher login) from the CrowdSec Local API, labelled with the time of the read. An unreadable source is reported as unavailable, never as zero. |
| Cases and evidence | Every alert becomes a case: immutable, content-addressed evidence, an observation, a finding, and a proposed `sentinel.block_ip` action for critical and high severities. `GET /api/cases/{id}` returns the bundle and a replay proof that the case id re-derives from the stored evidence. |
| Approval and execution | `POST /api/cases/{id}/respond` records the operator's decision (approve or reject, under the signed-in name). `POST /api/cases/{id}/execute` runs only an approved action, once, and returns a receipt and a read-back verification of the ban. Without `SENTINEL_BLOCK_ENABLED=1` execution is a recorded dry run. A failed execution is retried with backoff up to five times. |
| Ban guard | Loopback, link-local, multicast, private and reserved space, this host, the LAPI host and the operator's own address are never banned unless explicitly allowed (`SENTINEL_BLOCK_ALLOW_NETWORKS`, `SENTINEL_PROTECTED_IPS`). Durations are capped (`SENTINEL_BLOCK_MAX_HOURS`, default 168). |
| Assistant | `POST /api/agent` and the panel on the dashboard answer questions about what CrowdSec holds, through a read-only tool registry. A model is optional: with no `REDEVOPS_LLM_BASE_URL` the answers are deterministic summaries and nothing leaves the process. |
| Operator surface | `GET /capabilities` and `POST /invoke` expose `sentinel.triage`, `sentinel.block_ip` and `sentinel.unblock_ip` to a mission runtime. `/invoke` has its own credential (`AGENTIC_OS_CONTROL_TOKEN`) and is disabled until one is set. |
| Probes | `GET /health` (liveness) and `GET /readyz` (readiness: the state directory is usable and, in the production profile, an authority store is configured). Both are public and carry no case data. |
| Dashboard | `GET /`: status banner, KPI tiles, alert feed by severity, scenario meter, active decisions, the case panel with approve/reject/execute buttons, and the assistant. |

What the standalone image does not do, so nothing is promised that is not there:

* The detection engine is CrowdSec. Edge Sentinel does not read logs itself; point CrowdSec at your logs (see [CrowdSec log sources](#crowdsec-log-sources)).
* The repository's hunting, AI-security, agent-event, STIX/ATT&CK and SOC 2 evidence modules are libraries with their own tests. They are not wired to any route of this app in this release, so there is no API to ingest an agent event or a log batch into Edge Sentinel.
* The network-access (NetBird), backup (Portabase), runtime scan (deploy-scan), Wazuh and ClamAV panels need those services (`NETBIRD_API_URL`/`NETBIRD_API_TOKEN`, `PORTABASE_API_URL`/`PORTABASE_API_TOKEN`, `DEPLOY_SCAN_MCP_URL`, `WAZUH_API_URL`, `CLAMAV_HOST`). Without them the panels show labelled sample data or report themselves unavailable. They were not exercised against live services for this release.
* The supply-chain console tools (`supply_chain_scan`, `sbom`, `list_vulnerabilities`) need Trivy and Syft, which are not in the default image. Build with `--build-arg WITH_SCANNERS=1` to include both (pinned versions, sha256-verified for each architecture) and they run against the container's own filesystem. Scanning other containers needs Docker access, which the standalone image does not use.
* No docker CLI and no Docker socket: alerts and bans go over the CrowdSec Local API with a watcher login.

## Requirements

* Docker Engine with the Compose plugin (v2), or a Kubernetes/k3s cluster with a default StorageClass. NetworkPolicy is enforced only if the cluster's network plugin implements it (k3s does).
* Outbound HTTPS from the CrowdSec container at start, to refresh its hub index (parsers and scenarios). CrowdSec's online API and community blocklist are off unless you turn them on.
* About 150 MB of RAM for Edge Sentinel and 128 MB or more for CrowdSec. One replica of each: state is a file on a volume.

## Install with Docker Compose (one host)

```bash
git clone https://github.com/redevops-io/agentic-os.git
cd agentic-os/deploy/standalone/edge-sentinel
./bootstrap.sh
```

`bootstrap.sh` is the one-time bootstrap. It creates `./secrets/operator_password`, `./secrets/crowdsec_bouncer_key` and `./secrets/crowdsec_machine_password` (random, mode 0600, never printed), copies `.env.example` to `.env`, starts CrowdSec, registers the bouncer key and the `edge-sentinel` watcher login in it, and starts Edge Sentinel. It never overwrites an existing secret. Afterwards:

```bash
docker compose up -d        # start or update
docker compose logs -f      # logs
docker compose down         # stop (volumes are kept)
```

The dashboard is at `http://127.0.0.1:8203/`, published on the loopback interface only. Sign in as `operator` with the password in `./secrets/operator_password`. To reach it from other machines, put a TLS-terminating reverse proxy in front and keep the published address on loopback; if you must publish on another interface set `EDGE_SENTINEL_BIND` knowing the credential would otherwise travel in clear text.

Pin the image by digest for production: set `EDGE_SENTINEL_IMAGE=ghcr.io/redevops-io/edge-sentinel@sha256:<index digest>` in `.env`.

A first look with no CrowdSec traffic: set `EDGE_SENTINEL_DEMO=1` in `.env`. The dashboard then serves synthetic, labelled fixture data and performs no external read or write.

### CrowdSec log sources

CrowdSec only raises alerts for logs it is told to read. Mount the logs read-only into the `crowdsec` service, add an acquisition file under `/etc/crowdsec/acquis.d` and install a collection (`CROWDSEC_COLLECTIONS=crowdsecurity/linux` in `.env`). See the CrowdSec documentation for the acquisition syntax. To try the workflow without real attacks, add a test decision by hand:

```bash
docker compose exec crowdsec cscli decisions add --ip 203.0.113.10 --duration 4h --reason crowdsecurity/ssh-bf
```

## Install on Kubernetes or k3s

The manifests in `deploy/standalone/edge-sentinel/k8s` are a kustomization: Namespace, CrowdSec (Deployment, Service, two PVCs), Edge Sentinel (Deployment, Service, PVC), NetworkPolicies, and an optional Ingress example (`ingress.example.yaml`, placeholder host `sentinel.example.com`, not applied by default).

```bash
cd deploy/standalone/edge-sentinel/k8s
./bootstrap.sh              # prints the kubectl context and asks before it applies anything; -y skips the question
kubectl -n edge-sentinel port-forward svc/edge-sentinel 8203:80
kubectl -n edge-sentinel get secret edge-sentinel -o jsonpath='{.data.operator-password}' | base64 -d
```

The script creates the Secret `edge-sentinel` with random values (piped to kubectl, never written to disk or printed; `secret.example.yaml` shows the shape without values), applies the kustomization and registers the watcher login. It uses the current kubectl context: check it first. To pin another image: `kustomize edit set image ghcr.io/redevops-io/edge-sentinel=ghcr.io/redevops-io/edge-sentinel@sha256:<digest>`.

The pod runs as uid 10001 with a read-only root filesystem, no capabilities and the `RuntimeDefault` seccomp profile; state is on the PVC at `/data`. The NetworkPolicies deny everything by default, let Edge Sentinel talk to CrowdSec and DNS only, and let CrowdSec talk to DNS and HTTPS. Edit the ingress namespace label in `networkpolicy.yaml` to match your ingress controller. Put TLS on the Ingress: the credential is HTTP Basic.

### Verify the Kubernetes install on a real k3s

`deploy/standalone/edge-sentinel/verify-k3s.sh [IMAGE]` starts a throwaway k3s (the pinned `rancher/k3s` image, privileged, in Docker, under unique names), installs Edge Sentinel with `k8s/bootstrap.sh`, runs `routewalk.py` and `smoke.sh` (alert, case, approval, execution, read-back of the ban, pod restart, state kept), prints the pod security context and removes everything. It uses its own kubeconfig and never reads or changes yours. With no argument it tests the published 0.6.2 digest; a local image tag is loaded into the k3s. It needs docker, kubectl, python3 and curl, and runs in about three minutes; a CI job can call it as is.

## First run

1. Open the dashboard and sign in as `operator`.
2. Add a test decision as above. Within one refresh (the dashboard caches CrowdSec reads for 10 seconds) a case appears in the case panel, awaiting approval.
3. Approve it. The decision is recorded as `operator:operator`. Execute it: with `SENTINEL_BLOCK_ENABLED` unset the result is `DRY_RUN`. Set `SENTINEL_BLOCK_ENABLED=1` (and `SENTINEL_BLOCK_ALLOW_NETWORKS` for test ranges) in `.env` to enforce real bans.

`deploy/standalone/edge-sentinel/smoke.sh` runs this whole path against a Compose deployment and prints PASS or FAIL for each step, including a restart.

## Authentication and authorization

| Layer | Behaviour in the standalone profile |
|---|---|
| Sign-in | Every route except `/health` and `/readyz` needs the operator credential (HTTP Basic; browsers show a sign-in prompt). The app refuses to start without `SENTINEL_BASIC_AUTH_USER` and a password (`SENTINEL_BASIC_AUTH_PASS` or `SENTINEL_BASIC_AUTH_PASS_FILE`). This is set by `SENTINEL_REQUIRE_AUTH=1`, which the image sets. |
| Authorization of actions | An approval is recorded under the signed-in name; execution needs a recorded approving decision on that case and is refused otherwise (409). The action state machine (awaiting approval, approved, executed, failed, rejected) is persisted. |
| `/invoke` | Authenticated separately with `AGENTIC_OS_CONTROL_TOKEN` (503 while unset). In the production profile a side-effecting capability also needs a human approval or an execution authorization issued by a control point. |
| Production profile | `AGENTIC_OS_PROFILE=production` (the image default) requires a durable authority store (`AGENTIC_OS_AUTHORITY_DB=/data/authority.db`) and has no permissive fallback for side effects. Running without it is an explicit choice: `AGENTIC_OS_PROFILE=development` together with `SENTINEL_REQUIRE_AUTH=0`, and neither belongs on a reachable host. |
| Kernel permission plane | The image sets `AGENTIC_OS_PERMISSIONS=off`: per-tool grants are not used and `/health` reports `authorization: unenforced`. Access is the operator credential. |

There is a single operator credential, not per-user accounts or roles. The password is compared in constant time. Rotate it by replacing the secret file (or Kubernetes Secret) and restarting.

## Configuration reference

Set these in `.env` (Compose) or the Deployment environment (Kubernetes). A name ending in `_FILE` reads the value from that file, which is how the Compose and Kubernetes manifests supply every secret.

| Variable | Default in the image | Meaning |
|---|---|---|
| `SENTINEL_BASIC_AUTH_USER`, `SENTINEL_BASIC_AUTH_PASS` (`_FILE`) | none | The operator credential. Required. |
| `SENTINEL_REQUIRE_AUTH` | `1` | Standalone profile: sign-in on every route but the probes. |
| `AGENTIC_OS_PROFILE` | `production` | `production` or `development`. |
| `AGENTIC_OS_AUTHORITY_DB`, `AGENTIC_OS_REPLAY_DB` | `/data/authority.db`, `/data/replay.db` | Durable authority ledger and replay guard for the operator surface. |
| `AGENTIC_OS_CONTROL_TOKEN` (`_FILE`) | none | Credential for `/invoke`. Unset disables `/invoke`. |
| `CROWDSEC_LAPI_URL` | `http://localhost:8086` (the manifests set `http://crowdsec:8080`) | CrowdSec Local API. |
| `CROWDSEC_BOUNCER_KEY` (`_FILE`) | none | Reads decisions and verifies bans. |
| `CROWDSEC_MACHINE_ID`, `CROWDSEC_MACHINE_PASSWORD` (`_FILE`) | none | Watcher login: reads alerts and writes and lifts bans over the LAPI. Without it alerts need `cscli` through Docker, which this image lacks. |
| `CROWDSEC_FRONT_URL` | `http://localhost:8086` | Target of the dashboard's "Open CrowdSec metrics" button. |
| `SENTINEL_BLOCK_ENABLED` | unset | `1` makes an approved execution real. |
| `SENTINEL_BLOCK_ALLOW_NETWORKS`, `SENTINEL_BLOCK_ALLOW_WIDE_CIDR`, `SENTINEL_BLOCK_MAX_HOURS`, `SENTINEL_PROTECTED_IPS`, `SENTINEL_HOST_IPS` | unset, off, 168 | Ban guard. |
| `SENTINEL_STATE_DIR` | `/data/state` | Durable case-decision state. |
| `SENTINEL_TENANT` | unset | Organisation name in the dashboard; unset shows no name. |
| `SENTINEL_TRUST_PROXY` | off | Use the right-most `X-Forwarded-For` entry as the client address behind a proxy you control. |
| `SENTINEL_RATE_PER_MIN`, `SENTINEL_RATE_BURST` | 20, 10 | Per-client rate limit on the POST routes. |
| `EDGE_SENTINEL_DEMO` | unset | `1` serves synthetic fixture data and refuses every external effect. |
| `REDEVOPS_LLM_BASE_URL`, `REDEVOPS_LLM_MODEL`, `OPENAI_API_KEY` | none | Optional one-sentence narration on triage through an in-boundary OpenAI-compatible endpoint. Unset means no model call. |
| `SENTINEL_BIND_HOST`, `SENTINEL_ALLOW_EXPOSURE`, `PORT` | `0.0.0.0`, `1`, `8203` | Inside the container; the credential rule above is what protects the bind. |

The complete list with owners is `apps/edge-sentinel/config_contract.json`; the application README describes each.

## Persistence, backup and upgrade

State lives in the `edge-sentinel-data` volume (Compose) or PVC (Kubernetes), mounted at `/data`:

* `state/cases-state.json` (mode 0600): action states, decisions, receipts and case statuses. Cases and their evidence are re-derived from CrowdSec's alerts at each read, and the case id is content-addressed from the alert (a decision's remaining `duration` is excluded), so the same alert is the same case after a restart.
* `authority.db`, `replay.db`: the operator surface's authority ledger and replay guard.

CrowdSec's own volumes (`crowdsec-data`, `crowdsec-config`) hold its alerts and decisions; back them up as well.

```bash
docker compose stop edge-sentinel
docker run --rm -v edge-sentinel_edge-sentinel-data:/data:ro -v "$PWD":/backup busybox:1.37 tar czf /backup/edge-sentinel-data.tgz -C /data .
docker compose start edge-sentinel
```

Restore with the application stopped: `docker run --rm -v edge-sentinel_edge-sentinel-data:/data -v "$PWD":/backup:ro busybox:1.37 tar xzf /backup/edge-sentinel-data.tgz -C /data` (ownership by uid 10001 is preserved). The volume is named `<project>_edge-sentinel-data`; the project defaults to `edge-sentinel`.

Upgrade by changing the image tag or digest and running `docker compose pull && docker compose up -d` (Kubernetes: `kubectl apply -k .` after `kustomize edit set image`). The state file carries a version and an unexpected shape fails closed rather than being guessed at. Keep the previous image reference to roll back; do not run two replicas.

## Security model and defaults

* Sign-in on every route but two probes; the app does not start without a credential; secrets come from files, never from the image.
* Non-root (uid 10001), read-only root filesystem friendly (writable only `/data` and `/tmp`), no capabilities, no docker socket.
* Loopback publishing by default in Compose; default-deny NetworkPolicy in Kubernetes.
* Real bans are off until `SENTINEL_BLOCK_ENABLED=1`. A ban needs a recorded approval by the signed-in operator, a ban-target check and a read-back of the LAPI to be reported as enforced.
* Responses carry a content security policy that allows only the app's own origin, `X-Frame-Options: DENY` and no referrer; the dashboard makes no third-party request.
* The container's `HEALTHCHECK` and the Kubernetes probes use the public probes.
* Known limits: one shared operator credential over HTTP Basic (terminate TLS in front of it); no audit export beyond the state file and the receipts in each case bundle; the `/health` response states that the kernel permission plane is not enforcing.

## Operating limits

One replica per deployment; CrowdSec reads are cached for 10 seconds; the alert feed shows the most recent 50 alerts from the LAPI; the POST routes are rate limited per client address; approved executions retry at most five times with backoff; case state does not follow an alert that CrowdSec has expired. Sizing beyond a single host's worth of alerts has not been measured.

## Compatibility with the rAIlsApps distribution

The reference image (`apps/edge-sentinel/Dockerfile`) and the rAIlsApps stack are unchanged. The standalone profile adds one route (`GET /readyz`) and opt-in variables (`SENTINEL_REQUIRE_AUTH`, `SENTINEL_TENANT`, `CROWDSEC_MACHINE_ID`, `CROWDSEC_MACHINE_PASSWORD` and the `_FILE` forms). Without them the app behaves as before: reads are open, execution is authenticated. `apps/edge-sentinel/test_standalone.py` pins the route set and the variable names the distribution uses, and `apps/test_route_walk.py` covers the new probe. Two behaviours changed for everyone and are fixes: a ban or unban against a real CrowdSec LAPI now works when the watcher login is set (the bouncer key alone is refused by the LAPI for writes), and the same live alert no longer opens a new case on every read.

## Verify the image

```bash
cosign verify ghcr.io/redevops-io/edge-sentinel@sha256:<index digest> \
  --certificate-identity 'https://github.com/redevops-io/agentic-os/.github/workflows/oci-edge-sentinel.yml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/redevops-io/edge-sentinel@sha256:<index digest> --repo redevops-io/agentic-os
```

The image labels name the source commit. The signature and the GitHub attestation are on the index digest; the two per-architecture manifests are also signed (`cosign sign --recursive`).

| Release | Tags | Index digest | amd64 | arm64 | Source commit |
|---|---|---|---|---|---|
| 0.6.2 | `0.6.2`, `0.6.2-8fdd81761a4f` | `sha256:90fd0ab8be167535c63ef57c124f17f5ee436f255e47c9106187c49b6cb1e31b` | `sha256:641f0554286ccac1d6533f78522de02e73e114b22fc381f896d2959806ebd86d` | `sha256:0243be7012c44af3a9f032191329630e02d69c8f3ff416b06747c783f14517ec` | `8fdd81761a4fb4040a2436a8b5010b390620ca43` (tag `edge-sentinel-v0.6.2`, on `main`) |

To install exactly this release, set `EDGE_SENTINEL_IMAGE=ghcr.io/redevops-io/edge-sentinel@sha256:90fd0ab8be167535c63ef57c124f17f5ee436f255e47c9106187c49b6cb1e31b` in `.env`. Each later release adds a row.

## Build it yourself

```bash
docker buildx build -f apps/edge-sentinel/Dockerfile.standalone --platform linux/amd64,linux/arm64 \
  --build-arg SOURCE_DATE_EPOCH="$(git log -1 --format=%ct -- apps/edge-sentinel agentic_os)" -t edge-sentinel:local .
```

Run it from the repository root. The base image is pinned by digest, every Python dependency is pinned (`requirements.standalone.txt`; git requirements by commit), and the kernel is built from the same checkout.
