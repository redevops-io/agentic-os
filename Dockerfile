# Control plane image (agentic_os.control_plane on :8080).
# Base pinned by the index digest (python 3.12-slim-trixie, multi-arch: linux/amd64 + linux/arm64).
# Bump deliberately: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
WORKDIR /app
# Reproducible builds: pass --build-arg SOURCE_DATE_EPOCH=<unix time> (and --output ...,rewrite-timestamp=true).
# Python then writes checked-hash .pyc files instead of mtime-stamped ones; unset, nothing changes.
ARG SOURCE_DATE_EPOCH
# Only what the wheel build needs, not the whole build context: a local .env, key or evidence file in the checkout must
# not be able to land in an image layer. hatch_build.py is the wheel build hook named in pyproject.toml.
COPY pyproject.toml README.md modules.yaml hatch_build.py LICENSE LICENSE-COMMONS-CLAUSE LICENSE.md ./
COPY agentic_os ./agentic_os
COPY deploy/assets ./deploy/assets
# The hook fetches the Projects UI bundle from a release, which the control plane does not serve; skip it so the build
# needs no download besides the pinned git dependencies (set for this RUN only). (Dockerfile.projects keeps the fetch.)
# git is needed to resolve the git-pinned dependencies; install it for the build and purge it in the same layer so the
# runtime image ships without it. PIP_NO_CACHE_DIR also reaches the isolated build-environment pip.
# The import check fails THIS build if the contract package, or the first module that needs it, cannot be imported
# (run from / so the working directory cannot shadow the installed package).
# pip, setuptools and wheel are removed last: the image installs nothing at run time and ships no package manager.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && AGENTIC_OS_SKIP_UI_FETCH=1 PIP_NO_CACHE_DIR=1 pip install --no-cache-dir . \
 && (cd / && python -c "import runtime_contracts, agentic_os.sources") \
 && apt-get purge -y git && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /var/log/apt /var/log/dpkg.log /var/log/alternatives.log /var/cache/ldconfig/aux-cache \
 && pip uninstall -y pip setuptools wheel
# Run unprivileged. The control plane writes only under /data (permissions plane, AGENTIC_OS_HOME) and
# /app/.agentic-os (the Context home when AGENTIC_OS_HOME is unset); both are created owned by the service user so a
# fresh named volume mounted at /data inherits that ownership. /app stays root-owned and read-only to the service user.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /app/.agentic-os && chown app:app /data /app/.agentic-os
USER 10001:10001
EXPOSE 8080
# Same probe the Helm chart uses (GET /health on the http port); python is already in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
# Image metadata (set by scripts/release/oci_prepare.sh from the release tag and the source commit; empty when built by hand).
ARG OCI_VERSION
ARG OCI_REVISION
LABEL org.opencontainers.image.title="redevops-runtime" \
      org.opencontainers.image.description="ReDevOps runtime control plane" \
      org.opencontainers.image.version="$OCI_VERSION" \
      org.opencontainers.image.revision="$OCI_REVISION" \
      org.opencontainers.image.vendor="ReDevOps"
CMD ["uvicorn", "agentic_os.control_plane:app", "--host", "0.0.0.0", "--port", "8080"]
