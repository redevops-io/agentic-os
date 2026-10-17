# agentic-document-editor — connector to an EXTERNALLY MANAGED document editor (stock ONLYOFFICE first).
# The editor is never bundled or managed by this image; documents live on a mounted LOCAL document root.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The agentic-os KERNEL is pinned HERE (single source, overridable) rather than in requirements.txt, so
# the image tracks a known kernel tag instead of a stale commit. Override at build time, e.g.
#   docker build --build-arg KERNEL_REF=main .   (or a newer release tag)
# v0.3.32+ is required: the capabilities declare per-action compensation disposition=PRESERVE.
ARG KERNEL_REF=v0.3.39
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir "agentic-os @ https://github.com/redevops-io/agentic-os/archive/${KERNEL_REF}.tar.gz" \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Copy ALL python modules into an importable PACKAGE — app.py uses package-relative
# imports (`from . import …`) that a flat `app:app` cannot resolve, and the source
# dir name (apps/document-editor) has a hyphen, not a valid module name. Re-home under a valid pkg.
COPY *.py ./document_editor/
RUN touch document_editor/__init__.py

# Runtime configuration (nothing secret is baked in):
#   DOCEDITOR_CONFIG                    path to the JSON/YAML config (document_backend block, tenants, destinations)
#   DOCEDITOR_DOCUMENT_ROOT             mounted local document root
#   DOCEDITOR_STATE_DIR                 connector state (proposals); safe to delete
#   DOCEDITOR_CONTENT_BASE_URL          how the editor reaches THIS service to fetch signed document bytes
#   Vault-provided at runtime:          DOCEDITOR_ONLYOFFICE_URL, DOCEDITOR_ONLYOFFICE_JWT_SECRET,
#                                       DOCEDITOR_POLICY_SIGNING_KEY (optional), DOCEDITOR_CONTENT_SIGNING_SECRET (multi-replica),
#                                       AGENTIC_OS_APPROVAL_SECRET, AGENTIC_OS_CONTROL_TOKEN / AGENTIC_OS_TENANT_TOKENS
# Run as an unprivileged user; /app and /data are writable by it (state lives under $HOME or /data).
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir -p /data && chown -R app:app /app /data
RUN printf '#!/bin/sh\n[ -w /data ] || { echo "/data is not writable by uid 10001; see apps/README.md (Upgrading images)" >&2; exit 1; }\nexec "$@"\n' > /usr/local/bin/aos-entrypoint \
 && chmod 755 /usr/local/bin/aos-entrypoint
ENTRYPOINT ["aos-entrypoint"]
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=3)"
ENV PORT=8216 PYTHONPATH=/app
EXPOSE 8216

CMD ["uvicorn", "document_editor.app:app", "--host", "0.0.0.0", "--port", "8216"]
