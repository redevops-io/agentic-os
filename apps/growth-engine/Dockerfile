# agentic-growth-engine — FastAPI agent layer + MD3 dashboard over a real Umami core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   UMAMI_URL, WEBSITE_ID, UMAMI_ADMIN_USER, UMAMI_ADMIN_PASS, UMAMI_FRONT_URL
# Note: from inside a container, UMAMI_URL should point at the Umami service
# (e.g. http://umami:3000 or http://host.docker.internal:3002), not localhost.
ENV PORT=8205
EXPOSE 8205

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8205"]
