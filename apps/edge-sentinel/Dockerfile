# edge-sentinel — FastAPI agent layer + MD3 SOC dashboard over a real CrowdSec core.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

# docker CLI (static binary) so `cscli` (alerts/bans) can be exec'd into the CrowdSec
# container via the mounted /var/run/docker.sock. Decisions/health are read purely over
# the LAPI and need none of this — alerts just degrade gracefully if the socket is absent.
ARG DOCKER_CLI_VERSION=27.3.1
ARG TRIVY_VERSION=0.75.0
ARG TRIVY_SHA256=c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f
ARG SYFT_VERSION=1.54.1
ARG SYFT_SHA256=c069905b391cc4c20a5ba65ad5c10be2a7ba074f8ea6ad203e24d14e303dad47
ARG COSIGN_VERSION=3.1.3
ARG COSIGN_SHA256=4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    # Supply-chain inspection (Phase 2): Trivy (CVEs/secrets/IaC/SBOM) + Syft (SBOM) + Cosign. The
    # SupplyChainScanner shells out to these; Trivy's vuln DB downloads on first scan (cached in-container).
    # PINNED release artifacts, each verified against a sha256 recorded HERE (no `curl | sh` from a moving
    # `main`, no `latest`). To bump a tool: change the VERSION and SHA256 ARGs together (sha256 from the
    # release's *_checksums.txt) -- a mismatch fails the build.
    && curl -fsSL -o /tmp/trivy.tgz "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
    && echo "${TRIVY_SHA256}  /tmp/trivy.tgz" | sha256sum -c - \
    && tar -xzf /tmp/trivy.tgz -C /usr/local/bin trivy \
    && curl -fsSL -o /tmp/syft.tgz "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_linux_amd64.tar.gz" \
    && echo "${SYFT_SHA256}  /tmp/syft.tgz" | sha256sum -c - \
    && tar -xzf /tmp/syft.tgz -C /usr/local/bin syft \
    # Cosign -- sign SBOMs/images (the Lightwell "attest at the pinned version" step)
    && curl -fsSL -o /usr/local/bin/cosign "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-amd64" \
    && echo "${COSIGN_SHA256}  /usr/local/bin/cosign" | sha256sum -c - \
    && chmod +x /usr/local/bin/cosign \
    && rm -f /tmp/trivy.tgz /tmp/syft.tgz \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The agentic-os KERNEL is pinned HERE (single source, overridable) rather than in requirements.txt, so
# the image tracks a known kernel tag instead of a stale commit. Override at build time, e.g.
#   docker build --build-arg KERNEL_REF=main .   (or a newer release tag)
ARG KERNEL_REF=v0.3.36
RUN pip install --no-cache-dir "agentic-os @ https://github.com/redevops-io/agentic-os/archive/${KERNEL_REF}.tar.gz"

# Copy ALL python modules into an importable PACKAGE. app.py uses package-relative imports
# (`from . import core`, live, auth, netbird, portabase, deploy_scan, permissions_ui, operator, …),
# so a flat `app:app` cannot resolve them; and the source dir name (apps/edge-sentinel) has a
# hyphen, which is not a valid module name. Re-home the modules under a valid package name.
COPY *.py ./edge_sentinel/
RUN touch edge_sentinel/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   CROWDSEC_LAPI_URL, CROWDSEC_BOUNCER_KEY, CROWDSEC_CONTAINER, CROWDSEC_FRONT_URL
# Notes for containerized runs:
#   * CROWDSEC_LAPI_URL should point at the CrowdSec service on the shared docker
#     network (e.g. http://crowdsec:8080), not localhost.
#   * The `triage`/`approve_block`/alerts path shells out to `cscli` via the docker
#     socket; to use it from a container, mount /var/run/docker.sock. Decisions are
#     read purely over the LAPI bouncer key and need no socket.
# Run as an unprivileged user; /app and /data are writable by it (state lives under $HOME or /data).
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir -p /data && chown -R app:app /app /data
RUN printf '#!/bin/sh\n[ -w /data ] || { echo "/data is not writable by uid 10001; see apps/README.md (Upgrading images)" >&2; exit 1; }\nexec "$@"\n' > /usr/local/bin/aos-entrypoint \
 && chmod 755 /usr/local/bin/aos-entrypoint
ENTRYPOINT ["aos-entrypoint"]
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)"
ENV PORT=8203 PYTHONPATH=/app
EXPOSE 8203

CMD ["uvicorn", "edge_sentinel.app:app", "--host", "0.0.0.0", "--port", "8203"]
