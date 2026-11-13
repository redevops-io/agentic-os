# Residual findings in the published v0.6.1 images

Status record for the findings that the published container images carry and how they are tracked. Purpose and
procedure only; the release notes of `v0.6.1` link here for the container-images section.

Images: `ghcr.io/redevops-io/redevops-runtime:0.6.1` (index `sha256:9d5cb6aef88a136eae8d0bfac3b92ca9edc06c972026ceb07d0e0a6abcacf32e`)
and `ghcr.io/redevops-io/redevops-projects:0.6.1` (index `sha256:616633b471afb3fe222f2766f9f7cdebed9b9c4c5174bd0b9b8e1a68d356f971`),
amd64 and arm64, built by `.github/workflows/oci-release.yml` from tag `v0.6.1`. Published digests and tags are never changed;
an updated image gets a new tag.

## R-1: zlib1g (two HIGH findings with a Debian fix)

| Field | Value |
|---|---|
| Package | `zlib1g`, source package `zlib` (Debian 13 "trixie") |
| Installed in the images | `1:1.3.dfsg+really1.3.1-1+b1` |
| Fixed in Debian | `1:1.3.dfsg+really1.3.1-1+deb13u1` (trixie-security, DSA-6613-1, announced 2026-11-07) |
| CVEs | CVE-2026-76844 (heap buffer overflow in `gz_write()` after a failed `write()`), CVE-2026-85091 (heap buffer overflow in `gz_vacate()` with non-blocking `gzwrite()`); both HIGH in Trivy; both are in the `gz*` file API |
| Pinned base | `python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54` |
| Current base index (checked 2026-11-13) | `python:3.12-slim` = `sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b` (image created 2026-11-05, Python 3.12.16, on `debian:trixie-slim` `sha256:7378260c...`); still `1:1.3.dfsg+really1.3.1-1+b1`, so re-pinning does not help |
| Debian base | `debian:trixie-slim` newest dated tag `trixie-20261104-slim` (before the DSA); still `+b1` |
| Status | open, waiting for a patched base image |
| Owner | Alex |
| Next action | run the re-check below; when `python:3.12-slim` ships `1:1.3.dfsg+really1.3.1-1+deb13u1` or later, decide whether to build a new tag (`0.6.2` or the next release); not before |
| Public statement | the v0.6.1 release notes: "We will evaluate and publish updated images when a suitable patched base becomes available." No date is promised |

Evidence of the 2026-11-13 re-check: Debian security tracker pages for `zlib`, CVE-2026-76844, CVE-2026-85091 and DSA-6613-1;
`docker-library/python` and Docker Hub tag metadata (`python:3.12-slim` last pushed 2026-11-05); Trivy 0.76.2 on both
base digests and on the four published image/architecture pairs (0 CRITICAL, 49 HIGH per image of which 2 have a fix: the two
zlib1g findings).

### Re-check procedure

```bash
# 1. Debian: is the fixed version still the newest in trixie?
curl -s https://security-tracker.debian.org/tracker/source-package/zlib | sed 's/<[^>]*>//g' | grep -E 'trixie|CVE-2026-(76844|85091)'

# 2. What does the current base carry?
docker buildx imagetools inspect python:3.12-slim | sed -n 1,4p
docker run --rm python:3.12-slim dpkg-query -W -f '${Package} ${Version}\n' zlib1g
dpkg --compare-versions "$(docker run --rm python:3.12-slim dpkg-query -W -f '${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' && echo "PATCHED BASE AVAILABLE"

# 3. Scanner view of the base (DIGEST = the index digest printed by step 2)
DIGEST=sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
trivy image --scanners vuln --severity HIGH,CRITICAL "python:3.12-slim@$DIGEST" | grep -A3 zlib1g
```

The workflow `.github/workflows/base-image-zlib-check.yml` on `main` runs step 2 every Monday. It reports only: it never builds,
tags or publishes. It stays green while the base is unpatched and fails (which notifies the maintainers) when the base carries the
fixed version.

### When a patched base is available

Record it here (digest, date) first. Rebuilding is a separate decision: it needs a new tag (never a retag of `0.6.1`), the
`oci-release.yml` dispatch from a ref that carries the new base digest, and the same verification as `0.6.1` (cosign by digest,
`gh attestation verify`, a smoke run, a Trivy comparison). Pin the base by digest in `Dockerfile` and `Dockerfile.projects`.

## R-2: HIGH findings without an upstream fix

47 of the 49 HIGH findings per image have no fixed version in Debian 13 (`bsdutils`, `login`, `mount`, `util-linux`,
`libblkid1`, `libmount1`, `libsmartcols1`, `libuuid1`, `liblastlog2-2`, `libsqlite3-0`, `libacl1`, `libncursesw6`, `libtinfo6`,
`ncurses-base`, `ncurses-bin`, `libsystemd0`, `libudev1`, `perl-base`). They are the same in both images and both architectures
and are carried by the base. Nothing can be applied until Debian ships fixes; they are re-evaluated together with R-1.

## Change log

| Date | Change |
|---|---|
| 2026-11-13 | Record created; v0.6.1 release notes updated to the evaluate-and-publish wording; base checked, still unpatched |
