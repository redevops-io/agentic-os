# OCI release runbook: publishing the release images to GHCR

Status: PROPOSAL for owner approval. Nothing in this document has been executed against ghcr.io or any
remote registry. The local rehearsal (section 10) exercises every step that does not need GitHub: build,
digests, SBOM, scans, throwaway-key signing, verification and negative tests. The GitHub-side steps
(OIDC identity, `gh attestation`, package visibility) are described from the GitHub and Sigstore
documentation and have not been run for this repository; verify each against the current documentation
when the runbook is executed.

Scope: purpose and procedure only. It makes no statement about production readiness, and it does not
replace the advisory and release gates listed in section 2.

## 1. Release set

| Image (ghcr.io/redevops-io/...) | Built from | Port | Why it is in the set |
|---|---|---|---|
| `redevops-runtime` | `Dockerfile` (control plane: `agentic_os.control_plane`) | 8080 | The runtime image the reference Helm chart (`deploy/reference-system/redevops-runtime`) and the root `docker-compose.yml` reference. `/health` probe, no external dependency. |
| `redevops-projects` | `Dockerfile.projects` (Projects UI + API on one origin) | 8787 | The current end-user application (`agentic-os-projects`), documented as the self-hosted deployment. |

Everything else with a Dockerfile in this repository is a demo or development artefact and is NOT part
of the release set:

* `apps/*/Dockerfile` (15 reference apps): unpinned `python:3.12-slim`, run as root, no healthcheck,
  and install `agentic-os` and `context-runtime` from tarballs of older commits. They are reference
  implementations; `apps/edge-sentinel` additionally downloads trivy, syft, cosign (`latest`) and a
  hard-coded `x86_64` docker CLI at build time, so it is neither reproducible nor multi-arch.
* `deploy/*/Dockerfile` (admin-console, attention-console, evidence-explorer-demo,
  security-showcase-demo, unsubscribe-service, world-canvas-demo, refcore-billing): demo services.
  Six of them `COPY runtime_contracts`, a directory that is not in this repository (staged by the demo
  deploy scripts from another checkout), so they cannot be built from a clean checkout.
* `deploy/Dockerfile.module` and `deploy/docker-compose.yml`: the legacy stand-in fleet demo.
* `deploy/sidekick-devops/Dockerfile`: a development tool image (downloads `kubectl` without a checksum).
* `deploy/k8s/*`: an example Service for a local LLM; no image.

The private `agentic-*` application images and `redevops-runtime-enterprise` are built from the private
repositories and follow their own procedure; they are out of scope here.

## 2. Gates (all must hold before anything is pushed)

1. The owner approves this runbook and the names in section 3.
2. The commit to be released is a merge commit on the protected branch, with a green CI run.
3. No published or staged artefact claims an unshipped fix (advisory state is reviewed by the owner).
4. The build inputs are the ones qualified locally: `Dockerfile`/`Dockerfile.projects` base pinned by
   digest, non-root user, HEALTHCHECK, no `COPY .` (checked by `oci_prepare.sh` stage `lint`).
5. The vulnerability scan has been read by a person: no unfixed-and-reachable HIGH/CRITICAL finding in
   the application or its Python dependencies; base-OS findings with no upstream fix are recorded as
   residual with a justification (see the rehearsal report for the format).
6. The licence statement shipped in the image is the repository's: `LICENSE`, `LICENSE-COMMONS-CLAUSE`,
   `LICENSE.md`, `LICENSING.md` (the images copy them since the Dockerfile fix). The wording is
   "source-available (family licence: AGPL-3.0-or-later with the Commons Clause)".

## 3. Names, tags, visibility: the private-first (`staged-*`) policy

* First push of any new image goes to a PRIVATE package named `ghcr.io/redevops-io/staged-<name>`
  (`staged-redevops-runtime`, `staged-redevops-projects`), tag `<version>-<12-char commit>`. The
  package is NOT linked to a repository: never set `org.opencontainers.image.source` (and do not use
  `docker/metadata-action` defaults, which set it from the GitHub context); linking makes the package
  inherit the repository's access and breaks the "anonymous pull = 401" check.
* A staged package is verified exactly like the existing `staged-*` library packages: anonymous pull
  returns 401, an authenticated pull succeeds, the pulled digest equals the pushed digest, the
  signature and attestations verify (section 5).
* Promotion to the public name (`redevops-runtime`, `redevops-projects`) is a separate, owner-approved
  step: re-tag by digest (`docker buildx imagetools create -t <public>:<version> <staged>@<digest>`), so
  the public package carries the same digest, signature material is copied (cosign copy, below), and
  visibility is changed in the package settings. A public package cannot be made private again once it
  has many downloads (GitHub limit: verify at the time), so treat the switch as irreversible.
