# agentic-social-autopilot — FastAPI agent layer + MD3 dashboard over a real Postiz core.
FROM python:3.12-slim

WORKDIR /app

# postgresql-client provides `psql`, used to read/write Postiz's postgres over TCP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py vibex.py .

# The product-demo reel is recorded by a HOST-side UI-agent worker (headless Chrome + ffmpeg);
# this container drives it via a shared spool and serves the resulting MP4. The reel/ dir
# (out/ + spool/) is bind-mounted from the host at runtime.
ENV PORT=8206
EXPOSE 8206

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8206"]
