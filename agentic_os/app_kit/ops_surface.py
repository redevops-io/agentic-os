"""The read-only build-identity surface: which revision of this code is answering.

Why this exists is a live observation, not a hypothesis. On 2026-11-11 an anonymous, GET-only probe of the
hosted demo found that nothing a visitor could see identified the running code:

    GET /api/version           -> 404 {"detail":"Not Found"}
    GET /healthz               -> 404 {"detail":"Not Found"}
    GET /api/health            -> 404 {"detail":"Not Found"}
    GET /permissions/healthz   -> 404 {"detail":"Not Found"}

and the deploy files `build:` from local context with no registry digest and no stamp, so even the container
reference named no commit. Every "is the deployed build current?" question — the question an operator has to
be able to answer before trusting anything else a probe reports — ended there. This module answers it: one
`GET` of four non-secret fields, plus the build stamp the deploy layer has to supply.

What this module deliberately is NOT: a place to fix the scheme of a redirect. The same probe saw
`GET /permissions/` answered `307 location: http://demo.redevops.io/permissions` over a TLS visit, because
TLS terminates at the front. That cannot be fixed here, and the reason is worth keeping in writing: uvicorn
installs its own `ProxyHeadersMiddleware` **below** the ASGI app and, by default (`DEFAULT_FORWARDED_ALLOW_IPS`
= `127.0.0.1`), adopts `X-Forwarded-Proto` from a loopback peer before application code sees the scope. By the
time any app-level middleware runs, a client on plaintext has already become "https". An app-level gate would
therefore be a control that looks enforced and is not, so header trust stays where this repository already
decides it — at spawn, explicitly, per deploy unit (`--no-proxy-headers` unless a front is named; see
`deploy/permissions-showcase-demo/Dockerfile`, control RT-03, and `tests/test_ops_surface.py` for the
socket-level proof and the guard that no deploy unit may leave the decision to uvicorn's default).

Neither route is an authorisation surface: both are `GET`s of non-secret build metadata, and the route-policy
layer classifies write routes only.
"""
from __future__ import annotations

import os

ENV_BUILD_REVISION = "AGENTIC_OS_BUILD_REVISION"
ENV_BUILD_TIME = "AGENTIC_OS_BUILD_TIME"


def build_info() -> dict:
    """What this process was built from. Deliberately small: a version, a revision, when, and where the
    revision came from. Nothing here is config, feature flags, paths, hostnames or counts."""
    from agentic_os import __version__

    revision = (os.environ.get(ENV_BUILD_REVISION) or "").strip()
    return {
        "version": __version__,
        "revision": revision or "unknown",
        "build_time": (os.environ.get(ENV_BUILD_TIME) or "").strip() or "unknown",
        "revision_source": (ENV_BUILD_REVISION if revision else "unset"),
    }


def _paths(app) -> set:
    return {getattr(r, "path", None) for r in getattr(app, "routes", [])}


def install_build_identity(app, *, with_healthz: bool = True) -> dict:
    """Add `GET /api/version` — and `GET /healthz` for an orchestrator, unless the app already answers one —
    so a probe can name the code that is running. Returns what it added, so a deploy unit can assert it got
    what it asked for rather than trusting the call to have taken effect.

    The duplicate check looks at the routes that exist **when it is called**, which is the one way this helper
    can be used wrong: called before an app's own `@app.get("/healthz")`, it installs its answer first and
    Starlette then serves that one, silently replacing the unit's liveness contract. Either call this after the
    app's routes are declared, or pass `with_healthz=False` (what `deploy/permissions-showcase-demo` does).

    Nothing here reads a request, a credential or a config value beyond the two build variables, and neither
    route answers more than its own fields: a version endpoint that grows into a config dump becomes an
    enumeration surface, which is why the field set is pinned in tests.
    """
    added = []

    if "/api/version" not in _paths(app):
        @app.get("/api/version", include_in_schema=False)
        def _version():
            return build_info()
        added.append("/api/version")

    if with_healthz and "/healthz" not in _paths(app):
        @app.get("/healthz", include_in_schema=False)
        def _healthz():
            return {"status": "ok"}
        added.append("/healthz")

    return {"added": added, "revision_source": build_info()["revision_source"]}
