"""The views of installed applications (``agentic_os.app_definitions``), for a signed-in person.

Generated from the definition on the server, with no script: every title, record and label is escaped, the pages carry the
approval pages' content policy, and they live under the approval pages so that the same signed-in session, CSRF token
and origin check guard them. A person who may decide can also export an application with its records, and bring an exported
one into their tenant after seeing what it is (never over something that exists). A person sees their own tenant's applications (a deployment operator may name one with
``?tenant=``). A requester may look; adding a record needs someone who may decide. What an application proposes to change
is decided on the ordinary approval card the view links to.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from agentic_os import app_definitions as D
from agentic_os.governed_writes.authorization import tenant_may_use_app

from .approval_center import WRITE_PERMISSION, Person

PREFIX = "/approvals/apps"
TRANSFER = "/approvals/apps-transfer"
MAX_FORM_FIELDS = 30
#: a pasted bundle: form encoding grows JSON up to about three times
MAX_BUNDLE_FORM = 3 * D.MAX_BUNDLE_BYTES + 200_000


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
                       f"{fields}<button>{_e(block['submit'])}</button>"
                       # the runtime's own words: whatever the author wrote around the form, this is what the button does
                       '<p class=muted>Platform note: this button saves a record in this application at once. No approval is involved. '
                       'Changes to other applications wait for a person under Approvals.</p></form>')
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

    def may_move(person: Person, tenant: str) -> str:
        """Why this person may not take an application out of, or bring one into, this tenant; empty when they may: someone
        who may decide the tenant's changes, and the tenant is granted portable applications."""
        if person.role == "requester":
            return "a requester does not move applications"
        why = person.refusal(WRITE_PERMISSION)
        if why:
            return why
        if not tenant_may_use_app(tenant, "apps"):
            return f"the tenant {tenant or '(none)'} is not granted portable applications here"
        return ""

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
        move = "" if may_move(Person(session.principal), tenant) else \
            f'<p><a href="{_e(pages._base(request) + TRANSFER + query)}">Bring an application from another deployment</a></p>'
        return _page("Applications", f'<p><a href="{_e(pages._base(request))}/approvals">&larr; Approvals</a></p><h1>Applications</h1>'
                     + (body or "<p class=muted>No application is installed for this tenant.</p>") + move)

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
        export = "" if may_move(person, tenant) else (f'<p><a href="{_e(pages._base(request) + PREFIX + "/" + found["id"] + "/export" + query)}">'
                                                      "Export this application with its records</a></p>")
        return _page(found["definition"]["name"], view_html(found, chosen, store=store, tenant=tenant, writes=writes, base=pages._base(request),
                                                           csrf=session.csrf, may_write=person.role != "requester", query=query) + export)

    # ── moving an application with its records: a person who may decide, for their tenant ──
    @app.get(PREFIX + "/{app_id}/export")
    def _export(app_id: str, request: Request):
        session = pages._session(request)
        if session is None:
            return pages._to_signin(request, "/approvals")
        person = Person(session.principal)
        tenant, _ = tenant_of(request, person)
        why = may_move(person, tenant)
        if why:
            raise HTTPException(403, {"error": "refused", "detail": why})
        try:
            bundle = store.export_bundle(tenant, app_id, by=person.principal)
        except D.DefinitionError as e:
            return _page("Not found", f"<p>{_e(e)}</p>", 404)
        name = f"{bundle['definition']['id']}-v{bundle['definition']['version']}.app-bundle.json"      # both validated: safe in a header
        return Response(json.dumps(bundle, indent=1, ensure_ascii=False), media_type="application/json",
                        headers={**HEADERS, "content-disposition": f'attachment; filename="{name}"'})

    def paste_form(request: Request, query: str, csrf: str, message: str = "", status: int = 200):
        note = f"<p class=note>{_e(message)}</p>" if message else ""
        return _page("Bring an application", f'<p><a href="{_e(pages._base(request) + PREFIX + query)}">&larr; All applications</a></p>'
                     f"<h1>Bring an application from another deployment</h1>{note}<p>Paste the exported file. Nothing is installed "
                     "until you have seen what it is and confirmed.</p>"
                     f'<form method="post" action="{_e(pages._base(request) + TRANSFER + "/review" + query)}">'
                     f'<input type="hidden" name="csrf" value="{_e(csrf)}"><textarea name="bundle" rows="14" style="width:100%" required></textarea>'
                     "<button>Review</button></form>", status)

    @app.get(TRANSFER)
    def _paste(request: Request):
        session = pages._session(request)
        if session is None:
            return pages._to_signin(request, "/approvals")
        tenant, query = tenant_of(request, Person(session.principal))
        why = may_move(Person(session.principal), tenant)
        if why:
            raise HTTPException(403, {"error": "refused", "detail": why})
        return paste_form(request, query, session.csrf)

    async def submitted(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BUNDLE_FORM:
            raise HTTPException(413, {"error": "too_large", "detail": "the exported file is too large to bring over here"})
        try:
            form = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=4))
        except ValueError:
            raise HTTPException(422, {"error": "invalid", "detail": "too many fields"}) from None
        session = pages.require_session_post(request, form)
        person = Person(session.principal)
        tenant, query = tenant_of(request, person)
        why = may_move(person, tenant)
        if why:
            raise HTTPException(403, {"error": "refused", "detail": why})
        try:
            bundle = json.loads(form.get("bundle", ""))
            definition, state = D.check_bundle(bundle, store.catalog())
            if store.current(tenant, definition["id"]) is not None or store.counts(tenant, definition["id"]):
                raise D.DefinitionError(f"this tenant already has {definition['id']}: an import never overwrites; install a newer version instead")
        except (ValueError, TypeError, RecursionError) as e:                       # DefinitionError is a ValueError
            reason = str(e) if isinstance(e, D.DefinitionError) else "that is not an exported application"
            return None, paste_form(request, query, session.csrf, f"Not brought over: {reason}", 422)
        return (session, person, tenant, query, form, bundle, definition, state), None

    @app.post(TRANSFER + "/review")
    async def _review(request: Request):
        ok, refused = await submitted(request)
        if refused is not None:
            return refused
        session, person, tenant, query, form, bundle, definition, state = ok
        rows = D.describe(definition, None, tenant, store.catalog(), {})
        counts = ", ".join(f"{name}: {len(r)}" for name, r in sorted(state.items())) or "none"
        rows.insert(2, ["What arrives with it", f"Records ({counts}). Who last changed each is what the other deployment said and is "
                                                f"marked as such. Exported from tenant {bundle['from_tenant']} by {bundle['exported_by']} "
                                                "(as the file says; not verified)."])
        table = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows)
        folded = json.dumps(definition, indent=2, sort_keys=True, ensure_ascii=False)
        return _page(f"Bring {definition['name']}", f"<h1>Bring the application {_e(definition['name'])}</h1><table>{table}</table>"
                     f"<details><summary>Full technical definition (exactly what is installed)</summary><pre>{_e(folded)}</pre></details>"
                     f"<p class=muted>Content digest {_e(bundle['content_sha256'])}</p>"
                     f'<form method="post" action="{_e(pages._base(request) + TRANSFER + query)}"><input type="hidden" name="csrf" value="{_e(session.csrf)}">'
                     f'<input type="hidden" name="digest" value="{_e(bundle["content_sha256"])}">'
                     f'<input type="hidden" name="bundle" value="{_e(form["bundle"])}"><button>Install with these records</button></form>'
                     f'<p><a href="{_e(pages._base(request) + PREFIX + query)}">Cancel</a></p>')

    @app.post(TRANSFER)
    async def _bring(request: Request):
        ok, refused = await submitted(request)
        if refused is not None:
            return refused
        session, person, tenant, query, form, bundle, definition, state = ok
        if form.get("digest", "") != bundle["content_sha256"]:
            return paste_form(request, query, session.csrf, "Not brought over: what you confirmed is not what was sent. Review it again.", 409)
        try:
            store.import_bundle(tenant, bundle, by=person.principal)
        except D.DefinitionError as e:
            return paste_form(request, query, session.csrf, f"Not brought over: {e}", 409)
        return RedirectResponse(f"{pages._base(request)}{PREFIX}/{quote(definition['id'])}{query}", 303, headers=HEADERS)

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

    why = "requires a live signed-in session, its CSRF token, the same origin and a person who may decide for the tenant"
    return {f"POST {PREFIX}/{{app_id}}/records": RoutePolicy("apps.add_record", "operator_boundary", rationale=why),
            f"POST {TRANSFER}/review": RoutePolicy("apps.review_import", "operator_boundary", rationale=why, max_body=MAX_BUNDLE_FORM),
            f"POST {TRANSFER}": RoutePolicy("apps.import", "operator_boundary", rationale=why, max_body=MAX_BUNDLE_FORM)}


__all__ = ["PREFIX", "mount", "view_html"]
