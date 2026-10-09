# agentic-billing — FastAPI agent layer + MD3 dashboard over a real Lago core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/billing) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./billing/
RUN touch billing/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   LAGO_API_URL, LAGO_API_KEY, LAGO_FRONT_URL
# Note: from inside a container, LAGO_API_URL should point at the Lago api service
# (e.g. http://api:3000 or http://host.docker.internal:3000), not localhost.
ENV PORT=8201 PYTHONPATH=/app
EXPOSE 8201

CMD ["uvicorn", "billing.app:app", "--host", "0.0.0.0", "--port", "8201"]
