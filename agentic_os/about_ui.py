"""Public ``/about/`` pages: the About and licences view served from the build record that ships in the Licenses folder.

Design (docs/ABOUT_LEGAL_UI.md): the pages are STATIC and PUBLIC (they work without a session, before and without sign-in) and
are the only routes added to the unauthenticated GET surface of the local capability gate. They are registered one exact path at a
time through ``local_capability_ui.install_ui_bootstrap`` and returned to it as ``extra`` public paths:

    /about/                      the page (HTML)
    /about/app.js, /about/app.css
    /about/about.json            the build record (schema redevops.about/1), verified at start-up
    /about/files/<slug>          one shipped licence/notice file per slug of the record, byte-exact, text/plain

There is NO directory mount, no static-file handler, no path parameter and no user-controlled path join: the served bodies are held in
memory, keyed by slugs that come from the verified record itself, so a request can only select among them by exact string match
and traversal is impossible by construction. Anything else under ``/about/`` is not a public path: the gate answers 401 before
routing (the route table is not an oracle), and any method other than GET/HEAD is gated the same way. The routes change no state
and return no project data; they add no CORS header.

Verification at start-up (fail closed): the record must parse and validate (``about_record``), be listed with its sha256 in
LICENSES-MANIFEST.json, and every file it names must exist with the recorded size and sha256. Otherwise ``/about/about.json``
answers a tiny fixed error document and NO file is served; with no record at all it answers 404 (a development checkout, a
container, a wheel install). In both cases ``/about/`` still answers so that a signed-out person gets an explanation instead of the
gate's 401. The page inserts every value as text and never loads anything from outside its own origin.

Nothing here decides anything: the record is data for display (see ``about_record``).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import Response

from . import about_record as rec

ENTRY_ENABLED = True            # build-time switch for the whole About entry (the owner may set it to False)
ABOUT_PATH = "/about/"
_ASSET_DIR = Path(__file__).resolve().parent / "about_assets"
_ASSETS = {                      # url path -> (file, media type)
    "/about/": ("about.html", "text/html; charset=utf-8"),
    "/about/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/about/app.css": ("app.css", "text/css; charset=utf-8"),
}
ERR_INVALID, ERR_MISMATCH, ERR_MANIFEST = "ABOUT_RECORD_INVALID", "ABOUT_HASH_MISMATCH", "ABOUT_MANIFEST_MISSING"
ENV_DIR = "AGENTIC_OS_LICENSES_DIR"
NOT_AVAILABLE = (b"The About page is not available in this installation. "
                 b"The licence files are in the Licenses folder of the application.\n")


def find_licenses_dir() -> Optional[Path]:
    """Where the Licenses folder is: the PyInstaller bundle first; from a checkout, ``$AGENTIC_OS_LICENSES_DIR`` (tests, development)
    and then the folder the launcher build stages. A frozen sidecar never looks anywhere else (no environment override)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        cands = [Path(base) / "Licenses"] if base else []
    else:
        cands = []
        if os.environ.get(ENV_DIR):
            cands.append(Path(os.environ[ENV_DIR]))
        cands.append(Path(__file__).resolve().parent.parent / "deploy" / "launcher" / "src-tauri" / "licenses")
    for c in cands:
        if (c / "about.json").is_file():
            return c
    return None


def _read_capped(path: Path, cap: int) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
    except OSError:
        return None
    return data if len(data) <= cap else None


class AboutState:
    """Result of the start-up verification. ``status``: ok | absent | invalid."""

    def __init__(self) -> None:
        self.status = "absent"
        self.error_code = ""
        self.record_bytes = b""
        self.record_sha = ""
        self.files: Dict[str, Tuple[bytes, str]] = {}          # slug -> (bytes, sha256)

    def fail(self, code: str) -> "AboutState":
        self.status, self.error_code, self.record_bytes, self.files = "invalid", code, b"", {}
        return self


