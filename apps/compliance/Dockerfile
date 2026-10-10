# agentic-compliance — FastAPI agent layer + MD3 dashboard over a real OpenSCAP core.
#
# This image serves the dashboard from the cached oscap results (results/scan-results.xml
# + report.html), which are produced by scan.sh / seed.py on the host. The container
# itself does NOT need oscap to serve the dashboard — it parses the results file.
#
# To let POST /agent/run {"action":"scan"} re-run the scan from inside this container,
# mount the docker socket and ensure `sudo docker` is available; otherwise run scans on
# the host with scan.sh and the dashboard picks up the refreshed file automatically.
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
# dir name (apps/compliance) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./compliance/
RUN touch compliance/__init__.py
# Bake in the real scan artifacts + SCAP content so the image is self-contained.
COPY results/ ./results/
COPY content/ ./content/

# Point app.py at the baked-in results (override via env / mount for a live host path).
ENV SCAP_RESULTS=/app/results/scan-results.xml \
    SCAP_REPORT=/app/results/report.html \
    PORT=8208 \
    PYTHONPATH=/app
EXPOSE 8208

CMD ["uvicorn", "compliance.app:app", "--host", "0.0.0.0", "--port", "8208"]
