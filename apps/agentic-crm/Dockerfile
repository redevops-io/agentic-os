# agentic-crm — FastAPI agent layer + MD3 dashboard over a real ERPNext CRM core.
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
# dir name (apps/agentic-crm) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./agentic_crm/
RUN touch agentic_crm/__init__.py
# Live config injected at runtime (compose env / seed .env):
#   ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_FRONT_URL,
#   REDEVOPS_LLM_BASE_URL, REDEVOPS_LLM_MODEL, [DEERFLOW_URL], [ANTHROPIC_API_KEY]
# From inside a container ERPNEXT_URL should point at host.docker.internal:8092.
ENV PORT=8210 PYTHONPATH=/app
EXPOSE 8210
CMD ["uvicorn", "agentic_crm.app:app", "--host", "0.0.0.0", "--port", "8210"]
