# agentic-outreach-engine — FastAPI agent layer + MD3 dashboard over a real Twenty CRM core.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The agentic-os KERNEL is pinned HERE (single source, overridable) rather than in requirements.txt, so
# the image tracks a known kernel tag instead of a stale commit. Override at build time, e.g.
#   docker build --build-arg KERNEL_REF=main .   (or a newer release tag)
ARG KERNEL_REF=v0.3.36
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "agentic-os @ https://github.com/redevops-io/agentic-os/archive/${KERNEL_REF}.tar.gz" \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/outreach-engine) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./outreach_engine/
RUN touch outreach_engine/__init__.py
# Config injected at runtime: TWENTY_URL, TWENTY_API_KEY, GITHUB_TOKEN (optional).
# Run as an unprivileged user; /app and /data are writable by it (state lives under $HOME or /data).
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir -p /data && chown -R app:app /app /data
RUN printf '#!/bin/sh\n[ -w /data ] || { echo "/data is not writable by uid 10001; see apps/README.md (Upgrading images)" >&2; exit 1; }\nexec "$@"\n' > /usr/local/bin/aos-entrypoint \
 && chmod 755 /usr/local/bin/aos-entrypoint
ENTRYPOINT ["aos-entrypoint"]
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)"
ENV PORT=8214 PYTHONPATH=/app
EXPOSE 8214
CMD ["uvicorn", "outreach_engine.app:app", "--host", "0.0.0.0", "--port", "8214"]
