FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md modules.yaml ./
COPY agentic_os ./agentic_os
COPY deploy/assets ./deploy/assets
# git is needed to resolve the git-pinned runtime-contracts dependency; install it for the build and
# purge it in the same layer so the runtime image ships without it.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir . \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
EXPOSE 8080
CMD ["uvicorn", "agentic_os.control_plane:app", "--host", "0.0.0.0", "--port", "8080"]
