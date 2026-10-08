"""infra operator service — the deployment arm (Terraform + Ansible) as a Mission Runtime operator.

Run: uvicorn infra.app:app --port 8230  (mounts GET /capabilities + POST /invoke + GET /health).
"""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="infra operator (Terraform + Ansible)")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "terraform+ansible"}


# ── Mission Runtime operator surface (guarded: runs standalone without agentic-os) ──
try:
    from .operator import build_infra_operator
    app.include_router(build_infra_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface + manifest registration NOT done (%s: %s)",
        __name__, type(_op_exc).__name__, _op_exc)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8230")))
