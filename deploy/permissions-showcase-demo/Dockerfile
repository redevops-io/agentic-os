FROM python:3.12-slim
WORKDIR /app
# fastapi/uvicorn for the surface; cryptography so the GrantStore does REAL AES-GCM at-rest
# (encrypted=True) instead of the dev plaintext fallback.
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "pydantic>=2.6" "cryptography>=42" "pyyaml>=6"
# The real kernel permissions plane (agentic_os.permissions + views.permissions_page) — stdlib-only,
# bundled, not faked. No control-plane, no fleet, no registry.
COPY agentic_os agentic_os
COPY app.py .
EXPOSE 8109
# PERMISSIONS_DIR is a writable path for the encrypted grant store (seeded at boot).
ENV PERMISSIONS_DIR=/data/permissions
# --loop asyncio / --http h11: the host's AppArmor blocks socketpair() in uvloop/httptools.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8109", "--loop", "asyncio", "--http", "h11"]
