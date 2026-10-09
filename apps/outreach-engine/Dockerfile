# agentic-outreach-engine — FastAPI agent layer + MD3 dashboard over a real Twenty CRM core.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/outreach-engine) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./outreach_engine/
RUN touch outreach_engine/__init__.py
# Config injected at runtime: TWENTY_URL, TWENTY_API_KEY, GITHUB_TOKEN (optional).
ENV PORT=8214 PYTHONPATH=/app
EXPOSE 8214
CMD ["uvicorn", "outreach_engine.app:app", "--host", "0.0.0.0", "--port", "8214"]
