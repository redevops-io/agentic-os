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
EXPOSE 8080
CMD ["uvicorn", "agentic_os.control_plane:app", "--host", "0.0.0.0", "--port", "8080"]
