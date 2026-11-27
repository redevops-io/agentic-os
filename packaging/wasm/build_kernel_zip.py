#!/usr/bin/env python3
"""Build the dashboards kernel archive for a WebAssembly (Pyodide) page: exactly the agentic_os modules the server-less
dashboards import, nothing else.

    python packaging/wasm/build_kernel_zip.py <out.zip>

The list is not maintained by hand: a fresh interpreter exercises every route of agentic_os.dashboards.portable (all
fourteen dashboards, a drill-down, a saved view, a JUnit import) and the agentic_os modules it ended up importing are the
archive. The archive is reproducible (sorted entries, fixed timestamps), so its sha256 identifies its content. The script
fails if a web framework or an HTTP client was imported, or if the archive would exceed 2 MB.
"""
import hashlib, json, os, subprocess, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXERCISE = r'''
import json, sys
from agentic_os.dashboards.portable import PortableDashboards
from agentic_os.dashboards.service import DEFINITIONS
api = PortableDashboards()
junit = '<testsuite name="s" tests="2"><testcase classname="a" name="b"/><testcase classname="a" name="c"><failure message="x"/></testcase></testsuite>'
assert api.handle("POST", "/api/dashboards/qa/junit", body={"project": "local", "suite": "unit", "platform": "here", "platforms": ["here", "there"], "xml": junit})[0] == 200
assert api.handle("GET", "/api/dashboards")[0] == 200 and api.handle("GET", "/api/dashboards/_portable")[0] == 200
for df in DEFINITIONS:
    s, d = api.handle("GET", "/api/dashboards/" + df.id, {"days": "30"})
    assert s == 200, (df.id, s)
    for k in [m["drill"] for p in d["panels"] for m in p["metrics"] if m.get("drill")][:2]:
        assert api.handle("GET", "/api/dashboards/%s/drill" % df.id, {"key": k})[0] == 200
s, v = api.handle("PUT", "/api/dashboards/views", body={"name": "n", "dashboard": "qa"})
assert s == 200 and api.handle("DELETE", "/api/dashboards/views/" + v["id"])[0] == 200
mods = sorted((m.__name__, m.__file__) for m in list(sys.modules.values()) if getattr(m, "__name__", "").split(".")[0] == "agentic_os" and getattr(m, "__file__", None))
heavy = sorted(k for k in sys.modules if k.split(".")[0] in ("fastapi", "starlette", "pydantic", "httpx", "requests", "uvicorn", "cryptography"))
print(json.dumps({"mods": mods, "heavy": heavy}))
'''


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 64
    out = sys.argv[1]
    r = subprocess.run([sys.executable, "-c", EXERCISE], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ROOT})
    if r.returncode:
        print(r.stderr[-2000:], file=sys.stderr)
        return 1
    got = json.loads(r.stdout.strip().splitlines()[-1])
    if got["heavy"]:
        print(f"a server-side dependency was imported: {got['heavy']}", file=sys.stderr)
        return 1
    files = sorted({os.path.relpath(f, ROOT).replace(os.sep, "/") for _n, f in got["mods"] if f.endswith(".py")})
    version = {}
    exec(next(l for l in open(os.path.join(ROOT, "agentic_os", "__init__.py"), encoding="utf-8") if l.startswith("__version__")), version)  # noqa: S102
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info, open(os.path.join(ROOT, rel), "rb").read())
    size, sha = os.path.getsize(out), hashlib.sha256(open(out, "rb").read()).hexdigest()
    if size > 2_000_000:
        print(f"the archive is {size} bytes: something large was pulled in", file=sys.stderr)
        return 1
    print(json.dumps({"kernel": version["__version__"], "files": len(files), "bytes": size, "sha256": sha}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
