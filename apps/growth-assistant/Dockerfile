# growth-assistant — FastAPI growth strategist + MD3 dashboard.
# Generates 4-pillar traction assets and wires into ERPNext / Listmonk / Postiz.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The agentic-os KERNEL is pinned HERE (single source, overridable) rather than in requirements.txt, so
# the image tracks a known kernel tag instead of a stale commit. Override at build time, e.g.
#   docker build --build-arg KERNEL_REF=main .   (or a newer release tag)
ARG KERNEL_REF=v0.3.24
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "agentic-os @ https://github.com/redevops-io/agentic-os/archive/${KERNEL_REF}.tar.gz" \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/growth-assistant) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./growth_assistant/
RUN touch growth_assistant/__init__.py
# Live config injected at runtime (compose env):
#   REDEVOPS_LLM_BASE_URL, REDEVOPS_LLM_MODEL, [ANTHROPIC_API_KEY],
#   ERPNEXT_URL/_API_KEY/_API_SECRET, LISTMONK_API_URL/_API_USER/_API_TOKEN,
#   POSTIZ_API_URL/_API_KEY, GROWTH_DATA_DIR, GROWTH_TENANT
# From inside a container the core URLs should point at host.docker.internal:<port>.
ENV PORT=8213 PYTHONPATH=/app
EXPOSE 8213
CMD ["uvicorn", "growth_assistant.app:app", "--host", "0.0.0.0", "--port", "8213"]
