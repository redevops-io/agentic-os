# agentic-compliance — FastAPI agent layer + MD3 dashboard over a real OpenSCAP core.
#
# This image serves the dashboard from the cached oscap results (results/scan-results.xml
# + report.html), which are produced by scan.sh / seed.py on the host. The container
# itself does NOT need oscap to serve the dashboard — it parses the results file.
#
# To let POST /agent/run {"action":"scan"} re-run the scan from inside this container,
# mount the docker socket and ensure `sudo docker` is available; otherwise run scans on
# the host with scan.sh and the dashboard picks up the refreshed file automatically.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py scan.sh ./
# Bake in the real scan artifacts + SCAP content so the image is self-contained.
COPY results/ ./results/
COPY content/ ./content/

# Point app.py at the baked-in results (override via env / mount for a live host path).
ENV SCAP_RESULTS=/app/results/scan-results.xml \
    SCAP_REPORT=/app/results/report.html \
    PORT=8208
EXPOSE 8208

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8208"]
