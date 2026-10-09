# agentic-social-autopilot — FastAPI agent layer + MD3 dashboard over a real Postiz core.
FROM python:3.12-slim

WORKDIR /app

# postgresql-client provides `psql`, used to read/write Postiz's postgres over TCP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import core`, …) that a flat `app:app` cannot resolve, and the source
# dir name (apps/social-autopilot) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./social_autopilot/
RUN touch social_autopilot/__init__.py

# The product-demo reel is recorded by a HOST-side UI-agent worker (headless Chrome + ffmpeg);
# this container drives it via a shared spool and serves the resulting MP4. The reel/ dir
# (out/ + spool/) is bind-mounted from the host at runtime.
ENV PORT=8206 PYTHONPATH=/app
EXPOSE 8206

CMD ["uvicorn", "social_autopilot.app:app", "--host", "0.0.0.0", "--port", "8206"]
