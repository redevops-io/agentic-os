"""The views of installed applications (``agentic_os.app_definitions``), for a signed-in person.

Generated from the definition on the server, with no script: every title, record and label is escaped, the pages carry the
approval pages' content policy, and they live under the approval pages so that the same signed-in session, CSRF token
and origin check guard them. A person sees their own tenant's applications (a deployment operator may name one with
``?tenant=``). A requester may look; adding a record needs someone who may decide. What an application proposes to change
is decided on the ordinary approval card the view links to.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from agentic_os import app_definitions as D

from .approval_center import Person

PREFIX = "/approvals/apps"
MAX_FORM_FIELDS = 30


def _e(value: Any) -> str:
    from .approval_pages import _e as escape  # noqa: PLC0415
    return escape(value)


def _shown(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else "" if value is None else str(value)


def _input(field: Dict[str, Any]) -> str:
    name, kind = _e("f_" + field["name"]), field["type"]
    need = " required" if field.get("required") and kind != "boolean" else ""
    label = _e(field["name"].replace("_", " "))
    if kind == "long_text":
        control = f'<textarea name="{name}" rows="3" style="width:100%"{need}></textarea>'
    elif kind == "choice":
        control = f'<select name="{name}"{need}><option value=""></option>' + "".join(f"<option>{_e(c)}</option>" for c in field["choices"]) + "</select>"
    elif kind == "boolean":
        control = f'<input type="checkbox" name="{name}" value="yes" style="width:auto">'
    else:
        control = f'<input type="{ {"number": "number", "date": "date"}.get(kind, "text")}" name="{name}"{" step=any" if kind == "number" else ""}{need}>'
    return f"<label>{label}<br>{control}</label><br>"


def view_html(app: Dict[str, Any], view: Dict[str, Any], *, store: D.AppStore, tenant: str, writes: Any, base: str, csrf: str,
              may_write: bool, query: str) -> str:
    definition = app["definition"]
    collections = {c["name"]: c for c in definition["state"]["collections"]}
    here = f"{base}{PREFIX}/{quote(app['id'])}"
    out = [f'<p><a href="{_e(base + PREFIX + query)}">&larr; All applications</a></p>', f"<h1>{_e(definition['name'])}</h1>",
           f"<p class=muted>{_e(definition['summary'])} · version {app['version']} · installed by {_e(app['installed_by'])}</p>"]
    changed = D.contract_changes(app["contracts"], store.catalog())
    if changed:
        out.append("<p class=note>Some of what this application uses changed since it was approved: "
                   + _e("; ".join(f"{c['capability']}: {c['change']}" for c in changed)) + ". Install a new version to approve it.</p>")
    if len(definition["views"]) > 1:
        out.append("<p>" + " · ".join(f'<a href="{_e(here)}?view={_e(v["id"])}{_e(query.replace("?", "&"))}">{_e(v["title"])}</a>'
                                      for v in definition["views"]) + "</p>")
    out.append(f"<h2>{_e(view['title'])}</h2>")
    for block in view["blocks"]:
        if block["type"] == "text":
            out.append(f"<p style='white-space:pre-wrap'>{_e(block['text'])}</p>")
        elif block["type"] == "records":
            rows = store.records(tenant, app["id"], block["collection"], 100)
            head = "".join(f"<th>{_e(c.replace('_', ' '))}</th>" for c in block["columns"])
            body = "".join("<tr>" + "".join(f"<td>{_e(_shown(r['values'].get(c)))}</td>" for c in block["columns"])
                           + f"<td class=muted>{_e(r['updated_by'])}</td></tr>" for r in rows)
            out.append(f"<table class=tables><tr>{head}<th>updated by</th></tr>{body}</table>" if rows else
                       f"<p class=muted>No {_e(block['collection'].replace('_', ' '))} yet.</p>")
        elif block["type"] == "form":
            if not may_write:
                out.append("<p class=muted>Adding records needs someone who may decide.</p>")
                continue
            fields = "".join(_input(f) for f in collections[block["collection"]]["fields"])
            out.append(f'<form method="post" action="{_e(here)}/records{_e(query)}" class=card><input type="hidden" name="csrf" value="{_e(csrf)}">'
                       f'<input type="hidden" name="collection" value="{_e(block["collection"])}"><input type="hidden" name="view" value="{_e(view["id"])}">'
                       f"{fields}<button>{_e(block['submit'])}</button></form>")
        else:                                                                 # approvals
            used = {c["name"] for c in definition["capabilities"]}
            waiting = [v for v in (writes.list(tenant=tenant, state="pending", limit=100) if writes is not None else []) if v["action"] in used]
            out.append("<h3>Waiting for a decision</h3>" + ("".join(
                f'<div class=card><a href="{_e(base)}/approvals/writes/{_e(v["request_id"])}">{_e(v["summary"])}</a><br>'
                f'<span class=muted>{_e(v["requested_by"])}</span></div>' for v in waiting) or "<p class=muted>Nothing from this application is waiting.</p>"))
    return "".join(out)


def mount(pages: Any, app: Any, store: D.AppStore, writes: Any) -> Dict[str, Any]:
    """Add the application views to the approval pages (call before their card route, which would otherwise match)."""
    from agentic_os.app_kit.route_authz import RoutePolicy  # noqa: PLC0415

    from .approval_pages import HEADERS, _page  # noqa: PLC0415

    def tenant_of(request: Request, person: Person) -> "tuple[str, str]":
        if person.tenant is not None:
            return person.tenant, ""
        named = request.query_params.get("tenant", "")
        return named, (f"?tenant={quote(named)}" if named else "")

    @app.get(PREFIX)
    def _apps(request: Request):
        session = pages._session(request)
        if session is None:
            return pages._to_signin(request, "/approvals")
        tenant, query = tenant_of(request, Person(session.principal))
        rows = store.installed(tenant)
        body = "".join(f'<div class=card><a href="{_e(pages._base(request) + PREFIX + "/" + a["id"] + query)}"><b>{_e(a["definition"]["name"])}</b></a>'
                       f'<br><span class=muted>{_e(a["definition"]["summary"])} · version {a["version"]}</span></div>' for a in rows)
        return _page("Applications", f'<p><a href="{_e(pages._base(request))}/approvals">&larr; Approvals</a></p><h1>Applications</h1>'
                     + (body or "<p class=muted>No application is installed for this tenant.</p>"))

    @app.get(PREFIX + "/{app_id}")
    def _app(app_id: str, request: Request, view: str = ""):
        session = pages._session(request)
        if session is None:
            return pages._to_signin(request, f"{PREFIX}/{app_id}" if app_id.replace("-", "").isalnum() else "/approvals")
        person = Person(session.principal)
        tenant, query = tenant_of(request, person)
        found = store.current(tenant, app_id)
        if found is None:
            return _page("Not found", "<p>no such application</p>", 404)
        views = found["definition"]["views"]
        chosen = next((v for v in views if v["id"] == view), views[0])
        return _page(found["definition"]["name"], view_html(found, chosen, store=store, tenant=tenant, writes=writes, base=pages._base(request),
                                                           csrf=session.csrf, may_write=person.role != "requester", query=query))

    @app.post(PREFIX + "/{app_id}/records")
    async def _add(app_id: str, request: Request):
        raw = await request.body()
        try:
            form = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS))
        except ValueError:
            raise HTTPException(422, {"error": "invalid", "detail": "too many fields"}) from None
        session = pages.require_session_post(request, form)
        person = Person(session.principal)
        if person.role == "requester":
            raise HTTPException(403, {"error": "refused", "detail": "a requester does not change an application's records"})
        tenant, query = tenant_of(request, person)
        try:
            collection = store.collection(tenant, app_id, form.get("collection", ""))
            values: Dict[str, Any] = {}
            for f in collection["fields"]:
                text = form.get("f_" + f["name"], "")
                if f["type"] == "boolean":
                    values[f["name"]] = text == "yes"
                elif text != "":
                    values[f["name"]] = float(text) if f["type"] == "number" else text.replace("\r\n", "\n")
            store.add(tenant, app_id, collection["name"], values, by=person.principal)
        except (D.DefinitionError, ValueError) as e:
            return _page("Not added", f"<p class=note>Not added: {_e(e)}</p><p>Go back and correct it.</p>", 422)
        back = f"{pages._base(request)}{PREFIX}/{quote(app_id)}?view={quote(form.get('view', ''))}" + query.replace("?", "&")
        return RedirectResponse(back, 303, headers=HEADERS)

    return {f"POST {PREFIX}/{{app_id}}/records": RoutePolicy(
        "apps.add_record", "operator_boundary",
        rationale="requires a live signed-in session, its CSRF token, the same origin and a person who may decide")}


__all__ = ["PREFIX", "mount", "view_html"]
