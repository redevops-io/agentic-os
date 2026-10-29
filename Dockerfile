FROM python:3.12-slim
WORKDIR /app
# hatch_build.py is the wheel build hook named in pyproject.toml; the build stops without it.
COPY pyproject.toml README.md modules.yaml hatch_build.py ./
COPY agentic_os ./agentic_os
COPY deploy/assets ./deploy/assets
# The hook fetches the Projects UI bundle from a release, which the control plane does not serve; skip it so
# the build needs no download besides the pinned git dependencies (set for this RUN only). (Dockerfile.projects keeps the fetch.)
# git is needed to resolve the git-pinned runtime-contracts dependency; install it for the build and
# purge it in the same layer so the runtime image ships without it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && AGENTIC_OS_SKIP_UI_FETCH=1 pip install --no-cache-dir . \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
EXPOSE 8080
CMD ["uvicorn", "agentic_os.control_plane:app", "--host", "0.0.0.0", "--port", "8080"]
