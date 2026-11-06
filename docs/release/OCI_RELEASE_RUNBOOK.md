# OCI release runbook: `redevops-runtime` and `redevops-projects`

Purpose and procedure. Licence wording: source-available (family licence: AGPL-3.0-or-later with the Commons Clause). Nothing here is a
production-readiness claim. Nothing in this document has been executed against ghcr.io: the local rehearsal (section 9) exercises every step that
does not need GitHub, and each GitHub-side step is marked UNVERIFIED until it has been run once under the owner's account.

## 1. Release set

| Image (`ghcr.io/redevops-io/...`) | Built from | Port | Role |
|---|---|---|---|
| `redevops-runtime` | `Dockerfile` (`agentic_os.control_plane`) | 8080 | the control plane the reference Helm chart (`deploy/reference-system/redevops-runtime`) and the root `docker-compose.yml` use |
| `redevops-projects` | `Dockerfile.projects` (Projects UI + API, one origin) | 8787 | the self-hosted Projects app |

Everything else with a Dockerfile (`apps/*`, `deploy/*`) is a demo or development artefact and is not part of the set. The private
`redevops-runtime-enterprise` images follow their own procedure.

## 2. Gates (all hold before the first registry write)

1. The owner approves this runbook, the names and the version (`pyproject.toml` and the image tag agree: `0.5.0`).
2. The commit released is a merge commit on `main` with the required checks green. The image tag carries its first 12 characters.
3. The images were built from that commit by `scripts/release/oci_prepare.sh` with an explicit `OCI_TAG=0.5.0-<12 hex>` from a clean tree.
4. The scan output was read by a person on the day (section 9) and the residual findings accepted in writing.
5. No release text claims more than the evidence in this repository supports. The security notice that accompanies the release is published with
   the release by the owner (step 10 below); its text is not part of this repository.

## 3. One flow: stage, verify, promote, verify, announce

Names: the first push of any image goes to a PRIVATE package `ghcr.io/redevops-io/staged-<name>` that is never linked to a repository. Tags are
`MAJOR.MINOR.PATCH` and `MAJOR.MINOR.PATCH-<12 hex commit>`; never `latest` or any other moving tag. Consumers reference the INDEX DIGEST. A
published tag is never overwritten (GHCR has no tag immutability; the guards below are the control).

1. **Qualify locally.** `OCI_TAG=0.5.0-<12 hex> OCI_NAME_PREFIX=<prefix> scripts/release/oci_prepare.sh` (section 9). It leaves
   `registry-data.tar.gz` (the local registry with exactly the qualified digests) and `results/DIGESTS.txt` (index and per-architecture digests).
2. **Stage (owner terminal).** Restore the exported registry on loopback and copy ONLY the allow-list to the private staged package:
   the index (by digest) and the manifests it lists (per-architecture images and the BuildKit attestation manifests). Not copied: the `tampered`
   tag, the `<tag>-rebuild` index, and every cosign signature or attestation in the export (they were made with a throwaway key that no longer exists).
   ```bash
   # restore (loopback only), then:  IDX=sha256:<index digest>  SRC=127.0.0.1:<port>/redevops-runtime  DST=ghcr.io/redevops-io/staged-redevops-runtime
   source docs/release/oci-promote-owner.sh.draft   # with PROMOTE_FUNCTIONS_ONLY=1: defines tag_state only
   [ "$(tag_state "$DST:0.5.0-<12 hex>")" = absent ] || { echo refuse; exit 1; }     # 404 = absent; every other answer stops
   [ "$(oras manifest fetch --plain-http "$SRC@$IDX" | shasum -a 256 | awk '{print "sha256:"$1}')" = "$IDX" ]     # equality before
   oras cp -r --from-plain-http "$SRC@$IDX" "$DST:0.5.0-<12 hex>"
   [ "$(oras manifest fetch "$DST@$IDX" | shasum -a 256 | awk '{print "sha256:"$1}')" = "$IDX" ]                   # equality after; repeat per child
   ```
   Check the three things that make a staged package safe: anonymous pull returns 401; an authenticated pull succeeds and the pulled digest equals
   `$IDX`; the package page shows NO connected repository (section 6).
