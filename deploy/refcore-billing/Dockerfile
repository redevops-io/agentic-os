FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6"
COPY app.py .
EXPOSE 8106
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8106", "--loop", "asyncio", "--http", "h11"]
