"""Serves the browser console (apps/edge-sentinel/ui): static files, no build step, no third-party asset.

The files carry no data, so they need no credential (the sign-in page has to load before anyone can sign in); everything the console
shows comes from `/api/v1`, which does. A strict Content-Security-Policy (no inline script or style, no eval, same origin only) is set
HERE so it replaces the legacy page's looser default. `GET /` redirects to `/ui/` when SENTINEL_UI is on (the standalone image's default).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse, Response

UI_DIR = Path(__file__).resolve().parent / "ui"
FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "api.js": "text/javascript; charset=utf-8",
         "dom.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8", "icon.svg": "image/svg+xml"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
           "Cache-Control": "no-cache"}

router = APIRouter()


def ui_enabled() -> bool:
    return os.environ.get("SENTINEL_UI", "").strip().lower() in ("1", "true", "yes", "on")


@router.get("/ui", include_in_schema=False)
def ui_root():
    return RedirectResponse("/ui/", status_code=302)


@router.get("/ui/{path:path}", include_in_schema=False)
def ui_file(path: str):
    name = path.strip("/")
    if name in FILES and (UI_DIR / name).is_file():
        return FileResponse(UI_DIR / name, media_type=FILES[name].split(";")[0], headers=HEADERS)
    if "." in name.rsplit("/", 1)[-1]:                       # a file name we do not serve
        return Response(status_code=404, headers=HEADERS)
    return FileResponse(UI_DIR / "index.html", media_type="text/html", headers=HEADERS)     # the console's own client-side routes