3. **Read the provenance.** The BuildKit provenance and SBOM attestations are part of the index and become public with it: read them first
   (`docker buildx imagetools inspect <ref>@<digest> --format '{{json .Provenance}}'`): they embed the Dockerfile text, build arguments and the
   source revision. They must contain no path, address, host name or credential of the build host.
4. **Prepare the repository side (once).** Create the GitHub environment `release-promotion` with required reviewers; in each package's settings
   under Manage Actions access give this repository `Read` on the `staged-*` packages and `Write` on the public ones (access control, not a
   repository link); restrict who can push tags and branch `main`.
5. **Promote.** Run the workflow `oci-promote` (Actions, `workflow_dispatch`, branch `main`) once per image with the staged index digest, the
   two per-architecture digests and the public tags. A reviewer of `release-promotion` approves the run. The workflow (`.github/workflows/oci-promote.yml`)
   has no build step. It asserts the staged index and each manifest hash to the given digests, refuses if a destination tag exists (registry
   status 404 = absent; any other status, error or timeout stops the run), copies by digest, asserts equality again, signs the index and each image
   by digest keyless, attests the BuildKit SBOM and provenance, verifies against the exact identity below, and checks the package link and labels.
   **Publication happens at the first write to the destination package.** For `redevops-runtime`, which already exists and is public with earlier
   tags, the first promotion creates a new public tag in that package; for `redevops-projects` the package is created by the workflow and is
   private until the owner changes its visibility (irreversible in practice: decide deliberately).
6. **Verify as a consumer, anonymously**, from a machine with no credentials:
   ```bash
   cosign verify ghcr.io/redevops-io/redevops-runtime@sha256:<index digest> \
     --certificate-identity 'https://github.com/redevops-io/agentic-os/.github/workflows/oci-promote.yml@refs/heads/main' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```
   The identity is an exact string: the workflow file AND `refs/heads/main`. Never a pattern, never a `v*` tag ref (anyone able to push a tag
   could then produce a valid signature). Repeat `cosign verify-attestation --type spdxjson` and `--type slsaprovenance1` on each
   per-architecture digest.
7. **Check the package link right after the first write.** The package must not be linked to a repository (`repository` is `null` in
   `GET /orgs/redevops-io/packages/container/<name>`; the workflow checks this; UNVERIFIED on GHCR). Never set the source label (the images
   carry none) and never use `docker/metadata-action` defaults. A push with `GITHUB_TOKEN` may auto-link a package to the repository; if the
   check finds a link, disconnect it in the package settings at once and run the check again.
8. **Chart and compose.** In a follow-up commit set `runtime.image.digest` in the chart to the released index digest and `appVersion` to the
   released version (`docs/release/CONTAINER_ACCEPTANCE_CRITERIA.md`, section 6).
9. **Release notes** carry the required operator configuration (section 7), the verification commands above and the digests.
10. **Security notice.** A security notice is published with the release, by the owner, from the private draft; the order is: fixed image
    public and verified, then the notice. Transition window and retention of earlier tags are the owner's decisions.

### Option B (fallback): promotion from the owner's terminal with a managed key