def load_state(licenses_dir: Optional[Path]) -> AboutState:
    st = AboutState()
    if licenses_dir is None:
        return st
    raw = _read_capped(licenses_dir / "about.json", rec.MAX_BYTES_READ)
    if raw is None:
        return st.fail(ERR_INVALID) if (licenses_dir / "about.json").exists() else st
    doc, problems = rec.parse_record(raw)
    if doc is None:
        return st.fail(ERR_INVALID)
    manifest_raw = _read_capped(licenses_dir / "LICENSES-MANIFEST.json", 1024 * 1024)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))["files"] if manifest_raw is not None else None
    except (ValueError, KeyError, UnicodeDecodeError, AttributeError):
        manifest = None
    if not isinstance(manifest, dict):
        return st.fail(ERR_MANIFEST)
    sha = hashlib.sha256(raw).hexdigest()
    if (manifest.get("about.json") or {}).get("sha256") != sha:
        return st.fail(ERR_MISMATCH)
    wanted = [(f["id"], f["file"], f["bytes"], f["sha256"]) for f in doc["licence_files"]]
    wanted.append(("notices", doc["notices"]["file"], doc["notices"]["bytes"], doc["notices"]["sha256"]))
    files: Dict[str, Tuple[bytes, str]] = {}
    for slug, name, size, want in wanted:
        data = _read_capped(licenses_dir / name, min(size, rec.MAX_SERVED_FILE_BYTES))
        if data is None or len(data) != size or hashlib.sha256(data).hexdigest() != want:
            return st.fail(ERR_MISMATCH)
        files[slug] = (data, want)
    st.status, st.record_bytes, st.record_sha, st.files = "ok", raw, sha, files
    return st


def install_about(app: Any, base_path: str = "", licenses_dir: Optional[Path] = None) -> frozenset:
    """Register the About routes on ``app`` and return their exact paths (to be added to the public UI paths).
    ``base_path`` is accepted for symmetry with ``install_ui_bootstrap``; every URL inside the pages is relative."""
    if not ENTRY_ENABLED:
        return frozenset()
    paths = set()

    def unavailable(request: Request) -> Response:
        return Response(NOT_AVAILABLE, status_code=404, media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    try:
        assets = {path: ((_ASSET_DIR / name).read_bytes(), media) for path, (name, media) in _ASSETS.items()}
    except OSError as e:
        # A missing About asset must never stop the control plane: one ASCII log line, no About routes except a plain-text 404
        # note on /about/ (the shell's About link then shows that note instead of the gate's 401).
        sys.stderr.write(f"about: an About page asset is unreadable ({e.__class__.__name__}); the About page is disabled, "
                         "the rest of the service is not affected\n")
        sys.stderr.flush()
        app.add_api_route("/about/", unavailable, methods=["GET", "HEAD"], include_in_schema=False)
        app.state.about_state = AboutState()
        return frozenset({"/about/"})
    try:
        state = load_state(licenses_dir if licenses_dir is not None else find_licenses_dir())
    except Exception as e:                      # never let the legal-notices page take the service down
        sys.stderr.write(f"about: the build record could not be loaded ({e.__class__.__name__}); the About page shows an error\n")
        sys.stderr.flush()
        state = AboutState().fail(ERR_INVALID)

    def reply(request: Request, body: bytes, media: str, sha: str) -> Response:
        etag = f'"{sha}"'
        headers = {"Cache-Control": "no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(body, media_type=media, headers=headers)

    def serve(path: str, body: bytes, media: str) -> None:
        sha = hashlib.sha256(body).hexdigest()

        def handler(request: Request) -> Response:
            return reply(request, body, media, sha)
        app.add_api_route(path, handler, methods=["GET", "HEAD"], include_in_schema=False)
        paths.add(path)

    for path, (body, media) in assets.items():
        serve(path, body, media)

    if state.status == "ok":
        serve("/about/about.json", state.record_bytes, "application/json")
        for slug, (data, _sha) in state.files.items():
            serve(f"/about/files/{slug}", data, "text/plain; charset=utf-8")
    elif state.status == "invalid":
        err = json.dumps({"schema": rec.SCHEMA, "error": {"code": state.error_code}}, separators=(",", ":")).encode("ascii")
        serve("/about/about.json", err, "application/json")
    else:
        def absent(request: Request) -> Response:
            return Response(b'{"error":{"code":"ABOUT_NO_RECORD"}}', status_code=404, media_type="application/json",
                            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
        app.add_api_route("/about/about.json", absent, methods=["GET", "HEAD"], include_in_schema=False)
        paths.add("/about/about.json")
    app.state.about_state = state
    return frozenset(paths)
