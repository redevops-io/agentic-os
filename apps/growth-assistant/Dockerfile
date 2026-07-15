# growth-assistant — FastAPI growth strategist + MD3 dashboard.
# Generates 4-pillar traction assets and wires into ERPNext / Listmonk / Postiz.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# Live config injected at runtime (compose env):
#   REDEVOPS_LLM_BASE_URL, REDEVOPS_LLM_MODEL, [ANTHROPIC_API_KEY],
#   ERPNEXT_URL/_API_KEY/_API_SECRET, LISTMONK_API_URL/_API_USER/_API_TOKEN,
#   POSTIZ_API_URL/_API_KEY, GROWTH_DATA_DIR, GROWTH_TENANT
# From inside a container the core URLs should point at host.docker.internal:<port>.
ENV PORT=8213
EXPOSE 8213
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8213"]