Use only if the keyless workflow cannot be used. `docs/release/oci-promote-owner.sh.draft` is a DRAFT (not enabled, not executable as shipped).
It runs a dry run by default and writes only with `--execute`. Preconditions: you are logged in to the registry in that terminal; the staged index
is signed and attested with the release key (`cosign sign --key <managed key>` by digest, `cosign attest --type cyclonedx|slsaprovenance1` on each
per-architecture digest, from a signing config without a transparency-log service unless one is wanted), whose public half is published through
a channel other than the registry. It copies only the allow-list (the index, the manifests it lists, and the cosign 3 referrer tags `sha256-<hex of the subject digest>` of
the index and its children), requires exactly 1 sigstore bundle on the index and exactly 3 on each image (its signature and two attestations), so a
signature by a throwaway or unknown key is refused, refuses an existing destination tag (404-only), asserts digest equality for the index and every child before and after
the copy, verifies at the destination, proves that no earlier tag of the destination moved, and checks the package link and the labels.
The key is a dedicated cosign key in a managed service; never reuse the release-manifest signing key, an SSH key, a GPG key or a Developer ID
credential. With a transparency-log-free signature, consumers need `--insecure-ignore-tlog`; say so in the release notes.

## 4. Build inputs

Both Dockerfiles: base `python:3.12-slim` pinned by index digest, uid/gid 10001, `HEALTHCHECK` through python `urllib` (no extra tool), an explicit
`COPY` set (no `COPY .`; `.dockerignore` excludes `.git`, `.env*`, key material), git installed for the build and purged in the same layer, pip,
setuptools and wheel removed in that layer (the image installs nothing at run time), `import runtime_contracts, agentic_os.sources` run in the
build so a broken install fails it, `SOURCE_DATE_EPOCH` for reproducible per-architecture digests (index digests differ between builds because
the BuildKit provenance records a build id), and the labels `org.opencontainers.image.title|description|version|revision|vendor`. The labels
source, url, documentation and licenses are never set. `SOURCE_DATE_EPOCH` is the commit time of the build inputs.
Floating inputs remain: `pip install` resolves `fastapi>=0.110` and similar at build time; the git dependencies are pinned by tag. The BuildKit
provenance of a local build records the revision but is not hosted-builder provenance: no SLSA level is claimed.

## 5. Retention and clean-up

Keep, for the support window the owner sets: every released index digest, its per-architecture manifests, the signature and attestation
artefacts and the release record (digests, commit, tool versions). Never run an automated "delete untagged versions" job on these packages unless
it is known to understand multi-architecture indexes and cosign tags. A superseded `staged-*` snapshot stays; the record names the current one.
A bad release is withdrawn by publishing a new signed digest and updating the release record; the old digest stays and is marked superseded.
A compromised signing path is handled by changing the expected identity (keyless) or rotating the key (managed key) and re-signing the supported
digests.

## 6. Package visibility and the repository link

A staged package is private and unlinked. A public package must not be linked to the repository either, unless the owner decides otherwise: a link
makes the package inherit the repository's access and breaks the anonymous-401 check on staged packages. Verify immediately after the first write
(step 7). UNVERIFIED on GHCR: whether a `GITHUB_TOKEN` push auto-links, and the exact package-settings wording for disconnecting.

## 7. Consumer instructions (release notes)

