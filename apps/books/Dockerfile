# agentic-books — FastAPI agent layer + MD3 dashboard over a real ERPNext core.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/books) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./books/
RUN touch books/__init__.py

# Live data config is injected at runtime (compose env or --env-file the seed .env):
#   ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_FRONT_URL, COMPANY
# Note: from inside a container, ERPNEXT_URL should point at the ERPNext host gateway
# (e.g. http://host.docker.internal:8092), not localhost.
ENV PORT=8209 PYTHONPATH=/app
EXPOSE 8209

CMD ["uvicorn", "books.app:app", "--host", "0.0.0.0", "--port", "8209"]
