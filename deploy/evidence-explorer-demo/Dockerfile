FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6" "pyyaml>=6" "duckdb>=1.0"
# The real v0.3.0 kernel (SecurityMonitor + MissionTrace + Executor) and the canonical contracts
# INCLUDING runtime_contracts.store (the EvidenceStore seam) — the corpus is persisted through the real
# open interface, not faked. DuckDB then queries the JSONL the file backend wrote.
COPY agentic_os agentic_os
COPY runtime_contracts runtime_contracts
COPY app.py .
ENV EVIDENCE_PATH=/tmp/evidence-explorer/evidence.jsonl
EXPOSE 8101
# --loop asyncio / --http h11: the host's AppArmor 4.1 blocks socketpair() in uvloop/httptools.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8101", "--loop", "asyncio", "--http", "h11"]
