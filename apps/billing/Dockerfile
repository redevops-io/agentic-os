# agentic-billing — FastAPI agent layer + MD3 dashboard over a real Lago core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   LAGO_API_URL, LAGO_API_KEY, LAGO_FRONT_URL
# Note: from inside a container, LAGO_API_URL should point at the Lago api service
# (e.g. http://api:3000 or http://host.docker.internal:3000), not localhost.
ENV PORT=8201
EXPOSE 8201

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8201"]
