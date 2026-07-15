# agentic-crm — FastAPI agent layer + MD3 dashboard over a real ERPNext CRM core.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# Live config injected at runtime (compose env / seed .env):
#   ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_FRONT_URL,
#   REDEVOPS_LLM_BASE_URL, REDEVOPS_LLM_MODEL, [DEERFLOW_URL], [ANTHROPIC_API_KEY]
# From inside a container ERPNEXT_URL should point at host.docker.internal:8092.
ENV PORT=8210
EXPOSE 8210
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8210"]