* Tags: `<version>` (for example `0.2.4`) and `<version>-<12-char commit>`. Never `latest` and never a
  moving tag in a manifest the project ships (`agentic_os/mission/deployment_conformance.py` already
  treats a mutable tag as a conformance failure). Deployment manifests and the Helm values reference the
  digest: `image: ghcr.io/redevops-io/redevops-runtime@sha256:<index digest>`.
* GHCR offers no tag-immutability switch (verify at the time). The workflow therefore refuses to push
  over an existing tag: `docker buildx imagetools inspect <ref>` must fail before the push.

## 4. Build (GitHub-hosted runner, on a tag push or manual dispatch from the protected branch)

Sketch of `.github/workflows/oci-release.yml` (to be reviewed; actions pinned by commit SHA, not tag):

```yaml
permissions: {contents: read}
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: {contents: read, packages: write, id-token: write, attestations: write}
    steps:
      - uses: actions/checkout@<sha>
      - uses: docker/setup-qemu-action@<sha>
      - uses: docker/setup-buildx-action@<sha>
      - uses: docker/login-action@<sha>            # registry: ghcr.io, password: GITHUB_TOKEN
      - name: Refuse to overwrite an existing tag
        run: "! docker buildx imagetools inspect ghcr.io/redevops-io/${NAME}:${TAG}"
      - id: build
        uses: docker/build-push-action@<sha>
        with:
          context: .
          file: Dockerfile            # Dockerfile.projects for the Projects image
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ghcr.io/redevops-io/${{ env.NAME }}:${{ env.TAG }}
          build-args: SOURCE_DATE_EPOCH=${{ env.SDE }}      # commit time of the build inputs
          outputs: type=image,rewrite-timestamp=true
          provenance: mode=max
          sbom: true
```

`SDE` is the commit time of the build inputs (`git log -1 --format=%ct -- Dockerfile Dockerfile.projects
pyproject.toml agentic_os deploy/assets modules.yaml README.md LICENSE LICENSE-COMMONS-CLAUSE LICENSE.md
LICENSING.md`). With `SOURCE_DATE_EPOCH` and
`rewrite-timestamp=true` the per-architecture image digests are identical across clean rebuilds (shown
locally for both images, section 10). The index digest is NOT stable across builds, because the
BuildKit provenance attestation records a build id and timestamps; the released identity is therefore
the signed index digest, and a rebuild is compared by per-architecture digest.

Do not use the BuildKit `provenance: mode=max` output as a public document without reading it: it embeds
the Dockerfile text, build arguments and the repository URL and revision. For a `staged-*` image built
from a private repository, the attestation is only readable by people who can pull the package; it must
be re-read before the package is made public (the 2026-10-14 exposure audit found early attestations
naming a private repository).

## 5. Signing and attestation

Two options. The owner picks one per package class; both sign BY DIGEST, never by tag.

### Option A: keyless (GitHub Actions OIDC identity)

* Signer identity = the workflow file and ref. No key to store, rotate or lose.
* Cost: Fulcio and the public Rekor log record the workflow identity, the repository name and the image
  digest. For an image whose repository is private this makes the repository and workflow names public.
  Use option A only for public packages built from the public repository.
* Steps in the same job, after the build:

```bash
DIGEST=ghcr.io/redevops-io/redevops-runtime@${{ steps.build.outputs.digest }}
cosign sign --yes --recursive "$DIGEST"                       # index and each per-arch manifest
cosign attest --yes --type cyclonedx        --predicate sbom.cdx.json    "$DIGEST@<arch digest>"
cosign attest --yes --type slsaprovenance1  --predicate provenance.json  "$DIGEST@<arch digest>"
```

* Consumers verify against the identity, not a key:

```bash
cosign verify ghcr.io/redevops-io/redevops-runtime@sha256:<index digest> \
  --certificate-identity-regexp '^https://github.com/redevops-io/agentic-os/\.github/workflows/oci-release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

### Option B: managed key (KMS or HSM-backed, never a file in the repository)

* Use for `staged-*` (private) packages and whenever the repository is private: with a signing config
  that has no transparency-log service nothing about the repository reaches a public log (this is what
  the local rehearsal does with a throwaway key).
* Key: a dedicated cosign key in a managed service the owner already operates (for example
  `azurekms://<vault>.vault.azure.net/<key>`, `hashivault://<key>`), used only for image signing. Never
  reuse the release-manifest signing key, an SSH key, the GPG key or a Developer ID/Authenticode
  credential. The public key (and its fingerprint) is published through a channel other than the
  registry (the repository and the website) before the first release that uses it.
