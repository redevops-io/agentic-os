"""The dashboards with no server (agentic_os/dashboards/portable.py): the same answers as the HTTP routes, from one function,
with nothing but the standard library, pyyaml and sqlite3 - what a browser tab running WebAssembly Python has."""
import subprocess
import sys

from agentic_os.dashboards.portable import STANDALONE, PortableDashboards

JUNIT = """<testsuite name="s" tests="4">
  <testcase classname="checkout" name="adds an item" time="0.1"/>
  <testcase classname="checkout" name="applies a discount"><failure message="expected 90, got 80"/></testcase>
  <testcase classname="checkout" name="pays by wallet"><skipped message="not supported on this platform"/></testcase>
  <testcase classname="checkout" name="refunds" time="0.4"/>
</testsuite>"""


def _metric(d, mid):
    return next(m for p in d["panels"] for m in p["metrics"] if m["id"] == mid)


def test_it_starts_with_nothing_and_says_what_cannot_work_here():
    api = PortableDashboards()
    s, d = api.handle("GET", "/api/dashboards/_portable")
    assert s == 200 and d["mode"] == "standalone" and not d["durable"]
    assert [a["id"] for a in d["available"]] == list(STANDALONE)
    assert len(d["unavailable"]) == 10 and all(u["needs"] and u["why"] for u in d["unavailable"])
    why = {u["id"]: u["why"] for u in d["unavailable"]}
    assert "record files" in why.pop("self_improvement")                     # files on a server: no application credential is involved
    assert all("credential" in w for w in why.values()), why
    s, rev = api.handle("GET", "/api/dashboards/revenue")
    assert s == 200 and "credential is never placed in this page" in rev["empty"]                       # asked for directly, it explains; it never shows zeros
    assert all(m["value"] is None for p in rev["panels"] for m in p["metrics"])


def test_added_results_build_the_qa_dashboard_with_the_served_rules():
    api = PortableDashboards()
    s, out = api.handle("POST", "/api/dashboards/qa/junit", body={"project": "shop", "project_name": "Shop", "suite": "e2e",
                                                                 "platform": "chromium", "platforms": ["chromium", "firefox"], "xml": JUNIT})
    assert s == 200 and out["results"] == 4 and out["duplicate"] is False, out
    s, cat = api.handle("GET", "/api/dashboards")
    assert s == 200 and "qa" in [d["id"] for d in cat["dashboards"]] and {p["id"] for p in cat["projects"]} >= {"shop"}
    s, qa = api.handle("GET", "/api/dashboards/qa", {"project": "shop", "days": "30"})
    assert s == 200
    assert _metric(qa, "planned")["value"] == 8 and _metric(qa, "unexecuted")["value"] == 4      # the platform nobody ran counts against coverage
    assert _metric(qa, "failed")["value"] == 1 and _metric(qa, "unsupported")["value"] == 1      # a skip is never a pass
    s, dr = api.handle("GET", "/api/dashboards/qa/drill", {"key": _metric(qa, "failed")["drill"], "project": "shop"})
    assert s == 200 and dr["total"] == 1 and "discount" in dr["records"][0]["title"]            # a number equals its records
    s, again = api.handle("POST", "/api/dashboards/qa/junit", body={"project": "shop", "suite": "e2e", "platform": "chromium",
                                                                   "run_id": out["run_id"], "xml": JUNIT})
    assert s == 200 and again["duplicate"] is True                                              # the same file twice is one run


def test_views_refusals_and_bad_input():
    api = PortableDashboards()
    s, v = api.handle("PUT", "/api/dashboards/views", body={"name": "mine", "dashboard": "portfolio", "filters": {"days": 7}})
    assert s == 200 and v["id"].startswith("view-")
    assert api.handle("GET", "/api/dashboards/views", {"dashboard": "portfolio"})[1]["views"][0]["name"] == "mine"
    assert api.handle("DELETE", f"/api/dashboards/views/{v['id']}")[0] == 200
    assert api.handle("GET", "/api/dashboards/nope")[0] == 404
    assert api.handle("GET", "/api/dashboards/portfolio/drill", {"key": "nope"})[0] == 404
    assert api.handle("POST", "/api/dashboards/portfolio/track", body={"key": "x"})[0] == 501    # no intake in a page
    assert api.handle("POST", "/api/dashboards/qa/junit", body={"project": "shop", "suite": "e2e", "xml": "<not xml"})[0] == 422
    assert api.handle("POST", "/api/dashboards/qa/junit", body={"project": "Bad Id", "suite": "e2e", "xml": JUNIT})[0] == 422
    assert api.handle("GET", "/api/projects")[0] == 404


def test_results_survive_a_reload_when_a_directory_is_given(tmp_path):
    a = PortableDashboards(data_dir=str(tmp_path))
    assert a.handle("POST", "/api/dashboards/qa/junit", body={"project": "local", "suite": "unit", "platform": "here", "xml": JUNIT})[0] == 200
    b = PortableDashboards(data_dir=str(tmp_path))
    assert "qa" in b.describe()["offered"]


def test_it_imports_no_web_framework_or_http_client():
    code = ("import sys; from agentic_os.dashboards.portable import PortableDashboards as P; P().handle('GET','/api/dashboards/qa');"
            "bad=sorted(k for k in sys.modules if k.split('.')[0] in ('fastapi','starlette','pydantic','httpx','requests','uvicorn','cryptography')); print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stdout + out.stderr
