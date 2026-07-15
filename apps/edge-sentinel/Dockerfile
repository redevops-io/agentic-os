# edge-sentinel — FastAPI agent layer + MD3 SOC dashboard over a real CrowdSec core.
FROM python:3.12-slim

WORKDIR /app

# docker CLI (static binary) so `cscli` (alerts/bans) can be exec'd into the CrowdSec
# container via the mounted /var/run/docker.sock. Decisions/health are read purely over
# the LAPI and need none of this — alerts just degrade gracefully if the socket is absent.
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker \
    # Supply-chain inspection (Phase 2): Trivy (CVEs/secrets/IaC/SBOM) + Syft (SBOM). The
    # SupplyChainScanner shells out to these; Trivy's vuln DB downloads on first scan (cached in-container).
    && curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin \
    && curl -sfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin \
    # Cosign — sign SBOMs/images (the Lightwell "attest at the pinned version" step)
    && curl -sfL https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64 -o /usr/local/bin/cosign \
    && chmod +x /usr/local/bin/cosign \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   CROWDSEC_LAPI_URL, CROWDSEC_BOUNCER_KEY, CROWDSEC_CONTAINER, CROWDSEC_FRONT_URL
# Notes for containerized runs:
#   * CROWDSEC_LAPI_URL should point at the CrowdSec service on the shared docker
#     network (e.g. http://crowdsec:8080), not localhost.
#   * The `triage`/`approve_block`/alerts path shells out to `cscli` via the docker
#     socket; to use it from a container, mount /var/run/docker.sock. Decisions are
#     read purely over the LAPI bouncer key and need no socket.
ENV PORT=8203
EXPOSE 8203

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8203"]
