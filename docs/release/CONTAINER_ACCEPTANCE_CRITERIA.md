# Container acceptance criteria for the next release

Status: acceptance criteria set by the owner decision of 2026-11-06 for the next release of the runtime image, its compose
files and the reference Helm chart. Purpose and procedure only; this document makes no statement about production readiness.
Nothing here has been run against a cluster or an admission controller.

## 1. Why

The earlier published `redevops-runtime` tags `0.3.0`, `0.3.1` and `v6` are deprecated and unsupported. The reference chart ships
with NO default image, and the criteria below are what a release image, the compose files and the chart must satisfy: non-root,
dropped capabilities, health checks, immutable references, loopback publishing and secrets by reference.

## 2. The criteria

| # | Criterion | Requirement | Where it is set |
|---|---|---|---|
| C1 | Non-root containers | `runAsNonRoot: true`, uid/gid 10001 (postgres 999 for the optional bundled database) | image `USER 10001:10001`; chart `runtime.podSecurityContext` / `runtime.securityContext` |
| C2 | Dropped capabilities | `capabilities.drop: [ALL]`, no `add`; `allowPrivilegeEscalation: false`; never `privileged`; `seccompProfile: RuntimeDefault` | chart `runtime.securityContext` and the other workloads; compose/`docker run`: `cap_drop: [ALL]`, `no-new-privileges` |
| C3 | Read-only root filesystem where feasible | `readOnlyRootFilesystem: true` plus explicit writable volumes for the paths the application writes | chart: `emptyDir` at `/tmp`, `/data` (`AGENTIC_OS_HOME=/data`), `/app/.agentic-os`, ledger volume; compose: `read_only: true` with the same paths |
| C4 | Explicit health and readiness checks | `startupProbe`, `readinessProbe`, `livenessProbe` on `GET /health`; image `HEALTHCHECK` | chart `runtime.probes`; image Dockerfile |
| C5 | Immutable image references | `repo@sha256:<64 hex>` (a fixed version tag only as a fallback); never `latest`, never a floating tag, never `0.3.0`, `0.3.1` or `v6`; no chart default image; `imagePullPolicy: IfNotPresent` | chart `runtime.image.digest` (required) and `rr.imageRef`; third-party images pinned by digest |
| C6 | Secure network defaults | publish on loopback only; Service `ClusterIP`; no Ingress, LoadBalancer or NodePort unless set explicitly; default-deny `NetworkPolicy`; no host mounts (`hostPath`, `hostNetwork`, `hostPID`), no Docker socket | root `docker-compose.yml` (`${AOS_BIND_ADDR:-127.0.0.1}`); chart `runtime.service.type`, `networkPolicy.*` |
| C7 | Secrets by reference | `AGENTIC_OS_CONTROL_TOKEN` and other credentials from a Secret reference, never in values, never in the image | chart `runtime.controlTokenSecret`, `permissions.keySecret`, `retriever.pgvector.*Secret` |
| C8 | Resource bounds and no token | requests and limits on every container; `automountServiceAccountToken: false` | chart |

Writable paths of the runtime image (from the OCI runbook, section "Runtime hardening that was exercised"): `/data` (the
permissions key and grants; `AGENTIC_OS_HOME=/data`) and `/app/.agentic-os` (fleet workdir, ephemeral); the chart also gives
`/tmp` and sets `HOME=/tmp` and `PYTHONDONTWRITEBYTECODE=1`. On the main line the permissions directory is under the ledger
volume (`PERMISSIONS_DIR=/var/lib/redevops/ledger/permissions`). The probes need no credential.

## 3. Exceptions (justified, listed so they are not silent)

