# suite-server — serve a Suite (plan §3.1/§13) as ONE surface composing the canonical apps' operators
# (/m/<app>/{capabilities,invoke} + /suite manifest). This is the runtime that lets a domain "suite"
# run from the canonical operators instead of a bespoke per-agent service. Pick the suite with SUITE=.
#
# Build context is the agentic-os repo ROOT (so the image can COPY apps/):
#     docker build -f apps/_suite-server/Dockerfile --build-arg KERNEL_REF=v0.3.37 -t suite-server .
#     docker run -e SUITE=revenue -p 8300:8300 suite-server
#
# The composed app operators are pure (httpx against their cores at invoke time — no web framework in
# core.py), so only fastapi/uvicorn/httpx/pyyaml + the agentic_os kernel (app_kit) are required.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# app_kit (incl. suite_server) is pinned to a kernel release tag (same tag as every app image); override to bump.
ARG KERNEL_REF=v0.3.37
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27" "httpx>=0.27" "pyyaml>=6.0" \
      "agentic-os @ git+https://github.com/redevops-io/agentic-os.git@${KERNEL_REF}" \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
# The composed apps (operators + manifests + producers). apps/ is the hyphenated-package root.
COPY apps ./apps
# Run as an unprivileged user; /app and /data are writable by it (state lives under $HOME or /data).
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir -p /data && chown -R app:app /app /data
RUN printf '#!/bin/sh\n[ -w /data ] || { echo "/data is not writable by uid 10001; see apps/README.md (Upgrading images)" >&2; exit 1; }\nexec "$@"\n' > /usr/local/bin/aos-entrypoint \
 && chmod 755 /usr/local/bin/aos-entrypoint
ENTRYPOINT ["aos-entrypoint"]
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s%s/health' % (os.environ.get('PORT','8300'), os.environ.get('SUITE_BASE','').rstrip('/')), timeout=3)"
ENV PYTHONPATH=/app/apps PORT=8300 SUITE=revenue HOST=0.0.0.0
EXPOSE 8300
# sh -c so $PORT/$SUITE expand at runtime (parity with the other app images).
CMD ["sh", "-c", "python apps/_suite-server/serve.py"]
