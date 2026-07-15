# agentic-outreach-engine — FastAPI agent layer + MD3 dashboard over a real Twenty CRM core.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# Config injected at runtime: TWENTY_URL, TWENTY_API_KEY, GITHUB_TOKEN (optional).
ENV PORT=8214
EXPOSE 8214
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8214"]
