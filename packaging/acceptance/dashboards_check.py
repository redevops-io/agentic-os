#!/usr/bin/env python3
"""API-level checks of the Projects dashboards on a RUNNING installation (standard library only, read-only).

    DASH_BASE=http://127.0.0.1:8787 DASH_SESSION=... DASH_CSRF=... python3 dashboards_check.py <result.json>
        [--project ID] [--suite ID] [--platform NAME] [--release VERSION] [--environment NAME] [--run-id ID]

Sign-in, whichever the installation uses (all optional; with none the installation must be open):
    DASH_SESSION / DASH_CSRF   a desktop app's local session (sent as X-Agentic-Session / X-Agentic-CSRF)
    DASH_BEARER                a control-plane bearer
    DASH_BASIC                 "user:password" of the operator sign-in
    DASH_KNOWN                 comma-separated check ids that are known to differ on this build: they are still recorded as
                               failed in the result file, but do not fail this command

What is checked is what the dashboards promise, on whatever data the installation has:
  * the catalogue answers, and without the credential it does not (when a credential was given);
  * every offered dashboard builds, names the sources it depends on, and gives a reason for every number it does not know
    (an unknown is never a zero);
  * a number equals the records behind it: for every countable tile, the drill-down lists exactly that many;
  * a record's link is http(s) or absent; an unknown drill-down or dashboard is refused, not answered with something else;
  * a dashboard that is not offered here says what it needs when asked for directly.

One line per check on stdout. The result file is the QA dashboard's own report shape (POST /api/dashboards/qa/results): a
check that cannot be judged on this installation is `unsupported` (never `passed`), and nothing is written to the
installation. Exit status 0 = no check failed (other than those named in DASH_KNOWN).
"""
import base64, json, os, platform, sys, time, urllib.error, urllib.parse, urllib.request

ALL = ("portfolio", "qa", "security", "missions", "revenue", "finance", "market", "support", "procurement", "marketing",
       "ai_costs", "infrastructure", "knowledge")
STATES = ("ok", "stale", "unavailable", "not_connected", "unauthorized", "gated")
BASE = os.environ.get("DASH_BASE", "http://127.0.0.1:8787").rstrip("/")
SESSION, CSRF = os.environ.get("DASH_SESSION", ""), os.environ.get("DASH_CSRF", "")
BEARER, BASIC = os.environ.get("DASH_BEARER", ""), os.environ.get("DASH_BASIC", "")
KNOWN = {k.strip() for k in os.environ.get("DASH_KNOWN", "").split(",") if k.strip()}
SIGNED_IN = bool(SESSION or BEARER or BASIC)


class Unsupported(Exception):
    pass


def http(path, anonymous=False, timeout=60):
    h = {"accept": "application/json", "user-agent": "dashboards-check/1"}      # some edges refuse the default urllib agent
    if not anonymous:
        if SESSION:
            h["X-Agentic-Session"] = SESSION
            if CSRF:
                h["X-Agentic-CSRF"] = CSRF
        if BEARER:
            h["Authorization"] = "Bearer " + BEARER
        elif BASIC:
            h["Authorization"] = "Basic " + base64.b64encode(BASIC.encode()).decode()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=h), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get(path):
    status, raw = http(path)
    if status != 200:
        raise AssertionError(f"GET {path} answered {status}: {raw[:160]!r}")
    return json.loads(raw)


def drill_total(dash, key):
    return get(f"/api/dashboards/{dash}/drill?key={urllib.parse.quote(key, safe='')}")["total"]


results = []


def check(test_id, fn):
    t0 = time.time()
    try:
        detail, status = fn() or "", "passed"
    except Unsupported as e:
        detail, status = str(e), "unsupported"
    except AssertionError as e:
        detail, status = str(e) or "assertion failed", "failed"
    except Exception as e:      # noqa: BLE001  (a crash of a check is a failed check, with its reason)
        detail, status = f"{type(e).__name__}: {e}", "failed"
    results.append({"test_id": test_id, "status": status, "detail": str(detail)[:500], "duration_ms": round((time.time() - t0) * 1000, 1)})
    known = " (known difference on this build)" if status == "failed" and test_id in KNOWN else ""
    print(f"{status.upper():11s} {test_id}{known}{' - ' + str(detail)[:200] if detail else ''}", flush=True)


catalog = {}


def c_catalog():
    catalog.update(get("/api/dashboards"))
    ids = [d["id"] for d in catalog["dashboards"]]
    assert ids, "the catalogue offers no dashboard"
    assert all(i in ALL for i in ids), f"the catalogue lists a dashboard this check does not know: {ids}"
    assert all(d.get("title") for d in catalog["dashboards"]), "a dashboard has no title"
    return "offered: " + ", ".join(ids)


def c_anonymous():
    if not SIGNED_IN:
        raise Unsupported("no credential was given, so there is no sign-in to test")
    for path in ("/api/dashboards", "/api/dashboards/portfolio"):
        status, _ = http(path, anonymous=True)
        assert status in (401, 403), f"{path} answered {status} without the credential"
    return "401/403 without the credential"


def offered():
    if not catalog:
        raise Unsupported("the catalogue did not answer")
    return [d["id"] for d in catalog["dashboards"]]


