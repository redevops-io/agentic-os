# agentic-guide — RAG onboarding assistant over the stack's app docs (redevops-rag pattern).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/guide) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./guide/
RUN touch guide/__init__.py
# Optional LLM synthesis: REDEVOPS_LLM_BASE_URL / REDEVOPS_LLM_MODEL (falls back to retrieval).
ENV PORT=8215 PYTHONPATH=/app
EXPOSE 8215
CMD ["uvicorn", "guide.app:app", "--host", "0.0.0.0", "--port", "8215"]
