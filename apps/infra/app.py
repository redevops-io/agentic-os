"""infra operator service — the deployment arm (Terraform + Ansible) as a Mission Runtime operator.

Run: uvicorn infra.app:app --port 8230  (mounts GET /capabilities + POST /invoke + GET /health).
"""
from __future__ import annotations

import os

from fastapi import FastAPI

# ── permissions: basic (AGPL) permissions or the enterprise plane; the app starts gated or not at all (agentic_os/app_gate.py). Every route that
# EXECUTES a tool asks the gate first (authorisation), separately from the approval the core stages for consequential actions.
from agentic_os.app_gate import RequestContext as _RequestContext, denial_result as _denial_result, deny_response as _deny_response, install_gate as _install_gate  # noqa: E402
from agentic_os.app_gate import with_authorization as _with_authorization
_GATE_APP = "infra"
_req_principal = _install_gate(_GATE_APP)

app = FastAPI(title="infra operator (Terraform + Ansible)")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route
app.add_middleware(_RequestContext)   # the active gate (basic or enterprise) sees THIS request's principal in every route


@app.get("/health")
def health() -> dict:
    return _with_authorization(_GATE_APP, {"status": "ok", "core": "terraform+ansible"})


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
    from agentic_os.app_gate import require_explicit_setting_when_exposed as _exposed
    _exposed("0.0.0.0")   # a reachable bind needs an explicit permissions setting
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8230")))
