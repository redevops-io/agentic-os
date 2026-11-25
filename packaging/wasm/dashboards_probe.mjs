// Proof that the dashboards run in WebAssembly Python (Pyodide), i.e. in a browser tab with no server, from the kernel ARCHIVE
// alone (packaging/wasm/build_kernel_zip.py): it imports the archive, adds a JUnit file and reads the dashboards back through
// agentic_os.dashboards.portable, the same service a served deployment uses.
//
//   python packaging/wasm/build_kernel_zip.py /tmp/kernel.zip
//   cd <any directory> && npm install pyodide@0.27.7 && node <repo>/packaging/wasm/dashboards_probe.mjs /tmp/kernel.zip [junit.xml]
//
// Only two packages are loaded next to the standard library: pyyaml and sqlite3. The script fails if a web framework or an
// HTTP client gets imported, if a thread is needed, or if the numbers differ from what the rules require. Exit 0 = it holds.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// pyodide is resolved from the directory the probe is run in, so the repository needs no node_modules of its own
const { loadPyodide } = createRequire(resolve(process.cwd(), "probe.js"))("pyodide");
const here = dirname(fileURLToPath(import.meta.url));
if (!process.argv[2]) { console.error("usage: node dashboards_probe.mjs <kernel.zip> [junit.xml]"); process.exit(64); }
const kernel = readFileSync(process.argv[2]);
const junit = readFileSync(process.argv[3] || resolve(here, "sample-junit.xml"), "utf8");
const py = await loadPyodide({ stdout: () => {}, stderr: () => {} });
await py.loadPackage(["pyyaml", "sqlite3"], { messageCallback: () => {} });
py.FS.writeFile("/kernel.zip", kernel);
py.FS.mkdirTree("/data");
py.globals.set("JUNIT", junit);
const out = JSON.parse(await py.runPythonAsync(`
import sys, json, time
sys.path.insert(0, "/kernel.zip")
t0 = time.time()
from agentic_os.dashboards.portable import PortableDashboards
api = PortableDashboards(data_dir="/data")
s, added = api.handle("POST", "/api/dashboards/qa/junit", body={"project": "local", "suite": "suite", "platform": "here", "platforms": ["here", "elsewhere"], "xml": JUNIT})
assert s == 200, added
about = api.handle("GET", "/api/dashboards/_portable")[1]
qa = api.handle("GET", "/api/dashboards/qa", {"days": "30"})[1]
m = {x["id"]: x for p in qa["panels"] for x in p["metrics"]}
drill = api.handle("GET", "/api/dashboards/qa/drill", {"key": m["unexecuted"]["drill"]})[1]["total"]
each = {}
for d in about["available"] + about["unavailable"]:
    s, body = api.handle("GET", "/api/dashboards/" + d["id"])
    each[d["id"]] = "%d: %s" % (s, "explains what it needs" if d in about["unavailable"] and body.get("empty") else "%d panels" % len(body.get("panels", [])))
reopened = "qa" in PortableDashboards(data_dir="/data").describe()["offered"]
json.dumps({"results": added["results"], "offered": about["offered"], "unavailable": len(about["unavailable"]),
            "qa": {k: m[k]["value"] for k in ("planned", "coverage", "failed", "blocked", "unexecuted", "unsupported")},
            "drill_unexecuted": drill, "each": each, "survives_reopen": reopened, "seconds": round(time.time() - t0, 2),
            "heavy": sorted(k for k in sys.modules if k.split(".")[0] in ("fastapi", "httpx", "pydantic", "starlette", "uvicorn", "cryptography", "requests"))})
`));
const fail = [];
if (out.heavy.length) fail.push(`a server-side dependency was imported: ${out.heavy}`);
if (out.qa.planned !== 2 * out.results) fail.push(`planned should be every reported test on both declared platforms (${2 * out.results}), got ${out.qa.planned}`);
if (out.qa.unexecuted !== out.results || out.drill_unexecuted !== out.results) fail.push("the platform nobody ran must read as not executed, and its drill-down must list exactly those tests");
if (!out.offered.includes("qa") || !out.offered.includes("portfolio")) fail.push(`QA and portfolio must be offered, got ${out.offered}`);
if (out.unavailable !== 9) fail.push(`nine dashboards need a connected application, got ${out.unavailable}`);
if (!out.survives_reopen) fail.push("the results must still be there when the store is opened again");
console.log(JSON.stringify(out, null, 1));
if (fail.length) { console.error("FAIL\n- " + fail.join("\n- ")); process.exit(1); }
console.log("OK: the dashboards run in WebAssembly Python from the kernel archive");
