# agentic-market-radar — FastAPI agent layer + MD3 dashboard over a real changedetection.io core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/market-radar) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./market_radar/
RUN touch market_radar/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   CD_API_URL, CD_API_KEY, CD_FRONT_URL
# Note: from inside a container, CD_API_URL should point at the changedetection service
# (e.g. http://changedetection:5000 or http://host.docker.internal:5001), not localhost.
ENV PORT=8204 PYTHONPATH=/app
EXPOSE 8204

CMD ["uvicorn", "market_radar.app:app", "--host", "0.0.0.0", "--port", "8204"]
