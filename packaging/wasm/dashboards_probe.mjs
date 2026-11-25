// Proof that the dashboards' projection kernel runs in WebAssembly Python (Pyodide), i.e. in a browser tab with no server:
// it parses a JUnit file, ingests it into an in-memory store and builds dashboards with the same rules as a served deployment.
//
//   cd <any directory> && npm install pyodide@0.27.7 && node <repo>/packaging/wasm/dashboards_probe.mjs [junit.xml]
//
// Only two packages are loaded next to the standard library: pyyaml and sqlite3. The script fails if a web framework or an
// HTTP client gets imported, if a thread is needed, or if the numbers differ from what the rules require. Exit 0 = it holds.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// pyodide is resolved from the directory the probe is run in, so the repository needs no node_modules of its own
const { loadPyodide } = createRequire(resolve(process.cwd(), "probe.js"))("pyodide");
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const junit = readFileSync(process.argv[2] || resolve(root, "packaging/wasm/sample-junit.xml"), "utf8");
const py = await loadPyodide({ stdout: () => {}, stderr: () => {} });
py.FS.mkdirTree("/repo");
py.FS.mount(py.FS.filesystems.NODEFS, { root }, "/repo");
await py.loadPackage(["pyyaml", "sqlite3"], { messageCallback: () => {} });
py.globals.set("JUNIT", junit);
const out = JSON.parse(await py.runPythonAsync(`
import sys, json, time
sys.path.insert(0, "/repo")
t0 = time.time()
from agentic_os.dashboards.config import parse_config
from agentic_os.dashboards.store import DashboardStore
from agentic_os.dashboards.service import DashboardService, DEFINITIONS
from agentic_os.dashboards.contracts import Filters
from agentic_os.dashboards import report_junit
open("/tmp/r.xml", "w").write(JUNIT)
results = report_junit.parse_junit("/tmp/r.xml")

class LocalProvider:                       # what a browser-only installation has: no runtime, no products, no applications
    serves_fixtures = False
    def projects(self): return [{"id": "local", "name": "This browser"}]
    def attention(self, pid): return []
    def missions(self, pid): return []
    def sources(self, pid): return []
    def runtime(self, pid): return {"runtimes": [], "models": []}

cfg = parse_config({"schema": "aos.dashboards/v1", "projects": [{"id": "local", "name": "This browser"}],
                    "qa": {"suites": [{"id": "suite", "project": "local", "name": "Reported suite", "platforms": ["here", "elsewhere"]}]}})
svc = DashboardService(LocalProvider(), config=cfg, store=DashboardStore(""))
svc.store.ingest_qa({"project": "local", "suite": "suite", "run_id": "wasm-1", "platform": "here", "results": results}, by="browser")
acc = svc.access("local-owner")
qa = svc.build("qa", Filters(days=30), acc).to_dict()
m = {x["id"]: x["value"] for p in qa["panels"] for x in p["metrics"]}
each = {}
for df in DEFINITIONS:
    d = svc.build(df.id, Filters(days=30), acc).to_dict()
    each[df.id] = "needs an application" if d["empty"] else "%d panels" % len(d["panels"])
json.dumps({"results": len(results), "statuses": sorted({r["status"] for r in results}), "offered": [d["id"] for d in svc.catalog(acc)["dashboards"]],
            "qa": {k: m[k] for k in ("planned", "coverage", "passed" if "passed" in m else "pass_rate", "failed", "blocked", "unexecuted", "unsupported")},
            "drill_unexecuted": svc.drill("qa", "cells:unexecuted", Filters(days=30), acc)["total"], "each": each, "seconds": round(time.time() - t0, 2),
            "heavy": sorted(k for k in sys.modules if k.split(".")[0] in ("fastapi", "httpx", "pydantic", "starlette", "uvicorn", "cryptography", "requests"))})
`));
const fail = [];
if (out.heavy.length) fail.push(`a server-side dependency was imported: ${out.heavy}`);
if (out.qa.planned !== 2 * out.results) fail.push(`planned should be every reported test on both declared platforms (${2 * out.results}), got ${out.qa.planned}`);
if (out.qa.unexecuted !== out.results || out.drill_unexecuted !== out.results) fail.push("the platform nobody ran must read as not executed, and its drill-down must list exactly those tests");
if (!out.offered.includes("qa") || !out.offered.includes("portfolio")) fail.push(`QA and portfolio must be offered locally, got ${out.offered}`);
console.log(JSON.stringify(out, null, 1));
if (fail.length) { console.error("FAIL\n- " + fail.join("\n- ")); process.exit(1); }
console.log("OK: the dashboards kernel runs in WebAssembly Python");
