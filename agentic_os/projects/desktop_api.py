"""The person at the installed app: approvals, connected agents and installed applications, as JSON for the Projects UI.

Mounted only behind the desktop's local capability gate, and every route requires the **session that gate validated**: the
person who opened the app from its own launcher. An agent's token reaches only ``/api/agent-gateway/``; a page of another
site or port cannot send the session header; no command line reaches these routes.

Nothing here is a second approval engine or a second permission system:

* approvals are ``ApprovalCenter``'s cards and its ``decide()``, the ones the signed-in pages use, so the same refusals,
  the same digest of what was shown and the same authority checks apply;
* connecting an agent gives it a stored, scoped, expiring authority (``local_agents.connect_agent``) and its token FILE;
  a token is never returned. This is the ONLY place an agent can be given a permission that changes anything: the command
  line grants reading only, so an agent that can run commands cannot give itself more;
* an application's records are read and added through the same store and checks as its signed-in pages.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable, Dict, List, Mapping, Optional

from fastapi import Body, HTTPException, Request
from fastapi.responses import Response

from agentic_os import app_definitions as D
from agentic_os.app_kit.route_authz import RoutePolicy
from agentic_os.local_capability import SESSION_SCOPE_KEY
from agentic_os.products.principal import LOCAL_OWNER

from .approval_center import ApprovalCenter, DecisionRefused, Person

#: the tenant of everything an installation's own agents do (local_agents.TENANT)
TENANT = "local"
CLIENTS = ("generic", "gemini-cli", "claude-desktop", "local-model")
_WHY = "only the person who opened the installed app from its launcher: the session the local gate validated"


def _person(request: Request) -> Person:
    """The person at the app. A request without the gate's session is nobody, whatever else it carries."""
    if request.scope.get(SESSION_SCOPE_KEY) is None:
        raise HTTPException(401, {"error": "session_required", "detail": "open the app from its window: this needs the app's own session"})
    return Person(LOCAL_OWNER)


def _card(card: Any) -> Dict[str, Any]:
    out = dataclasses.asdict(card)
    out["changes"] = [{"label": k, "value": v} for k, v in card.changes]
    out["outcome"] = [{"label": k, "value": v} for k, v in card.outcome]
    out["ref"] = card.ref
    return out


