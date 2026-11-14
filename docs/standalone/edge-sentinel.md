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

The analysis modules that are safe to expose are routed under `/api/v1`, each with validated, bounded input and no side effect: `POST /hunt/validate` (is this query read-only and well formed; nothing is executed), `GET /attack/map?scenario=` (ATT&CK techniques for a CrowdSec scenario, deterministic, with the matched token as the basis), `POST /ioc/normalize` (canonical STIX observable and indicator), `POST /detections/validate` (Sigma or YARA structure; never published), `POST /agent-events` (one agent-runtime event becomes an investigating case: a signal that proposes no action; operator role or the service credential) and `GET /soc2/controls` (the control catalogue). `GET /api/v1/info` lists everything that is not routed, each with its reason.

What the standalone image does not do, so nothing is promised that is not there:

* The detection engine is CrowdSec. Edge Sentinel does not read logs itself; point CrowdSec at your logs (see [CrowdSec log sources](#crowdsec-log-sources)).
* DFIR timelines, enrichment providers, the evaluation harness, the risk handoff, AI-security assessment of runtime events, the CTI graph, detection publishing and the SOC 2 evidence collectors are library code without a safe client input path or a consumer in this deployment: they are not routed, and `/api/v1/info` says so. A log batch cannot be ingested into Edge Sentinel; CrowdSec reads the logs.
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

Every route except `/health` and `/readyz` needs a credential, and the app refuses to start without an operator credential (`SENTINEL_BASIC_AUTH_USER` and a password, or `SENTINEL_BASIC_AUTH_PASS_FILE`). Who is signed in is a named local user:

| Credential | Used by | Notes |
|---|---|---|
| Session cookie | the browser UI | `sentinel_session`, `HttpOnly`, `SameSite=Strict`, server-side, idle timeout 30 minutes, absolute 12 hours, at most 5 per user. Every write needs the per-session `X-CSRF-Token` and, when the browser sends one, a matching `Origin`. |
| HTTP Basic | scripts, `curl`, `smoke.sh` | checked against the same user table; no cookie, so no CSRF. |
| Service credential | Projects/Sidekick, other products | `X-Control-Token` (`AGENTIC_OS_CONTROL_TOKEN`): may read and stage a proposal, never decide or execute. |
| Approval token | Projects, for one click | `X-Approval-Token` with the service credential: minted for one action digest, single use, honoured only for a Projects principal an admin linked to a user with the `operator` role; the decision is recorded for that user with `via: projects`. |

### Users and roles

Users are stored in the same SQLite file as the history; passwords are `scrypt` hashes (at least 12 characters). On the first start with no users, the operator credential the deployment ships with becomes the first `admin`, so the secrets created by `bootstrap.sh` keep working: sign in with it, create named users, then retire the shared one. There are three fixed roles:

| Role | Can |
|---|---|
| `viewer` | read events, decisions, cases, evidence, actions, history |
| `operator` | viewer, plus stage a manual ban or unban, approve or reject, and execute |
| `admin` | operator, plus manage users and actor links |

Manage users with the admin API (`/api/v1/users`) or from the command line, in the same state directory the server uses:

```bash
docker compose exec edge-sentinel edge-sentinel users list
```

Every decision and execution is recorded with the user who made it, their role at that moment, and how (`session`, `basic`, `projects`, `runtime`). An approval is bound to the digest of the exact action that was shown: if the action changes, the approval is void (409 `stale_approval`). With `SENTINEL_REQUIRE_TWO_PERSON=1` the approver may not also execute. Execution needs a recorded approving decision, is a recorded dry run until `SENTINEL_BLOCK_ENABLED=1`, and a ban is reported as contained only when the read-back of CrowdSec confirms it.

### Identity and its boundary

This is the identity of the source-available distribution: a small local user table, three fixed roles, attributable approvals and an audit trail. The following are deliberately not built here, and nothing in this product depends on an Enterprise identity or permission service: single sign-on (OIDC, SAML), directory sync, groups, tenants and multi-tenant isolation, custom roles, per-case or per-object permissions, MFA enrolment, password reset by e-mail, and organisation-wide policy administration and compliance reporting. The kernel's basic permissions plane (`AGENTIC_OS_PERMISSIONS=1`) is unchanged: when it is on, its grants also gate `/api/v1` (reads need the `threat_summary` tool, decisions and execution the `ban_ip` tool); when it is off, the fixed role matrix above is the authorization. Basic permissions authorise the app instance, not individual callers.

### Production profile

`AGENTIC_OS_PROFILE=production` (the image default) requires a durable authority store (`AGENTIC_OS_AUTHORITY_DB=/data/authority.db`) and has no permissive fallback for side effects. Running without it is an explicit choice: `AGENTIC_OS_PROFILE=development` together with `SENTINEL_REQUIRE_AUTH=0`, and neither belongs on a reachable host. `/invoke` is authenticated separately with `AGENTIC_OS_CONTROL_TOKEN` (503 while unset); a side-effecting capability there needs an approval from the runtime, and it then writes the same case, decision, receipt and read-back records as the browser.

## Configuration reference

Set these in `.env` (Compose) or the Deployment environment (Kubernetes). A name ending in `_FILE` reads the value from that file, which is how the Compose and Kubernetes manifests supply every secret.

| Variable | Default in the image | Meaning |
|---|---|---|
| `SENTINEL_BASIC_AUTH_USER`, `SENTINEL_BASIC_AUTH_PASS` (`_FILE`) | none | The operator credential. Required. |
| `SENTINEL_REQUIRE_AUTH` | `1` | Standalone profile: sign-in on every route but the probes. |
| `AGENTIC_OS_PROFILE` | `production` | `production` or `development`. |
| `AGENTIC_OS_AUTHORITY_DB`, `AGENTIC_OS_REPLAY_DB` | `/data/authority.db`, `/data/replay.db` | Durable authority ledger and replay guard for the operator surface. |
| `AGENTIC_OS_CONTROL_TOKEN` (`_FILE`) | none (the Compose and Kubernetes files generate one) | The service credential (`X-Control-Token`): `/invoke`, and reads and staging from Projects/Sidekick. Unset disables both. |
| `AGENTIC_OS_APPROVAL_SECRET` (`_FILE`) | none (generated likewise) | Verifies the per-click approval tokens Projects mints; the same value must be given to Projects. Unset: such tokens are refused and Projects can read and stage but not decide. |
| `CROWDSEC_LAPI_URL` | `http://localhost:8086` (the manifests set `http://crowdsec:8080`) | CrowdSec Local API. |
| `CROWDSEC_BOUNCER_KEY` (`_FILE`) | none | Reads decisions and verifies bans. |
| `CROWDSEC_MACHINE_ID`, `CROWDSEC_MACHINE_PASSWORD` (`_FILE`) | none | Watcher login: reads alerts and writes and lifts bans over the LAPI. Without it alerts need `cscli` through Docker, which this image lacks. |
| `CROWDSEC_FRONT_URL` | `http://localhost:8086` | Target of the dashboard's "Open CrowdSec metrics" button. |
| `SENTINEL_BLOCK_ENABLED` | unset | `1` makes an approved execution real. |
| `SENTINEL_BLOCK_ALLOW_NETWORKS`, `SENTINEL_BLOCK_ALLOW_WIDE_CIDR`, `SENTINEL_BLOCK_MAX_HOURS`, `SENTINEL_PROTECTED_IPS`, `SENTINEL_HOST_IPS` | unset, off, 168 | Ban guard. |
| `SENTINEL_STATE_DIR` | `/data/state` | Durable case-decision state. |
| `SENTINEL_REQUIRE_TWO_PERSON` | unset | The approver of an action may not execute it. |
| `SENTINEL_TENANT` | unset | Organisation name in the dashboard; unset shows no name. |
| `SENTINEL_TRUST_PROXY` | off | Use the right-most `X-Forwarded-For` entry as the client address behind a proxy you control. |
| `SENTINEL_RATE_PER_MIN`, `SENTINEL_RATE_BURST` | 20, 10 | Per-client rate limit on the POST routes. |
| `EDGE_SENTINEL_DEMO` | unset | `1` serves synthetic fixture data and refuses every external effect. |
| `REDEVOPS_LLM_BASE_URL`, `REDEVOPS_LLM_MODEL`, `OPENAI_API_KEY` | none | Optional one-sentence narration on triage through an in-boundary OpenAI-compatible endpoint. Unset means no model call. |
| `SENTINEL_BIND_HOST`, `SENTINEL_ALLOW_EXPOSURE`, `PORT` | `0.0.0.0`, `1`, `8203` | Inside the container; the credential rule above is what protects the bind. |

The complete list with owners is `apps/edge-sentinel/config_contract.json`; the application README describes each.

## Persistence, backup and upgrade

State lives in the `edge-sentinel-data` volume (Compose) or PVC (Kubernetes), mounted at `/data`:

* `state/sentinel.db` (mode 0600, SQLite WAL): cases (a snapshot that stays readable after CrowdSec expires the alert), content-addressed evidence, actions, decisions with the acting user, receipts with the persisted read-back, the append-only history that is also the change feed, local users and sessions. Back it up with the application stopped.
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
* Known limits: sign-in is a local user table with three roles (terminate TLS in front of it; no SSO or MFA); the audit trail is the history table and the receipts in each case bundle, with no export; the `/health` response states that the kernel permission plane is not enforcing.

## Operating limits

One replica per deployment; CrowdSec reads are cached for 10 seconds; the alert feed shows the most recent 50 alerts from the LAPI; the POST routes are rate limited per client address; approved executions retry at most five times with backoff; case state does not follow an alert that CrowdSec has expired. Sizing beyond a single host's worth of alerts has not been measured.

## Using it from Projects and Sidekick

`bootstrap.sh` (Compose) and `k8s/bootstrap.sh` generate a service credential (`secrets/control_token`) and an approval secret (`secrets/approval_secret`; Kubernetes keys `control-token`, `approval-secret`). To connect Projects, give it the product URL, the control token and the same approval secret, then, as an admin, link each Projects principal that may approve to an Edge Sentinel user with the `operator` role (Users, Projects links, or `POST /api/v1/actor-links`). Projects can then read the queue and stage proposals with the service credential; an approval clicked in Projects is accepted only as a single-use token bound to that action's digest and to the linked user, and is recorded as that user with `via: projects`. The product pages Projects links to are `/ui/cases/<case-id>` and `/ui/actions/<act-id>`. The same records are what the browser console shows.

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
