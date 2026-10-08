"""Serve a Suite as one surface over the canonical apps' operators (plan §3.1/§13).

A suite is "not necessarily a separate agent service" — it is a composition over the canonical apps.
``build_suite_app`` realises that at runtime: it mounts each composed app's Operator surface
(``GET /capabilities`` + ``POST /invoke``) under ``/m/<app>`` on a single FastAPI app, exposes the
suite manifest at ``/suite``, and registers the suite (fail-closed) so the surface advertises a
governed, conformant composition. This is what lets the former v6 domain-agent *services* be replaced
by a suite served from the canonical operators, with no bespoke per-agent service code.

Operators are injected (``operators`` maps app name -> Operator), so this is testable with fakes and
deploys with the real app operators. ``operators_for_suite`` builds them from the apps' ``manifest``
modules when you want the real thing.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from .registry import AppRegistry
from .suite import Suite, SuiteRegistry


def operators_for_suite(suite: Suite, importer: Callable[[str], Any]) -> Dict[str, Any]:
    """Build the real operators for a suite's apps. ``importer(app)`` returns the app's ``manifest``
    module (which exposes ``default_operator()``) — e.g. ``lambda a: importlib.import_module(f"{a}.manifest")``."""
    out: Dict[str, Any] = {}
    for app in suite.apps:
        out[app] = importer(app).default_operator()
    return out


def build_suite_app(suite: Suite, operators: Mapping[str, Any], *,
                    app_registry: Optional[AppRegistry] = None,
                    manifests: Optional[Mapping[str, Any]] = None) -> Any:
    """A FastAPI app serving ``suite`` over the given operators.

    Mounts each app's operator under ``/m/<app>`` (its ``/capabilities`` + ``/invoke``). ``/suite``
    returns the composition manifest, ``/`` a landing, ``/health`` liveness. If ``manifests`` (app ->
    AppManifest) is given, the suite is registered through an AppRegistry so the surface is
    conformance-gated (fail-closed on a non-runtime-native app).
    """
    from fastapi import FastAPI

    missing = [a for a in suite.apps if a not in operators]
    if missing:
        raise ValueError(f"suite '{suite.name}' is missing operators for {missing}")

    caps: list[str] = []
    registered_caps: frozenset = frozenset()
    if manifests is not None:
        reg = app_registry or AppRegistry()
        for app in suite.apps:
            if app not in reg:
                reg.register(manifests[app], operators[app])
        registered_caps = SuiteRegistry(reg).register(suite).capabilities
        caps = sorted(registered_caps)
    else:
        for app in suite.apps:
            caps.extend(c.name for c in operators[app].manifest.capabilities)
        caps = sorted(set(caps))

    app = FastAPI(title=f"suite:{suite.name}",
                  description=suite.description or f"{suite.name} suite over {', '.join(suite.apps)}")
    for name in suite.apps:
        app.include_router(operators[name].router(), prefix=f"/m/{name}")

    @app.get("/suite")
    def suite_manifest() -> dict:
        return {"suite": suite.name, "version": suite.version, "description": suite.description,
                "apps": list(suite.apps), "capabilities": caps,
                "mission_templates": list(suite.mission_templates),
                "surfaces": [f"/m/{a}" for a in suite.apps]}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "suite": suite.name, "apps": list(suite.apps),
                "capabilities": len(caps)}

    @app.get("/")
    def landing() -> dict:
        return {"suite": suite.name, "composes": list(suite.apps), "capabilities": caps,
                "invoke": f"/m/<app>/invoke", "manifest": "/suite"}

    return app


def serve(suite_name: str = "revenue", *, host: str = "0.0.0.0", port: int = 8300) -> None:  # pragma: no cover
    """Entry point: build the real operators for ``suite_name`` and serve the suite (needs apps/ on path)."""
    import importlib

    import uvicorn

    from agentic_os import suites as _suites

    suite = {s.name: s for s in _suites.DOMAIN_SUITES}[suite_name]
    importer = lambda a: importlib.import_module(f"{a}.manifest")  # noqa: E731
    ops = operators_for_suite(suite, importer)
    mans = {a: importer(a).MANIFEST for a in suite.apps}
    uvicorn.run(build_suite_app(suite, ops, manifests=mans), host=host, port=port)


__all__ = ["operators_for_suite", "build_suite_app", "serve"]
