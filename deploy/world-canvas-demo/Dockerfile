FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6" "pyyaml>=6"
# The real world-runtime engine (agentic_os.world) + the canonical contracts incl. runtime_contracts.world.
COPY agentic_os agentic_os
COPY runtime_contracts runtime_contracts
COPY app.py .
EXPOSE 8102
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8102", "--loop", "asyncio", "--http", "h11"]
