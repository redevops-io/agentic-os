# lifecycle — FastAPI agent layer + MD3 dashboard over a real Listmonk core.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8211
EXPOSE 8211
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8211"]
