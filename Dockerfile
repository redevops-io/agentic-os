# Base pinned by the index digest (python 3.12.16-slim-trixie, multi-arch: linux/amd64 + linux/arm64).
# Bump deliberately: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
WORKDIR /app
COPY pyproject.toml README.md modules.yaml LICENSE LICENSE-COMMONS-CLAUSE LICENSE.md LICENSING.md ./
COPY agentic_os ./agentic_os
COPY deploy/assets ./deploy/assets
# git is needed to resolve the git-pinned runtime-contracts dependency; install it for the build and
# purge it in the same layer so the runtime image ships without it. PIP_NO_CACHE_DIR also reaches the
# isolated build-environment pip that --no-cache-dir does not (it left /root/.cache/pip in the image).
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && PIP_NO_CACHE_DIR=1 pip install --no-cache-dir . \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
# Run unprivileged. The control plane writes only under /data (permissions plane) and
# /app/.agentic-os (the Context home when AGENTIC_OS_HOME is unset); both are created owned by
# the service user so a fresh named volume mounted at /data inherits that ownership. An existing
# root-owned /data volume from an earlier image must be chown'd to 10001 once before upgrading.
RUN useradd --system --uid 10001 --user-group --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /app/.agentic-os && chown app:app /data /app/.agentic-os
USER 10001:10001
EXPOSE 8080
# Same probe the Helm chart uses (GET /health on the http port); python is already in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
CMD ["uvicorn", "agentic_os.control_plane:app", "--host", "0.0.0.0", "--port", "8080"]