1. Resolve the digest from the release record, not from a tag; verify signature and attestations before the first pull (step 6).
2. Reference the image by digest in compose, Helm values and Kubernetes manifests.
3. Run with `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, uid 10001.

**`redevops-runtime` (control plane)**

| Setting | Why |
|---|---|
| `PERMISSIONS_KEY` (base64, 16/24/32 bytes; from a secret store) | **Required.** The grant store never starts without it; the container exits at start otherwise. `agentic-os permissions key --env-file permissions.env` creates one |
| `AGENTIC_OS_CONTROL_TOKEN` (long random secret) | Required for every mutating and every non-public route; without it they answer 503 by design. `/health` stays open |
| `PERMISSIONS_ADMIN_KEY` (at least 24 characters) | Required for the `/api/permissions/*` routes (503 without it); separate from the control token |
| `AGENTIC_OS_PROFILE=production` plus `AGENTIC_OS_AUTHORITY_DB`, `AGENTIC_OS_REPLAY_DB` | Without it the process runs as `development`: authority is not enforced, approval replay protection is per process. The start-up log says which |
| `AGENTIC_OS_HOME=/data` on a writable volume; `/app/.agentic-os` writable | The only writable paths; an existing root-owned volume needs a one-time `chown` to 10001 |
| `MISSION_EVENT_BACKEND=duckdb\|postgres` (+ path/DSN) | The mission event store is in memory otherwise; missions do not survive a restart |
| `AGENTIC_OS_RESUME_SERVICE_PRINCIPAL` (optional) | Missions restored at start-up are read-only until authorized; unattended resume exists only with this principal and its `evidence.<scope>.resume` grant (`docs/REPLAY_AUTHORIZATION.md`) |
| Publish on loopback: `127.0.0.1:8080:8080` (compose: `AOS_BIND_ADDR`) | `0.0.0.0` only with the token set, behind a TLS reverse proxy you control and a host firewall rule |

Routes that answer WITHOUT a credential (verified on the image, section 9): `/health`, `/`, `/overview`, `/modules`, `/permissions`
(static shell), `/capabilities`, `/m/{name}` and `/m/{name}/raw`, and the module reverse proxy `GET|POST /m/{name}/api/{path}` (path-validated, forwards only
under the module's `/api/`; it is not token-gated). Everything else answers 401/403/503. **Deployment guidance: network isolation.** Treat the
port as internal: publish on loopback or a private network, restrict it with a firewall or Kubernetes NetworkPolicy (the chart's default), or put an
authenticating reverse proxy in front. Route semantics are unchanged in this release.

**`redevops-projects`** is a single-user application for loopback or a trusted network: it has no login. Publish with
`-p 127.0.0.1:8787:8787`. Folder sources need `PROJECTS_SOURCE_ROOTS` (mount the folders read-only and list them) or the control token. State is
kept under the service user's home (`/home/app`, writable). Hosted OAuth connect needs `AGENTIC_OS_CONTROL_TOKEN` on a client that sends it.

## 8. What the rehearsal does not cover (UNVERIFIED)

GHCR behaviour (anonymous 401, `GITHUB_TOKEN` auto-link, package settings), the keyless signing and its transparency-log entry, the workflow run
itself (no runner here), the exact error text of `oras` against GHCR for a missing tag (the guard accepts only the registry's 404 text and treats
everything else as an error), real amd64 hardware (amd64 runs under emulation on the build host), and the chart on a real cluster.

## 9. Local rehearsal: `scripts/release/oci_prepare.sh`

Reproduces every non-GitHub step on a workstation with Docker: a throwaway `registry:2` on `127.0.0.1` and a throwaway buildx builder (names
prefixed by `OCI_NAME_PREFIX`, removed at the end); both images for `linux/amd64` and `linux/arm64` with BuildKit provenance and SBOM; a clean rebuild
and digest comparison; BuildKit SBOM against a separate `syft` SBOM; `grype` and `trivy` per architecture digest; a layer scan of every file of every
layer for private identifiers (addresses, host aliases, home paths, personal mail) and for `.git`, `.env` and key files; image configuration checks
(user, healthcheck, exact labels, none of source/url/documentation/licenses); a THROWAWAY cosign key (deleted at the end) that signs by digest and
attests, the consumer flow and the negative tests (changed digest, wrong key, unsigned image, tampered image under a followed tag, an attestation type
that was never attached); every image started on both architectures with hardened flags. It re-executes itself under `env -i` with an empty Docker
configuration, so no credential reaches a build or signing step, and it refuses to run from a dirty tree or with a tag whose commit part is not the
commit being built. Limitations: `registry:2` has no referrers API and no TLS; amd64 runs under emulation on an arm64 host; base image and package
versions move over time, so a rebuild on another day can produce other digests.

## 10. Decisions the owner makes

1. Approve the release set, the names and the version `0.5.0`.
2. Accept the residual base-OS findings that have no upstream fix, or evaluate another base image.
3. Create the `release-promotion` environment and the package access described in step 4, and decide the visibility of `redevops-projects`.
4. Retention window for released digests and their signatures.
5. Whether the public packages are ever linked to the repository (staged packages stay unlinked).