| Workload | Exception | Reason | Compensating control |
|---|---|---|---|
| ROCm vLLM model server (`model.server.enabled`, off by default) | no forced `runAsNonRoot`, no read-only root filesystem | GPU image: its user and cache/weights paths are not verified | digest-pinned image required (placeholder and tags refused), drop ALL, no privilege escalation, seccomp, ingress only from the runtime |
| bundled pgvector (`retriever.pgvector.bundled.enabled`, off by default) | runs as uid 999 (the image's postgres user) | the upstream image's data directory ownership | non-root, read-only root, digest-pinned, password from a Secret, ingress only from the runtime, no egress |
| `cockpit` (off by default) | image is user-supplied; defaults assume uid 10001 and a read-only root | no public cockpit image exists | override `cockpit.securityContext` explicitly |
| Projects image | needs a writable home (`/home/app`) | single-user application state | see the OCI runbook |
| Demo compose files under `deploy/` (stand-in fleet, showcase demos) | still build local `:latest` images and may publish on all interfaces | demonstration artefacts, not part of the release set | not for production; outside this criteria set |

## 4. How each criterion is verified

1. `tests/test_chart_security_defaults.py` (pytest):
   * static layer, always runs, no helm needed: reads `values.yaml`, the templates, the chart README/NOTES, the root compose
     file and CI workflows; asserts the defaults of C1 to C8, that no image default or deprecated/floating tag appears, that
     no `hostPath`/`hostNetwork`/`privileged`/LoadBalancer/NodePort/Ingress/Docker socket is present, and that the compose
     file publishes on loopback.
   * rendered layer, needs `helm` (v3 or v4; `HELM_BIN` or PATH; skipped with that reason otherwise): runs `helm template`
     and asserts every criterion on the manifests of every workload, with all optional workloads enabled; asserts the render
     FAILS without an image reference, with `latest`/`v6`/`0.3.0`/`0.3.1`, with the deprecated digests, with malformed
     digests, with an unpinned model server or cockpit without an image; asserts a digest renders as `repo@sha256:...`; token
     from a `secretKeyRef`; NetworkPolicy present and restrictive; overrides only by explicit value.
2. `helm lint`, `kubeconform -strict` and `trivy config` on the rendered matrix (defaults, each toggle); the recorded runs
   are in the release report.
3. The OCI qualification script `scripts/release/oci_prepare.sh` is on the RC line (`post-rc/oci-release`), not on `main`.
   Its stages `lint` (Dockerfile policy: digest-pinned base, `USER`, `HEALTHCHECK`, no `curl | sh`), `run` (both images with the
   hardened flags: read-only root, cap-drop ALL, no-new-privileges, uid 10001, loopback ports) and `verify` (digest-pinned
   compose and Kubernetes examples) check C1 to C5 for the image itself. The static test
   `test_dockerfile_meets_the_image_criteria` applies the same Dockerfile policy here; it is an expected failure on `main`
   until the image hardening is converged (see section 7).
4. Not covered by any of the above (stated, not hidden): a real cluster install, an admission controller (Pod Security
   `restricted`, Kyverno, Gatekeeper), a CNI that enforces the NetworkPolicy, kubelet probe behaviour under the NetworkPolicy,
   and the behaviour of the unpublished image inside the chart.

## 5. Reference chart: required inputs and network needs

* Required: `runtime.image.digest` (or `runtime.image.tag`); the chart fails to render without it. Needed for any use beyond
  `/health`: `runtime.controlTokenSecret` (an existing Secret with key `AGENTIC_OS_CONTROL_TOKEN`). On the main line the grant-store key is also required: `permissions.keySecret`.
* For a private (staged) package: `imagePullSecrets`.
* Network: the runtime needs DNS, and (only when configured) its model endpoint, an OTLP collector, an external Postgres, a
  SIEM or Vault, and connected modules. The default NetworkPolicy allows DNS and the release's own pgvector/model server
  only; everything else is added through `networkPolicy.egress.extraRules`, and callers through `networkPolicy.ingress.from`.
  The policy is enabled by default because a default install needs nothing else; it does not break a basic install, it
  blocks what is not declared. `networkPolicy.enabled=false` turns it off.

## 6. When the image is public (owner steps)

1. Publish the release image under the OCI runbook (staged first, verified, then promoted by digest); record the signed index
   digest.
2. Verify it as the runbook says (signature, attestations, anonymous pull behaviour as intended).
3. In a follow-up commit set `runtime.image.digest` in `deploy/reference-system/redevops-runtime/values.yaml` to that digest
   (do not use a tag), set the chart `version` and add an `appVersion` that names the released version, and add that digest
   to the chart README install example. Nothing in the chart is to name a digest that has not been published and verified.
4. Keep the deny-list in `templates/_helpers.tpl` (`0.3.0`, `0.3.1`, `v6` and their digests) until the old tags are removed
   from the registry after the notice period.
5. Re-run the tests above and the matrix; update the exceptions table if an exception was closed.

## 7. Status on this branch

`main`'s `Dockerfile` does not yet meet C1, C4 or the pinned-base part of C5 (it builds from the floating `python:3.12-slim`,
runs as root and has no `HEALTHCHECK`); the hardened Dockerfile, the compose loopback publish and the OCI qualification
script are on the RC line `post-rc/oci-release`. The chart does not depend on the Dockerfile for C1 to C4: it forces uid 10001,
the read-only root, the writable volumes and the probes itself, so it also runs a root-built image unprivileged. Converging the
image hardening into `main` is a prerequisite for the release; the root `docker-compose.yml` on this branch publishes on
loopback (C6).