* Steps (cosign 3 requires an explicit signing config to sign with a key and no transparency log; create
  it once with `cosign signing-config create --out signing-config.json`):

```bash
cosign sign   --key azurekms://<vault>.vault.azure.net/<key> --signing-config signing-config.json --yes --recursive "$DIGEST"
cosign attest --key azurekms://<vault>.vault.azure.net/<key> --signing-config signing-config.json --yes \
  --type cyclonedx --predicate sbom.cdx.json "$DIGEST_PER_ARCH"
cosign attest --key azurekms://<vault>.vault.azure.net/<key> --signing-config signing-config.json --yes \
  --type slsaprovenance1 --predicate provenance.json "$DIGEST_PER_ARCH"
```

* Consumers verify with the published public key:

```bash
cosign verify --key cosign.pub --insecure-ignore-tlog ghcr.io/redevops-io/<name>@sha256:<index digest>
cosign verify-attestation --key cosign.pub --type cyclonedx       --insecure-ignore-tlog <name>@sha256:<arch digest>
cosign verify-attestation --key cosign.pub --type slsaprovenance1 --insecure-ignore-tlog <name>@sha256:<arch digest>
```

  (`--insecure-ignore-tlog` is correct here only because no transparency-log entry was made; say so in the
  release notes. If a transparency log is wanted, create the signing config with the log service and drop
  the flag.)

### SBOM and provenance content

* SBOM attestation: CycloneDX from `syft` (a tool independent of BuildKit). The BuildKit SPDX SBOM stays
  attached by BuildKit; the two are compared in the rehearsal (package counts agree, section 10).
* Provenance attestation: the BuildKit SLSA v1 predicate, re-signed with `cosign attest` so it is a
  signed statement whose subject is the per-architecture digest (checked in the rehearsal).

## 6. GitHub artifact attestations (`gh attestation`)

For packages built in the public repository, add GitHub's own provenance as a second, independent
statement:

```yaml
- uses: actions/attest-build-provenance@<sha>
  with:
    subject-name: ghcr.io/redevops-io/redevops-runtime
    subject-digest: ${{ steps.build.outputs.digest }}
    push-to-registry: true
```

Consumers verify with the GitHub CLI:

```bash
gh attestation verify oci://ghcr.io/redevops-io/redevops-runtime@sha256:<index digest> --owner redevops-io \
  --signer-workflow redevops-io/agentic-os/.github/workflows/oci-release.yml
```

Availability for a private repository depends on the GitHub plan (verify before relying on it); for
`staged-*` packages use option B only.

## 7. Retention and clean-up

* Keep, for the support window the owner sets: every released index digest, its per-architecture
  manifests, the cosign signature and attestation artefacts, and the release record (digests, commit,
  tool versions). cosign stores signatures and attestations as extra package versions (tags of the form
  `sha256-<hex>` or `sha256-<hex>.sig` / `.att`, or referrers); GHCR does not implement the OCI
  referrers API as far as this project has verified, so the tag form is used. They are not referenced by
  the image index and look like clutter to a clean-up tool.
* Never run an automated "delete untagged versions" job on these packages unless it is known to
  understand multi-architecture indexes and cosign tags: the per-architecture manifests and BuildKit
  attestation manifests are untagged package versions and are referenced only by the index.
* `staged-*` snapshots are not deleted or re-tagged (owner decision of 2026-11-02); a superseded
  snapshot stays, and the record names the current one.

## 8. Consumer instructions (what the release notes carry)

1. Resolve the digest from the release record, not from a tag.
2. `cosign verify` (and `verify-attestation` for the SBOM and provenance) with the identity or key in
   section 5 BEFORE the first pull; refuse on any failure.
3. Reference the image by digest in compose, Helm values and Kubernetes manifests. Examples (verified by
   the rehearsal, pointing at the local registry): `consumer/compose.yaml` and `consumer/k8s.yaml` in the
   rehearsal output. An admission policy (Sigstore policy-controller or Kyverno `verifyImages`) can
   enforce the same check in a cluster.
