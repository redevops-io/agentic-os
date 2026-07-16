# Sidekick for DevOps — build from the repo ROOT:
#   docker build -f deploy/sidekick-devops/Dockerfile -t sidekick-devops .
# (docker-compose.yml sets the context to the repo root for you.)
FROM python:3.12-slim
WORKDIR /app

# kubectl for read-only cluster inspection (compose mode reads a mounted kubeconfig)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -sLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.31.0/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Pin fastapi/starlette to a compatible pair (a mismatched starlette breaks include_router).
RUN pip install --no-cache-dir "fastapi==0.115.6" "uvicorn==0.32.1" "pydantic>=2.6" pyyaml "httpx>=0.27"

# the kernel + the infra operator + the bundle server/modules/skills
COPY agentic_os /app/agentic_os
COPY apps/infra /app/apps/infra
COPY deploy/sidekick-devops/sidekick_server.py deploy/sidekick-devops/mcp_reads.py deploy/sidekick-devops/monitor.py deploy/sidekick-devops/notify.py /app/
COPY deploy/sidekick-devops/skills /app/skills

# non-root (uvicorn binds unprivileged :8000); a home dir for the mounted kubeconfig
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app && chown -R app:app /app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "sidekick_server:app", "--host", "0.0.0.0", "--port", "8000"]
