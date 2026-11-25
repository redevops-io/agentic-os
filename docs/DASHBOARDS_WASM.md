# Dashboards in a WebAssembly distribution

The WASM distribution does not ship the Projects UI today. That is a packaging fact, not a technical limit. This page
records what was measured, which dashboards work in which arrangement, and what is still missing to ship one.

## Two arrangements

**A. Static UI, remote API.** The Projects UI is a static bundle. Built with `VITE_PROJECTS_API=<url>` it calls a Projects
API on another origin, and that API admits the page's origin with `PROJECTS_CORS_ORIGINS=<origin>[,<origin>]`. Every
dashboard is a projection served by `/api/dashboards/*`, so **all 13 work** this way, with the same numbers, drill-downs,
filters and saved views as a served deployment. Nothing runs locally except the page.

Limits of A: the caller authenticates with a bearer token (the CORS configuration allows headers, not cookies, so a
cookie session from another origin is not accepted); behind the operator gate the token must be a control-plane token;
the API must be reachable from the browser.

**B. Kernel in the browser.** The projection kernel (`agentic_os.dashboards`: config, store, sources, builders, service)
runs in WebAssembly Python (Pyodide 0.27.7) with two packages beside the standard library: `pyyaml` and `sqlite3`. No web
framework and no HTTP client is imported. `packaging/wasm/dashboards_probe.mjs` proves it and fails if that stops being
true:

    cd <any directory> && npm install pyodide@0.27.7 && node <repo>/packaging/wasm/dashboards_probe.mjs [junit.xml]

Measured (Apple M-series, node 24): the kernel imports, parses a JUnit file, ingests it and builds all 13 dashboards in
about 0.2 s after the interpreter has loaded. With a 69-test file reported for one of two declared platforms: 138 planned,
69 not executed, coverage 38.4 %, and the drill-down for "not executed" lists exactly 69 tests. The rules are the served
ones because it is the same code.

One change was needed: WebAssembly Python cannot start threads, and the source cache refreshed sources on background
threads. It now reads in the caller when a thread cannot be started (`SourceCache._refresh_behind`, `SourceCache.warm`).

## Which dashboard needs what

| Dashboard | A: remote API | B: in the browser, no server | What B is missing |
|---|---|---|---|
| Portfolio | yes | yes | — |
| QA | yes | yes, from results ingested locally | — |
| Missions | yes | yes, empty until a Mission Plane is reachable | a runtime to read missions from |
| Infrastructure | yes | yes, shows that nothing can be inspected | host inspection is a local service |
| Security | yes | no | Edge Sentinel API + its token |
| Revenue | yes | no | CRM (Twenty) API key |
| Finance | yes | no | ERPNext / Lago keys |
| Procurement | yes | no | ERPNext key |
| Support | yes | no | Chatwoot token |
| Marketing | yes | no | Umami / Postiz / Listmonk credentials |
| Market | yes | no | the kernel's market store |
| AI costs | yes | no | the runtime's usage ledger |
| Knowledge | yes | no | Context Runtime |

"No" in column B is not a rendering problem: in the probe each of those nine builds and explains what it needs. They read
applications whose credentials are server-side secrets and whose APIs do not admit browser origins. Putting those keys in
a page would hand them to anyone who opens it, so in B they stay unavailable unless a local service or relay holds the
credentials — which is arrangement A.

## What shipping it takes

* A: a build of the UI with `VITE_PROJECTS_API`, a sign-in step that stores a bearer token, and the origin in
  `PROJECTS_CORS_ORIGINS`. No kernel change.
* B: an adapter in the page that answers the UI's `/api/dashboards/*` calls from the in-browser service (the UI and the
  kernel exist; the adapter does not), and persistence for the store (the probe uses an in-memory database; a browser
  needs OPFS or IndexedDB behind sqlite). Not built.

Neither is in a published WASM artifact yet, so no public page should say dashboards are available there.
