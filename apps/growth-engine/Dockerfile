# agentic-growth-engine — FastAPI agent layer + MD3 dashboard over a real Umami core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/growth-engine) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./growth_engine/
RUN touch growth_engine/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   UMAMI_URL, WEBSITE_ID, UMAMI_ADMIN_USER, UMAMI_ADMIN_PASS, UMAMI_FRONT_URL
# Note: from inside a container, UMAMI_URL should point at the Umami service
# (e.g. http://umami:3000 or http://host.docker.internal:3002), not localhost.
ENV PORT=8205 PYTHONPATH=/app
EXPOSE 8205

CMD ["uvicorn", "growth_engine.app:app", "--host", "0.0.0.0", "--port", "8205"]
