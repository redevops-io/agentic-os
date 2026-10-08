# suite-server — serve a Suite (plan §3.1/§13) as ONE surface composing the canonical apps' operators
# (/m/<app>/{capabilities,invoke} + /suite manifest). This is the runtime that lets a domain "suite"
# run from the canonical operators instead of a bespoke per-agent service. Pick the suite with SUITE=.
#
# Build context is the agentic-os repo ROOT (so the image can COPY apps/):
#     docker build -f apps/_suite-server/Dockerfile --build-arg KERNEL_REF=main -t suite-server .
#     docker run -e SUITE=revenue -p 8300:8300 suite-server
#
# The composed app operators are pure (httpx against their cores at invoke time — no web framework in
# core.py), so only fastapi/uvicorn/httpx/pyyaml + the agentic_os kernel (app_kit) are required.
FROM python:3.12-slim
# app_kit (incl. suite_server) lives on the kernel's main line; override to a release tag once cut.
ARG KERNEL_REF=main
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27" "httpx>=0.27" "pyyaml>=6.0" \
      "agentic-os @ git+https://github.com/redevops-io/agentic-os.git@${KERNEL_REF}" \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
# The composed apps (operators + manifests + producers). apps/ is the hyphenated-package root.
COPY apps ./apps
ENV PYTHONPATH=/app/apps PORT=8300 SUITE=revenue HOST=0.0.0.0
EXPOSE 8300
# sh -c so $PORT/$SUITE expand at runtime (parity with the other app images).
CMD ["sh", "-c", "python -m agentic_os.app_kit.suite_server"]
