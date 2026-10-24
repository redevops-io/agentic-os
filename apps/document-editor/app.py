"""document-editor FastAPI surface.

  GET  /health            liveness of THIS service only (never depends on the editor)
  GET  /capabilities      operator manifest (authenticated)
  POST /invoke            capability invocation (authenticated, tenant-scoped, signed approval tokens)
  GET  /content/{token}   short-lived signed document/script bytes for the external editor to fetch
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from .errors import DocEditorError
from .operator import build_document_editor_operator
from .secure_router import secure_router
from .wiring import ServiceHolder, _HOLDER

_log = logging.getLogger("document_editor")

app = FastAPI(title="agentic-document-editor", docs_url=None, redoc_url=None, openapi_url=None)

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route
holder = _HOLDER
operator = build_document_editor_operator(holder.get)
app.include_router(secure_router(operator))


@app.get("/health")
def health():
    return {"status": "ok", "app": "document-editor"}


@app.get("/content/{token}")
def content(token: str):
    """Authenticated by the signed, expiring, document-scoped token alone (the editor cannot send our bearer)."""
    try:
        data, media = holder.get().serve_content(token)
    except DocEditorError as e:
        return JSONResponse({"error": e.code}, status_code=403 if e.http_status in (403, 404) else e.http_status)
    except Exception:  # noqa: BLE001
        _log.exception("content serve failed")
        raise HTTPException(500, "content unavailable")
    return Response(content=data, media_type=media, headers={"Cache-Control": "no-store"})
