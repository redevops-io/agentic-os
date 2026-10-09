# lifecycle — FastAPI agent layer + MD3 dashboard over a real Listmonk core.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/lifecycle) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./lifecycle/
RUN touch lifecycle/__init__.py
ENV PORT=8211 PYTHONPATH=/app
EXPOSE 8211
CMD ["uvicorn", "lifecycle.app:app", "--host", "0.0.0.0", "--port", "8211"]