def c_builds(dash):
    def run():
        d = get(f"/api/dashboards/{dash}")
        assert d.get("schema") == "aos.dashboard/v1" and d.get("id") == dash, "not a dashboard projection"
        assert d.get("generated_at"), "it does not say when it was generated"
        assert d["sources"], "it names no source"
        bad = [s["id"] for s in d["sources"] if s.get("state") not in STATES]
        assert not bad, f"a source has no valid state: {bad}"
        assert d["panels"] or d.get("empty"), "nothing to show and no reason given"
        silent = [f"{p['id']}/{m['id']}" for p in d["panels"] for m in p["metrics"] if m["value"] is None and not m.get("unknown")]
        assert not silent, f"a number is not known and no reason is given: {silent[:5]}"
        empty_panels = [p["id"] for p in d["panels"] if not (p["metrics"] or p["series"] or p["items"] or p["rows"]) and not p.get("empty")]
        assert not empty_panels, f"a panel shows nothing and does not say why: {empty_panels[:5]}"
        return f"{len(d['panels'])} panels; sources " + ", ".join(f"{s['id']}={s['state']}" for s in d["sources"])
    return run


def c_numbers(dash):
    def run():
        def one_pass():
            d, checked, wrong = get(f"/api/dashboards/{dash}"), 0, []
            for p in d["panels"]:
                for m in p["metrics"]:
                    v = m["value"]
                    if m.get("drill") and v is not None and m.get("unit", "") == "" and float(v).is_integer() and m.get("numerator") in (None, v):
                        got = drill_total(dash, m["drill"])
                        checked += 1
                        if got != v:
                            wrong.append((m["id"], v, got))
            return checked, wrong
        for attempt in range(3):            # live data may change between the two reads; three disagreements in a row are a defect
            checked, wrong = one_pass()
            if not wrong:
                break
            time.sleep(2 + 3 * attempt)
        assert not wrong, f"a number and its records disagree on three consecutive reads: {wrong[:5]}"
        if not checked:
            raise Unsupported("this dashboard has no countable number with records behind it on this installation")
        return f"{checked} numbers equal their records"
    return run


def c_links(dash):
    def run():
        d, seen, bad = get(f"/api/dashboards/{dash}"), 0, []
        keys = [m["drill"] for p in d["panels"] for m in p["metrics"] if m.get("drill")][:6]
        for k in keys:
            for r in get(f"/api/dashboards/{dash}/drill?key={urllib.parse.quote(k, safe='')}")["records"]:
                seen += 1
                u = r.get("url") or ""
                if u and not u.startswith(("https://", "http://")):
                    bad.append(u[:60])
                if not r.get("ref"):
                    bad.append("a record without a reference to its system of record")
        assert not bad, f"unsafe or missing links: {bad[:5]}"
        if not seen:
            raise Unsupported("no record is listed behind any number on this installation")
        return f"{seen} records, every link http(s) or absent"
    return run


def c_refusals():
    status, _ = http("/api/dashboards/no-such-dashboard")
    assert status == 404, f"an unknown dashboard answered {status}"
    first = offered()[0]
    status, _ = http(f"/api/dashboards/{first}/drill?key=no-such-key")
    assert status == 404, f"an unknown drill-down answered {status}"
    return "unknown dashboard and unknown drill-down are 404"


def c_not_offered():
    missing = [i for i in ALL if i not in offered()]
    if not missing:
        raise Unsupported("every dashboard is offered on this installation")
    silent = []
    for dash in missing:
        status, raw = http(f"/api/dashboards/{dash}")
        if status in (403, 404):
            continue
        assert status == 200, f"{dash} answered {status}"
        d = json.loads(raw)
        if not d.get("empty"):
            silent.append(dash)
    assert not silent, f"asked for directly, a dashboard that is not offered does not say what it needs: {silent}"
    return f"{len(missing)} not offered here; each is refused or says what it needs"


def c_range():
    if "qa" not in offered():
        raise Unsupported("the QA dashboard is not offered (no test results were reported to this installation)")

    def planned(days):
        d = get(f"/api/dashboards/qa?days={days}")
        return next((m["value"] for p in d["panels"] for m in p["metrics"] if m["id"] == "planned"), None)
    week, month = planned(7), planned(30)
    if week is None or month is None:
        raise Unsupported("the QA dashboard has no planned inventory on this installation")
    assert week <= month, f"7 days plan more tests ({week}) than 30 days ({month})"
    return f"planned: {week} in 7 days, {month} in 30 days"


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("--"):
        print(__doc__)
        return 64
    out_path, opt = args[0], dict(zip(args[1::2], args[2::2]))
    check("catalogue", c_catalog)
    check("sign_in_required", c_anonymous)
    for dash in (offered() if catalog else []):
        check(f"builds[{dash}]", c_builds(dash))
        check(f"number_equals_records[{dash}]", c_numbers(dash))
        check(f"record_links[{dash}]", c_links(dash))
    check("unknown_is_refused", c_refusals)
    check("not_offered_explains", c_not_offered)
    check("range_filter", c_range)
    plat = opt.get("--platform") or f"{platform.system().lower()}-{platform.machine().lower()}"
    release = opt.get("--release", "")
    report = {"project": opt.get("--project", "redevops-desktop"), "suite": opt.get("--suite", "dashboards-api-checks"),
              "platform": plat, "environment": opt.get("--environment", "installed"), "release": release,
              "run_id": opt.get("--run-id") or f"{release or 'unreleased'}-{plat}-{time.strftime('%Y%m%dT%H%MZ', time.gmtime())}",
              "origin": "packaging/acceptance/dashboards_check.py", "at": time.time(), "results": results}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("passed", "failed", "unsupported")}
    blocking = [r["test_id"] for r in results if r["status"] == "failed" and r["test_id"] not in KNOWN]
    print(f"dashboards check: {counts['passed']} passed, {counts['failed']} failed, {counts['unsupported']} unsupported"
          + (f"; failing: {', '.join(blocking)}" if blocking else ""))
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