4. Runtime hardening that was exercised: `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, user
   10001. Writable paths of the control plane: `/data` (permissions key and grants; set
   `AGENTIC_OS_HOME=/data`) and `/app/.agentic-os` (fleet workdir, ephemeral). The Projects image needs a
   writable home (`/home/app`) and, when the container port is mapped to a different host port,
   `PROJECTS_ALLOWED_HOSTS` set to the exact `Host` value (see `docs/LOCAL_API_SECURITY.md`, L6).
5. Control-plane credential: the `redevops-runtime` image fails closed. With no `AGENTIC_OS_CONTROL_TOKEN` (or legacy
   `AGENTIC_OS_API_KEY`) the mutating routes (`POST /missions`, `/missions/{id}/approve`, `/up`, `/down`, `/dispatch`,
   `/approvals/*`, `/api/permissions/*`) and the mission reads answer 503, while `/health` (the HEALTHCHECK and the
   Helm probes) stays open. Supply the token at run time (compose `environment`, Kubernetes Secret via
   `runtime.controlTokenSecret`), never in the image. Details: `docs/CONTROL_PLANE_SECURITY.md`.
6. Network exposure: publish on loopback unless you have a reason not to. Compose uses
   `"${AOS_BIND_ADDR:-127.0.0.1}:8080:8080"`, `docker run` examples use `-p 127.0.0.1:8080:8080` and
   `-p 127.0.0.1:8787:8787`. WARNING: `AOS_BIND_ADDR=0.0.0.0` (or a `-p 8080:8080` without an address) exposes the control
   plane to every network the host is on; do it only with the token set, behind your own TLS reverse proxy and a host
   firewall rule (`docs/CONTROL_PLANE_SECURITY.md`, "Network exposure"). The `redevops-projects` image has its own
   launch-code/session gate (the one-time launch URL is printed on the container's stderr), but it is a single-user
   desktop-style gate, not multi-user authentication; do not publish it beyond a trusted network. A deployment that has its own authentication in front (origin/main's hosted mode) opts out of that gate at run time with `-e AGENTIC_OS_LOCAL_CAPABILITY=off`; the image never ships with it.
7. Permission-grant reads (`GET /api/permissions/grants`, `/status`, `POST /api/permissions/preview`, `/authorize`) use
   the grant-administration credential; a valid credential that this route does not accept is a 403, an unknown one a 401.
8. The Evidence Explorer demo (`deploy/evidence-explorer-demo`) is NOT part of either release image and is not an image
   entrypoint. Its authenticated profile is a deployment recipe only. Its public synthetic profile
   (`evidence-demo.public.compose.yml`, `public_app.py`) has NOT been approved for deployment. If it is ever deployed, all of
   these must hold first: a host with no bind mounts of host paths, no Docker socket and no credentials of any kind in the
   container or its environment; an egress policy that denies all outbound traffic from the container; and
   `EVIDENCE_REQUIRE_OS_SANDBOX=1` so the query worker refuses to run without an OS sandbox. See
   `docs/EVIDENCE_REPLAY_BOUNDARY.md`. The wheel inside the images contains `agentic_os/evidence_query`, which is dormant:
   the images do not install `duckdb` (optional extra, pinned `==1.5.5`), nothing in the control plane or the Projects
   server imports or starts it, and only the mission-runtime replay gate (a rehydrated mission cannot run without a resume
   grant) ships active.
9. Upgrading from an earlier image that ran as root: an existing root-owned `/data` volume must be
   `chown`ed to uid/gid 10001 once.

## 8a. Earlier published tags: which are affected, and what consumers should do

Inventory read-only from the GHCR package listing (nothing pulled). Source revisions are inferred from package creation
times and git history, not from build metadata; confidence is stated per tag. All three public tags and both private
enterprise tags carry the control plane that ran OPEN by default.

| Package : tag | Index digest | Published (UTC) | Source revision (inferred) | Confidence | Open default present |
|---|---|---|---|---|---|
| `redevops-runtime:v6` | `sha256:cabbdd8d...` | 2026-08-05 18:43 | `main` at 531e9957 (2026-08-04) or branch `v6` (2ba565af, 2026-07-15) | low-medium | yes |
| `redevops-runtime:0.3.0` | `sha256:a134be71...` | 2026-08-25 00:52 | 63d9824d (2026-08-24 20:50 EDT, "build fixes for the 0.3.0 image", two minutes before) | high | yes |
| `redevops-runtime:0.3.1` | `sha256:9d0315d8...` | 2026-09-13 23:13 | git tag `v0.3.1` (73e33735) per the chart commit 8bed4739 ("rebuilt at the v0.3.1 release"); could be `main` of that day (2ea6a633) | medium | yes |
| `redevops-runtime-enterprise:0.3.0` / `0.3.1` (private) | `sha256:6519f506...` / `sha256:9e9ac348...` | 2026-08-25 00:52 / 2026-09-13 23:31 | same revisions as the public tags, plus private plugins | medium | yes (same control plane) |

"Open default" means: with no `AGENTIC_OS_API_KEY` set the mutating routes (`/up`, `/down`, `/dispatch`, `/approvals/*`,
`POST /api/permissions/grants`) and every `/missions` route are unauthenticated, the grant list is readable, the
module proxy accepted `..%2f` tails, and the image runs as root and listens on 0.0.0.0:8080. The control plane became
fail-closed upstream in v0.3.28 (routes) and v0.3.29 (mission API); every earlier tag is affected.

Consumer guidance (text for the release notes and the deprecation notice):

> Images published before the fail-closed release (`redevops-runtime` `v6`, `0.3.0`, `0.3.1`, and the matching `redevops-runtime-enterprise` tags, which have not been analysed) run the control plane without authentication and as root, and the bundled compose files publish port 8080 on all interfaces. Measured on the three public digests (amd64, default configuration): `POST /missions`, mission approval, permission-grant writes and every read answer 200 without credentials. Setting `AGENTIC_OS_API_KEY` is only a PARTIAL mitigation: it protects `POST`/`DELETE /api/permissions/grants` and `POST /approvals/{id}/{decision}` (header `X-API-Key`; a Bearer token is rejected), while mission creation and approval, `/simulate`, every GET, `POST /api/permissions/authorize` and `/preview` and the module proxy stay open. The only measure that covers every route is network isolation: publish on 127.0.0.1 or a private network only, firewall or NetworkPolicy the port, or put an authenticating reverse proxy in front. Do not expose these images to any network you do not fully control, review the missions, approvals and grants they hold, and move to the replacement image once it is published, which refuses requests until `AGENTIC_OS_CONTROL_TOKEN` is set. The old tags are deprecated and will not be updated. (Images are identified by digest: the `0.3.0` image is not the git tag `v0.3.0`.)

## 9. Rollback and revocation

* A bad release is withdrawn by publishing a new signed digest and updating the release record; the old
  digest stays in the registry (evidence) but is marked superseded in the record and in the release notes.
* Key compromise (option B): rotate the key, publish the new public key through the separate channel,
  re-sign the currently supported digests with the new key, state the date from which the old key is not
  trusted. Keyless (option A) has no key to revoke; a compromised workflow is handled by changing the
  expected identity in the verification command.

## 10. Local rehearsal

`scripts/release/oci_prepare.sh` reproduces every non-GitHub step on a workstation with Docker:

* throwaway `registry:2` bound to `127.0.0.1` and a throwaway `docker-container` buildx builder; both are
  removed at the end; nothing is pushed anywhere else;
* both images for `linux/amd64` and `linux/arm64`, BuildKit provenance `mode=max` and SBOM, a clean
  rebuild and a digest comparison;
* BuildKit SBOM compared to a separate `syft` SBOM; `grype` and `trivy` scans per architecture digest;
  layer, history and configuration secret scan;
* a THROWAWAY cosign key (deleted at the end): sign by digest, attest SBOM and provenance, then the
  consumer flow (`cosign verify`, `cosign verify-attestation`, `docker buildx imagetools inspect`,
  digest-pinned compose) and negative tests (changed digest, wrong key, unsigned image, tampered image
  under a followed tag, attestation type that was never attached) that must all fail;
* every image started on both architectures with synthetic configuration, loopback ports only, and
  hardened flags; the HEALTHCHECK must report `healthy`.

It re-executes itself under `env -i` with an empty Docker configuration, so no credential reaches a
build or a signing step. Limitations of the rehearsal: `registry:2` has no OCI referrers API and no TLS;
the amd64 images run under emulation on an arm64 host; the GitHub OIDC, `gh attestation` and GHCR
visibility steps are not exercised; the base image and Debian/PyPI package versions move over time, so a
rebuild on another day can legitimately produce other digests (the pinned base and `SOURCE_DATE_EPOCH`
remove the rest of the variation).

## 11. Decisions the owner has to make

1. Approve the release set and the public image names (`redevops-runtime`, `redevops-projects`). The
   version in `pyproject.toml` is 0.2.4, while the published `redevops-runtime` tags and the Helm chart
   `appVersion` are in the 0.3.x line: choose the version number the first image carries.
2. Signing option per package class (keyless for public, managed key for staged/private is proposed) and
   which managed key service holds the cosign key.
3. Whether the base-OS findings that have no upstream fix are acceptable as residual, or whether a
   different base (for example a distroless or hardened-vendor Python image) should be evaluated.
4. Retention window for released digests and their signatures.
5. Whether to link the public packages to the repository (`org.opencontainers.image.source`); staged
   packages stay unlinked.
