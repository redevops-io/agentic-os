# Polished "Try ReDevOps" image — the Projects control plane, sandboxed, cross-platform.
# Build from the REPO ROOT:
#   docker build -f deploy/try/Dockerfile -t redevops-projects-try .
# Run (hosted LLM — bring your key):
#   docker run --rm -p 8787:8787 -e LLM_API_KEY=sk-... redevops-projects-try
# Run (local/self-hosted model — no key; Ollama on the host):
#   docker run --rm -p 8787:8787 -e LLM_BASE_URL=http://host.docker.internal:11434/v1 \
#     --add-host=host.docker.internal:host-gateway redevops-projects-try   # (--add-host: Linux only)
# Then open http://localhost:8787
#
# This is the "try before you deploy the whole thing" sandbox. The full Sidekick DevOps combo
# (deploy/provision/monitor host infra) is the Docker-Compose / Helm quickstart, not this image.
FROM python:3.12-slim
WORKDIR /app

# git is needed at install time (a couple of git-based dependencies); tini for clean signal handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/*

COPY . /app
RUN pip install --no-cache-dir '.[projects]'

COPY deploy/try/entrypoint.sh /usr/local/bin/redevops-try
RUN chmod +x /usr/local/bin/redevops-try

# Defaults; the entrypoint honours LLM_API_KEY / LLM_BASE_URL / LLM_MODEL provided at run time.
ENV PROJECTS_HOST=0.0.0.0 PROJECTS_PORT=8787 \
    LLM_BASE_URL=https://api.openai.com/v1
EXPOSE 8787

# A lightweight liveness check so `docker ps` shows (un)healthy while you try it.
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=4 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PROJECTS_PORT','8787')+'/', timeout=3).status==200 else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/redevops-try"]
