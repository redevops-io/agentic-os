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
except Exception:  # noqa: BLE001
    pass


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8230")))
