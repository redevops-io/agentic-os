FROM python:3.12-slim
WORKDIR /app
# fastapi/uvicorn for the surface; cryptography so the GrantStore does REAL AES-GCM at-rest (encrypted=True) instead of the dev plaintext
# fallback. Pinned to the versions the kernel test suite runs against (a moving range is an unreviewed supply-chain change).
RUN pip install --no-cache-dir "fastapi==0.162.0" "uvicorn==0.54.0" "pydantic==2.14.1" "cryptography==50.0.2" "pyyaml==6.0.3"
# The real kernel permissions plane (agentic_os.permissions + views.permissions_page) — stdlib-only,
# bundled, not faked. No control-plane, no fleet, no registry.
COPY agentic_os agentic_os
COPY app.py .
# Unprivileged: uid 10001 owns the data directory. A volume created by an earlier (root) image must be chown'ed once:
#   docker run --rm -v permissions-demo_permissions_data:/d busybox chown -R 10001:10001 /d
RUN useradd --system --uid 10001 --home-dir /nonexistent --no-create-home app && mkdir -p /data/permissions && chown -R app:app /data
USER app
EXPOSE 8109
# PERMISSIONS_DIR is a writable path for the encrypted grant store (created on first admin use, never at start).
ENV PERMISSIONS_DIR=/data/permissions BIND_HOST=127.0.0.1
# BIND_HOST: one address, never 0.0.0.0 by default (compose sets it to the interface cloudflared reaches).
# --loop asyncio / --http h11: the host's AppArmor blocks socketpair() in uvloop/httptools.
# Proxy headers are EXPLICIT (RT-03): off unless PERMISSIONS_TRUSTED_PROXIES names the proxy, and then honoured only from those addresses, so a loopback peer can
# never choose its throttle identity through X-Forwarded-For. (uvicorn's default is ON for 127.0.0.1.)
CMD ["sh", "-c", "if [ -n \"$PERMISSIONS_TRUSTED_PROXIES\" ]; then exec uvicorn app:app --host \"$BIND_HOST\" --port 8109 --loop asyncio --http h11 --proxy-headers --forwarded-allow-ips \"$PERMISSIONS_TRUSTED_PROXIES\"; else exec uvicorn app:app --host \"$BIND_HOST\" --port 8109 --loop asyncio --http h11 --no-proxy-headers; fi"]
