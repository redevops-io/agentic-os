# agentic-privacy — FastAPI DSAR orchestrator over the live personal-data cores.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8212 PRIVACY_DATA_DIR=/data
EXPOSE 8212
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8212"]
