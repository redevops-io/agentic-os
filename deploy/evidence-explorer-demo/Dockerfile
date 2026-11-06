# Two deployment profiles, two targets. They are NOT switchable by an environment variable.
#   docker build --target authenticated    ... -> app.py        (needs EVIDENCE_EXPLORER_TOKEN, refuses to start without it)
#   docker build --target public-synthetic ... -> public_app.py (anonymous synthetic read-only demo; owner decision to deploy)
# Build context: the staged tree from deploy.sh (agentic_os/, runtime_contracts/, the *.py files here).
FROM python:3.12-slim AS base
WORKDIR /app
# duckdb is pinned to the version the repo's lock files pin (packaging/locks/requirements-*.lock) so the engine behaviour
# the sandbox depends on is the tested one.
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6" "pyyaml>=6" "cryptography>=42" "duckdb==1.5.5" \
 && useradd --system --no-create-home --uid 10001 evidence \
 && mkdir -p /var/lib/evidence && chown evidence /var/lib/evidence
# The real v0.3.0 kernel (SecurityMonitor + MissionTrace + Executor) and the canonical contracts
# INCLUDING runtime_contracts.store (the EvidenceStore seam) - the corpus is persisted through the real
# open interface, not faked. The query worker (agentic_os/evidence_query) then reads it through the scoped catalog.
COPY agentic_os agentic_os
COPY runtime_contracts runtime_contracts
COPY explorer_core.py .
ENV EVIDENCE_PATH=/var/lib/evidence/evidence.jsonl EVIDENCE_AUDIT_PATH=/var/lib/evidence/audit.jsonl PYTHONDONTWRITEBYTECODE=1
USER evidence
EXPOSE 8101

FROM base AS authenticated
COPY app.py .
# --loop asyncio / --http h11: the host's AppArmor 4.1 blocks socketpair() in uvloop/httptools.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8101", "--loop", "asyncio", "--http", "h11"]

FROM base AS public-synthetic
COPY public_app.py .
CMD ["uvicorn", "public_app:app", "--host", "0.0.0.0", "--port", "8101", "--loop", "asyncio", "--http", "h11"]