def mount(app: Any, *, center: Optional[ApprovalCenter], agents: Optional[Any], app_store: Optional[Any],
          grantable: Callable[[], Mapping[str, str]]) -> Dict[str, RoutePolicy]:
    """Add the routes and return the policies of those that change something. ``agents`` is the local agent store (None:
    this installation connects none); ``grantable`` maps each permission this installation can give to what it means."""
    policies: Dict[str, RoutePolicy] = {}

    def changing(route: str, name: str, max_body: Optional[int] = None) -> None:
        extra = {} if max_body is None else {"max_body": max_body}
        policies[route] = RoutePolicy(name, "human_only", permission="writes:decide", rationale=_WHY, **extra)

    @app.get("/api/desktop")
    def _features(request: Request) -> Dict[str, Any]:
        _person(request)
        return {"approvals": center is not None, "agents": agents is not None, "applications": app_store is not None}

    # ── approvals: the shared center, as the signed-in pages use it ─────────────────────────────────────────────
    @app.get("/api/approvals")
    def _approvals(request: Request) -> Dict[str, Any]:
        person = _person(request)
        if center is None:
            return {"waiting": [], "recent": [], "problems": []}
        listing = center.listing(person)
        return {"waiting": [_card(c) for c in listing["waiting"]], "recent": [_card(c) for c in listing["recent"]],
                "problems": listing["problems"]}

    @app.get("/api/approvals/{source}/{record_id}")
    def _approval(source: str, record_id: str, request: Request) -> Dict[str, Any]:
        person = _person(request)
        if center is None:
            raise HTTPException(404, {"error": "not_found", "detail": "no such approval"})
        try:
            return _card(center.card(person, source, record_id))
        except DecisionRefused as e:
            raise HTTPException(e.status, {"error": "refused", "detail": e.detail}) from None

    @app.post("/api/approvals/{source}/{record_id}/decision")
    def _decide(source: str, record_id: str, request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        person = _person(request)
        decision, digest, attested = body.get("decision"), body.get("digest"), body.get("attested", False)
        if (set(body) - {"decision", "digest", "attested"} or decision not in ("approve", "reject", "execute")
                or not isinstance(digest, str) or not isinstance(attested, bool)):
            raise HTTPException(422, {"error": "invalid", "detail": "body: {decision: approve|reject, digest, attested?}"})
        if center is None:
            raise HTTPException(404, {"error": "not_found", "detail": "no such approval"})
        try:
            return _card(center.decide(person, source, record_id, decision=decision, digest=digest, attested=attested))
        except DecisionRefused as e:
            raise HTTPException(e.status, {"error": "refused", "detail": e.detail}) from None
    changing("POST /api/approvals/{source}/{record_id}/decision", "desktop.decide")

    # ── agents: the only place a permission that changes something is given ─────────────────────────────────────
    def _agent_store() -> Any:
        if agents is None:
            raise HTTPException(404, {"error": "not_found", "detail": "this installation connects no agents"})
        return agents

    @app.get("/api/local-agents")
    def _agents(request: Request) -> Dict[str, Any]:
        _person(request)
        from agentic_os import local_agents as L  # noqa: PLC0415
        store = _agent_store()
        return {"agents": [r.public() for r in store.list()], "clients": list(CLIENTS), "default_days": L.DEFAULT_DAYS,
                "max_days": L.MAX_DAYS, "gateway": L.discover_gateway(store.dir),
                "grantable": [{"grant": g, "meaning": text, "changes": g in L.WRITING} for g, text in grantable().items()]}

    @app.post("/api/local-agents")
    def _connect(request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _person(request)
        from agentic_os import local_agents as L  # noqa: PLC0415
        store = _agent_store()
        name, grants, days, client = body.get("name"), body.get("grants"), body.get("days", L.DEFAULT_DAYS), body.get("client", "generic")
        if (set(body) - {"name", "grants", "days", "client"} or not isinstance(name, str) or not isinstance(grants, list)
                or not all(isinstance(g, str) for g in grants) or isinstance(days, bool) or not isinstance(days, (int, float))
                or client not in CLIENTS):
            raise HTTPException(422, {"error": "invalid", "detail": "body: {name, grants[], days?, client?}"})
        try:
            made = L.connect_agent(name, grants=grants, client=client, days=days, allowed=tuple(grantable()))
        except L.LocalAgentError as e:
            raise HTTPException(422, {"error": "invalid", "detail": str(e)}) from None
        made["mcp_server"] = L.mcp_server_config(made["token_file"], str(store.dir))
        return made
    changing("POST /api/local-agents", "desktop.connect_agent")

    @app.post("/api/local-agents/{agent_id}/revoke")
    def _revoke(agent_id: str, request: Request) -> Dict[str, Any]:
        _person(request)
        from agentic_os import local_agents as L  # noqa: PLC0415
        _agent_store()
        if not L.disconnect_agent(agent_id):
            raise HTTPException(404, {"error": "not_found", "detail": "no connected agent with this id"})
        return {"revoked": True, "id": agent_id}
    changing("POST /api/local-agents/{agent_id}/revoke", "desktop.revoke_agent")

    # ── applications ────────────────────────────────────────────────────────────────────────────────────────────
    def _apps() -> Any:
        if app_store is None:
            raise HTTPException(404, {"error": "not_found", "detail": "applications are not enabled on this installation"})
        return app_store

    def _found(app_id: str) -> Dict[str, Any]:
        found = _apps().current(TENANT, app_id)
        if found is None:
            raise HTTPException(404, {"error": "not_found", "detail": "no such application"})
        return found

    @app.get("/api/applications")
    def _applications(request: Request) -> Dict[str, Any]:
        _person(request)
        return {"applications": [{"app_id": a["id"], "name": a["definition"]["name"], "version": a["version"],
                                  "summary": a["definition"]["summary"], "installed_by": a["installed_by"]}
                                 for a in _apps().installed(TENANT)]}

    @app.get("/api/applications/{app_id}")
    def _application(app_id: str, request: Request) -> Dict[str, Any]:
        _person(request)
        found, store = _found(app_id), _apps()
        collections = [c["name"] for c in found["definition"]["state"]["collections"]]
        return {"app_id": found["id"], "version": found["version"], "installed_by": found["installed_by"], "definition": found["definition"],
                "records": {name: store.records(TENANT, app_id, name, 200) for name in collections},
                "changed_capabilities": D.contract_changes(found["contracts"], store.catalog())}

    @app.post("/api/applications/{app_id}/collections/{collection}/records")
    def _add(app_id: str, collection: str, request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        person = _person(request)
        _found(app_id)
        if set(body) != {"values"} or not isinstance(body["values"], dict):
            raise HTTPException(422, {"error": "invalid", "detail": "body: {values: {...}}"})
        try:
            return _apps().add(TENANT, app_id, collection, body["values"], by=person.principal)
        except D.DefinitionError as e:
            raise HTTPException(422, {"error": "invalid", "detail": str(e)}) from None
    changing("POST /api/applications/{app_id}/collections/{collection}/records", "desktop.add_record")

    @app.get("/api/applications/{app_id}/export")
    def _export(app_id: str, request: Request) -> Response:
        person = _person(request)
        _found(app_id)
        bundle = _apps().export_bundle(TENANT, app_id, by=person.principal)
        name = f"{bundle['definition']['id']}-v{bundle['definition']['version']}.app-bundle.json"      # both validated: safe in a header
        return Response(json.dumps(bundle, indent=1, ensure_ascii=False), media_type="application/json",
                        headers={"content-disposition": f'attachment; filename="{name}"'})

    def _checked(body: Dict[str, Any]) -> "tuple[dict, dict, dict]":
        store = _apps()
        try:
            bundle = body.get("bundle")
            definition, state = D.check_bundle(bundle, store.catalog())
            if store.current(TENANT, definition["id"]) is not None or store.counts(TENANT, definition["id"]):
                raise D.DefinitionError(f"this installation already has {definition['id']}: an import never overwrites")
        except (ValueError, TypeError, RecursionError) as e:                            # DefinitionError is a ValueError
            reason = str(e) if isinstance(e, D.DefinitionError) else "that is not an exported application"
            raise HTTPException(422, {"error": "invalid", "detail": f"not brought over: {reason}"}) from None
        return bundle, definition, state

    @app.post("/api/applications-import/review")
    def _review(request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _person(request)
        bundle, definition, state = _checked(body)
        rows: List[List[str]] = D.describe(definition, None, TENANT, _apps().catalog(), {})
        counts = ", ".join(f"{name}: {len(r)}" for name, r in sorted(state.items())) or "none"
        rows.insert(2, ["What arrives with it", f"Records ({counts}). Who last changed each is what the other installation said and is "
                                                f"marked as such. Exported from tenant {bundle['from_tenant']} by {bundle['exported_by']} "
                                                "(as the file says; not verified)."])
        return {"name": definition["name"], "digest": bundle["content_sha256"], "rows": [{"label": k, "value": v} for k, v in rows],
                "technical": json.dumps(definition, indent=2, sort_keys=True, ensure_ascii=False)}
    changing("POST /api/applications-import/review", "desktop.review_import", max_body=D.MAX_BUNDLE_BYTES + 200_000)

    @app.post("/api/applications-import")
    def _bring(request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        person = _person(request)
        bundle, definition, _ = _checked(body)
        if body.get("digest") != bundle["content_sha256"]:
            raise HTTPException(409, {"error": "conflict", "detail": "what you confirmed is not what was sent: review it again"})
        try:
            _apps().import_bundle(TENANT, bundle, by=person.principal)
        except D.DefinitionError as e:
            raise HTTPException(409, {"error": "conflict", "detail": f"not brought over: {e}"}) from None
        return {"installed": True, "app_id": definition["id"]}
    changing("POST /api/applications-import", "desktop.import_application", max_body=D.MAX_BUNDLE_BYTES + 200_000)

    return policies


__all__ = ["mount", "TENANT", "CLIENTS"]
