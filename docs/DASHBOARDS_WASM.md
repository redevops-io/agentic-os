# Dashboards in a WebAssembly distribution

The WASM distribution does not ship the Projects UI today. That is a packaging fact, not a technical limit. This page
records what was measured, which dashboards work in which arrangement, and what is still missing to ship one.

## Two arrangements

**A. Static UI, remote API.** The Projects UI is a static bundle. Built with `VITE_PROJECTS_API=<url>` it calls a Projects
API on another origin, and that API admits the page's origin with `PROJECTS_CORS_ORIGINS=<origin>[,<origin>]`. Every
dashboard is a projection served by `/api/dashboards/*`, so **all 14 work** this way, with the same numbers, drill-downs,
filters and saved views as a served deployment. Nothing runs locally except the page.

Limits of A: the caller authenticates with a bearer token (the CORS configuration allows headers, not cookies, so a
cookie session from another origin is not accepted); behind the operator gate the token must be a control-plane token;
the API must be reachable from the browser.

**B. Kernel in the browser.** The projection kernel (`agentic_os.dashboards`: config, store, sources, builders, service)
runs in WebAssembly Python (Pyodide 0.27.7) with two packages beside the standard library: `pyyaml` and `sqlite3`. No web
framework and no HTTP client is imported. `packaging/wasm/dashboards_probe.mjs` proves it and fails if that stops being
true:

    cd <any directory> && npm install pyodide@0.27.7 && node <repo>/packaging/wasm/dashboards_probe.mjs [junit.xml]

Measured (Apple M-series, node 24): the kernel imports, parses a JUnit file, ingests it and builds every dashboard (13 when measured) in
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
| Revenue | yes | no | CRM (Twenty) API key, optionally Metabase |
| Finance | yes | no | ERPNext / Lago keys |
| Procurement | yes | no | ERPNext key |
| Support | yes | no | Chatwoot token |
| Marketing | yes | no | Postiz / Listmonk credentials |
| Market | yes | no | Umami credentials |
| AI costs | yes | no | the Mission Runtime's record of executed steps |
| Knowledge | yes | no | the runtime's connected context sources |

"No" in column B is not a rendering problem: in the probe each of those nine builds and explains what it needs. They read
applications whose credentials are server-side secrets and whose APIs do not admit browser origins. Putting those keys in
a page would hand them to anyone who opens it, so in B they stay unavailable unless a local service or relay holds the
credentials — which is arrangement A.

## What is shipped

* `agentic_os/dashboards/portable.py`: the dashboards' routes as one function (`PortableDashboards.handle(method, path, query,
  body)`), for a host with no web framework. `GET /api/dashboards/_portable` lists what is available and, for each of the
  nine application dashboards, what it needs and why it is not available in a page.
* `packaging/wasm/build_kernel_zip.py`: the reproducible kernel archive (about 90 KB, 20 modules) a page loads into Pyodide.
  It is a release asset (`dashboards-kernel.zip`) from v0.4.15.
* `packaging/wasm/dashboards_probe.mjs`: runs that archive in Pyodide and checks the rules.
* The page itself (the Projects dashboards UI with a remote mode and a standalone mode) is built in
  `redevops-io/redevops-projects` (`npm run build:portable`) and published as the release asset
  `projects-dashboards-portable.tgz`.

In the page, a remote deployment is reached with a bearer token the person types; it is kept in the tab's session storage
and sent only to the deployment whose address they entered. Standalone results are kept in the browser (IndexedDB).
