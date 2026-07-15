# agentic-guide — RAG onboarding assistant over the stack's app docs (redevops-rag pattern).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# Optional LLM synthesis: REDEVOPS_LLM_BASE_URL / REDEVOPS_LLM_MODEL (falls back to retrieval).
ENV PORT=8215
EXPOSE 8215
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8215"]
