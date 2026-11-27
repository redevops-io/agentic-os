# Portable application definitions (`aos.app-definition/v1`)

An application as **data** that any agent, model or MCP client can read and write, installed and run by this runtime.
No code, no HTML, no credential, no vendor or artifact format.

```json
{"schema": "aos.app-definition/v1", "id": "collections-desk", "name": "Collections desk", "version": 1,
 "summary": "Track which overdue invoices are being chased, and propose comments on them.",
 "capabilities": [{"name": "erp.find_records", "why": "to find overdue invoices"},
                  {"name": "erp.request_comment", "why": "to propose a follow-up note on one"}],
 "state": {"collections": [{"name": "chases", "max_records": 500, "fields": [
   {"name": "invoice", "type": "text", "required": true},
   {"name": "status", "type": "choice", "choices": ["new", "chased", "paid"], "required": true},
   {"name": "due", "type": "date"}]}]},
 "views": [{"id": "main", "title": "Chasing", "blocks": [
   {"type": "text", "text": "Who we are chasing."},
   {"type": "records", "collection": "chases", "columns": ["invoice", "status", "due"]},
   {"type": "form", "collection": "chases", "submit": "Add"},
   {"type": "approvals"}]}]}
```

Turn on with `AGENTIC_OS_APPS=1` and `AGENTIC_OS_WRITES_DIR`; the tenant needs `app:apps`.

| Capability | Grant |
|---|---|
| `apps.list`, `apps.get`, `apps.state.list`, `apps.state.add`, `apps.state.update` | `apps.use` |
| `apps.request_install` (a **proposal**), `apps.request_status` | `apps.install` |

- **Installing is a person's decision** about that exact definition (its SHA-256 is part of the approval). The card lists
  each capability, whether it reads or proposes changes, the permissions an agent needs, and the state and views. It can
  never be preauthorized (developer mode).
- **Installing grants nothing.** The permissions an app needs are derived from its capabilities; each call is authorized
  exactly as without the app. `apps.get` tells an agent which it lacks.
- **Typed, pinned capabilities.** Each capability's input schema and effect are hashed at install. If one changes or goes
  away, `apps.get` and the view name it, and a pending install of a changed capability fails.
- **Author text is not checked.** An author, often a model, can write anything in a text block or on a button, including
  claims that are false (in a qualification run a model wrote that its form waited for approval; it does not). The install
  card shows every such text and label with what each button really does, and every form carries the platform's own note.
- **State** is the runtime's, per tenant: typed fields, size limits, a revision per record, retry-safe adds. A newer
  version keeps the records, including values of fields and collections it no longer has (changing a record leaves them
  in place). The card of a newer version says what it hides or restricts for records already kept.
- **Moving an application** is a person's action on these pages, not an agent's tool. Someone who may decide for the
  tenant exports it (`aos.app-bundle/v1`: the installed version and every record, with a digest of the content), and on
  the other deployment or tenant brings it in: the pages first say in words what it is and what arrives with it, and
  install only the content that was reviewed, all of it or nothing. The definition is judged against what **that**
  deployment offers, and an import never overwrites an application or records already there. Who last changed each
  record is what the other deployment said and is marked "(as exported)". The digest detects damage, not authorship: a
  bundle is not signed, so import only a file whose origin you trust. Up to 2 MB of records.
- **Views** are generated on the server with no script under the signed-in approval pages (`/approvals/apps/<id>`), with
  the same session, CSRF token, origin check and content policy. The `approvals` block links to the pending changes the
  app's capabilities proposed. A requester may look; adding a record needs someone who may decide.

Not in v1: charts or computed figures in views, views that call capabilities themselves, per-record permissions, deleting
records or uninstalling, signed bundles, and importing a definition from another artifact format.
