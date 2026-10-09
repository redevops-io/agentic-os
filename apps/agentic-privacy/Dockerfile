# agentic-privacy — FastAPI DSAR orchestrator over the live personal-data cores.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/agentic-privacy) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./agentic_privacy/
RUN touch agentic_privacy/__init__.py
ENV PORT=8212 PRIVACY_DATA_DIR=/data PYTHONPATH=/app
EXPOSE 8212
CMD ["uvicorn", "agentic_privacy.app:app", "--host", "0.0.0.0", "--port", "8212"]
