# agentic-support — FastAPI agent layer + MD3 dashboard over a real Chatwoot core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/support) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./support/
RUN touch support/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   CHATWOOT_API_URL, CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_FRONT_URL
# Note: from inside a container, CHATWOOT_API_URL should point at the Chatwoot rails
# service (e.g. http://host.docker.internal:3003), not localhost.
ENV PORT=8207 PYTHONPATH=/app
EXPOSE 8207

CMD ["uvicorn", "support.app:app", "--host", "0.0.0.0", "--port", "8207"]
