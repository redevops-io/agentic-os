# agentic-control-tower — FastAPI agent layer + MD3 dashboard over a real Metabase core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/control-tower) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./control_tower/
RUN touch control_tower/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   METABASE_API_URL, METABASE_SESSION, METABASE_DB_ID, METABASE_FRONT_URL
# Note: from inside a container, METABASE_API_URL should point at the Metabase service
# (e.g. http://metabase:3000 or http://host.docker.internal:3001), not localhost.
ENV PORT=8202 PYTHONPATH=/app
EXPOSE 8202

CMD ["uvicorn", "control_tower.app:app", "--host", "0.0.0.0", "--port", "8202"]
