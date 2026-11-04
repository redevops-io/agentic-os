"""UI side of the local capability gate: serve the vendored Projects UI's ``index.html`` with one extra
``<script>`` tag in front of the bundle, plus that script (``local_capability_shim.js``).

The vendored bundle itself is NOT modified: the tag is injected at serve time, so a rebuilt bundle
(``scripts/build_projects_ui.sh``) or a ``$PROJECTS_UI_DIR`` override keeps working unchanged. The shim
exchanges the launch code in the URL fragment for a session and adds the CSRF header to the bundle's
same-origin ``/api`` fetches (the bundle has exactly two ``fetch`` call sites and sets no auth header).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .local_capability import public_ui_paths

SHIM_URL_PATH = "/local-capability-shim.js"
_SHIM_FILE = Path(__file__).resolve().parent / "local_capability_shim.js"


def inject_shim_tag(html: str, base_path: str = "") -> str:
    tag = f'<script src="{base_path}{SHIM_URL_PATH}"></script>'
    if SHIM_URL_PATH in html:
        return html
    out, n = re.subn(r"(<head[^>]*>)", lambda m: m.group(1) + tag, html, count=1, flags=re.I)
    return out if n else tag + html


def install_ui_bootstrap(app: Any, ui_dir: Any, base_path: str = "") -> frozenset:
    """Register ``/``, ``/index.html`` (shim injected) and the shim route ahead of the static mount.
    Returns the exact set of public (unauthenticated GET) paths."""
    from fastapi.responses import HTMLResponse, Response
    ui = Path(ui_dir)

    def _index() -> HTMLResponse:
        html = (ui / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(inject_shim_tag(html, base_path), headers={"Cache-Control": "no-cache"})

    def _shim() -> Response:
        return Response(_SHIM_FILE.read_bytes(), media_type="text/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    app.add_api_route("/", _index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/index.html", _index, methods=["GET"], include_in_schema=False)
    app.add_api_route(SHIM_URL_PATH, _shim, methods=["GET"], include_in_schema=False)
    return public_ui_paths(ui, extra=("/", "/index.html", SHIM_URL_PATH))
