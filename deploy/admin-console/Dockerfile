FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6" "pyyaml>=6"
COPY agentic_os agentic_os
COPY runtime_contracts runtime_contracts
COPY app.py .
EXPOSE 8105
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8105", "--loop", "asyncio", "--http", "h11"]
