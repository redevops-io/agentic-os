FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "python-multipart>=0.0.9" "pyyaml>=6"
# SuppressionLedger + handle_unsubscribe live in agentic_os.world.outreach (imports runtime_contracts).
COPY agentic_os agentic_os
COPY runtime_contracts runtime_contracts
COPY app.py .
EXPOSE 8103
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8103", "--loop", "asyncio", "--http", "h11"]
