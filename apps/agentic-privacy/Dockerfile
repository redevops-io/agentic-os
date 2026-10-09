# agentic-privacy — FastAPI DSAR orchestrator over the live personal-data cores.
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
# dir name (apps/agentic-privacy) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./agentic_privacy/
RUN touch agentic_privacy/__init__.py
ENV PORT=8212 PRIVACY_DATA_DIR=/data PYTHONPATH=/app
EXPOSE 8212
CMD ["uvicorn", "agentic_privacy.app:app", "--host", "0.0.0.0", "--port", "8212"]
