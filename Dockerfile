FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md modules.yaml ./
COPY agentic_os ./agentic_os
COPY deploy/assets ./deploy/assets
RUN pip install --no-cache-dir .
EXPOSE 8080
CMD ["uvicorn", "agentic_os.control_plane:app", "--host", "0.0.0.0", "--port", "8080"]
