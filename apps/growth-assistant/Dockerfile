# growth-assistant — FastAPI growth strategist + MD3 dashboard.
# Generates 4-pillar traction assets and wires into ERPNext / Listmonk / Postiz.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/growth-assistant) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./growth_assistant/
RUN touch growth_assistant/__init__.py
# Live config injected at runtime (compose env):
#   REDEVOPS_LLM_BASE_URL, REDEVOPS_LLM_MODEL, [ANTHROPIC_API_KEY],
#   ERPNEXT_URL/_API_KEY/_API_SECRET, LISTMONK_API_URL/_API_USER/_API_TOKEN,
#   POSTIZ_API_URL/_API_KEY, GROWTH_DATA_DIR, GROWTH_TENANT
# From inside a container the core URLs should point at host.docker.internal:<port>.
ENV PORT=8213 PYTHONPATH=/app
EXPOSE 8213
CMD ["uvicorn", "growth_assistant.app:app", "--host", "0.0.0.0", "--port", "8213"]
