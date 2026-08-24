FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6" "pyyaml>=6"
# The real v0.3.0 kernel (SecurityMonitor + MissionTrace + Executor seams) and the canonical contracts
# (SecurityDecision/RuntimeSecurityEvent/TraceContext/correlate/Containment) — bundled, not faked.
COPY agentic_os agentic_os
COPY runtime_contracts runtime_contracts
COPY app.py .
EXPOSE 8099
# --loop asyncio / --http h11: the host's AppArmor 4.1 blocks socketpair() in uvloop/httptools.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8099", "--loop", "asyncio", "--http", "h11"]
