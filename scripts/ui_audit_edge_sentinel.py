"""Edge Sentinel browser audit against the fixture core. Local, loopback only, deterministic.

Records per journey: environment, expected, observed, evidence path, verdict. Console errors, page
errors and every network request are captured, so "it looked fine" is never the evidence. The server
under test runs with the fixture core installed, which refuses every CrowdSec side effect at core.

Usage: python ui_audit.py <base-url> <out-dir> <label>
Exit code 1 if any journey with severity>=block fails.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
OUT = Path(sys.argv[2])
LABEL = sys.argv[3]
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORTS = {"desktop": (1440, 900), "tablet": (834, 1112), "mobile": (390, 844)}
findings: list = []
notes: dict = {}
BAD_STATUS: list = []          # any 4xx/5xx seen on a subresource
CONSOLE: list = []             # console errors + pageexceptions, all journeys
NET: list = []                 # every request the page made


def record(area, expected, observed, verdict, severity="info", evidence=None):
    findings.append({"area": area, "expected": expected, "observed": observed,
                     "verdict": verdict, "severity": severity, "evidence": evidence or []})
    mark = {"pass": "PASS", "fail": "FAIL", "observe": "OBSV"}.get(verdict, verdict.upper())
    print(f"[{mark:4}] {severity:7} {area}: {observed if len(str(observed)) < 220 else str(observed)[:220] + '…'}")


def _attach(page, tag):
    page.on("console", lambda m: CONSOLE.append({"journey": tag, "type": m.type, "text": m.text})
            if m.type == "error" else None)
    page.on("pageerror", lambda e: CONSOLE.append({"journey": tag, "type": "pageerror", "text": str(e)}))

    def _resp(r):
        NET.append({"journey": tag, "url": r.url, "status": r.status})
        if r.status >= 400 and not r.url.rstrip("/").endswith(("/health",)):
            BAD_STATUS.append({"journey": tag, "url": r.url, "status": r.status})
    page.on("response", _resp)


def new_page(browser, vp):
    w, h = VIEWPORTS[vp]
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
    dialogs: list = []
    page = ctx.new_page()
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    return ctx, page, dialogs


def load(page, vp, tag, path="/"):
    _attach(page, tag)
    resp = page.goto(BASE + path, wait_until="load", timeout=30000)
    page.wait_for_timeout(400)                      # let any late script settle
    return resp


def shot(page, name):
    p = OUT / f"{LABEL}-{name}.png"
    page.screenshot(path=str(p), full_page=True)
    return str(p)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    print(f"chromium launched; base={BASE}")

    # ---- J1: cold load, three viewports, no client-side failure -------------------------------
    for vp in VIEWPORTS:
        ctx, page, _dlg = new_page(browser, vp)
        r = load(page, vp, f"load-{vp}")
        title = page.title()
        errs_before = len(CONSOLE)
        p = shot(page, f"load-{vp}")
        ok = r.status == 200 and len(title) > 0 and "edge" in (title + page.inner_text("body")[:400]).lower()
        record("dashboard loads", f"HTTP 200, titled, recognisable as Edge Sentinel at {vp}",
               f"status={r.status} title={title!r} evidence={p}", "pass" if ok else "fail",
               "block" if vp == "desktop" else "low")
        # no horizontal overflow: the page must fit its viewport
        sw = page.evaluate("document.documentElement.scrollWidth")
        cw = page.evaluate("document.documentElement.clientWidth")
        if sw > cw + 1:
            culprit = page.evaluate("""(cw) => [...document.querySelectorAll('*')]
                .map(e => ({tag: e.tagName.toLowerCase(), cls: String(e.className || '').slice(0, 34),
                            right: Math.round(e.getBoundingClientRect().right),
                            over: Math.round(e.scrollWidth - e.clientWidth),
                            ws: getComputedStyle(e).whiteSpace, mw: getComputedStyle(e).minWidth,
                            txt: (e.textContent || '').trim().slice(0, 46)}))
                .filter(o => o.right > cw || o.over > 0)
                .sort((a, b) => b.right - a.right || b.over - a.over).slice(0, 4)""", cw)
            notes[f"overflow_{vp}"] = culprit
        detail = f"scrollWidth={sw} clientWidth={cw}"
        if sw > cw + 1:
            worst = "; ".join("%s.%s right=%d over=%d ws=%s minw=%s" %
                              (c["tag"], c["cls"], c["right"], c["over"], c["ws"], c["mw"])
                              for c in notes.get(f"overflow_{vp}", []))
            detail += " | " + worst
        record("no horizontal overflow", f"scrollWidth <= clientWidth at {vp}",
               detail, "pass" if sw <= cw + 1 else "fail",
               "low" if vp != "mobile" else "medium")
        ctx.close()

    # ---- J2: the demo label is on the page, and honest -----------------------------------------
    ctx, page, _d = new_page(browser, "desktop")
    load(page, "desktop", "demo-label")
    banner = page.locator("[role=status]")
    btxt = banner.inner_text() if banner.count() else ""
    p = shot(page, "demo-banner")
    record("synthetic-data label visible without scrolling config",
           "a status region states the data is synthetic and that no CrowdSec core was read",
           f"banner={btxt[:180]!r}", "pass" if ("DEMO" in btxt.upper() and "synthetic" in btxt.lower()) else "fail",
           "block", [p])

    # ---- J3: content is populated from the fixture core -----------------------------------------
    body = page.inner_text("body")
    tables = page.locator("table").count()
    rows = page.locator("table tbody tr").count()
    p = shot(page, "panels")
    record("fixture data reaches the page", "at least one table and one populated row, rendered from fixtures",
           f"tables={tables} rows={rows}", "pass" if tables >= 1 and rows >= 1 else "fail", "block", [p])
    # 'nan'/'None' as substrings match real words (governance, maintenance), so match whole tokens only
    artefacts = [w for w in ("Traceback", "Internal Server Error", "NameError", "KeyError") if w in body]
    artefacts += [w for w in ("nan", "None", "undefined") if re.search(rf"(?<![\w.-]){w}(?![\w-])", body)]
    record("no raw error artefacts on the page", "no traceback, exception name, or bare None/nan token",
           f"found={artefacts or 'none'}", "pass" if not artefacts else "fail", "medium")
    notes["body_head"] = re.sub(r"\s+", " ", body[:600])

    # ---- J4: the health endpoint describes the mode the UI is showing ---------------------------
    hp = page.request.get(BASE + "/health")
    hj = hp.json()
    record("health reports the fixture mode the dashboard advertises",
           "demo.enabled true with a tag, and connectivity stated honestly",
           f"health={{demo: {hj.get('demo')}, connected: {hj.get('crowdsec_connected')}}}",
           "pass" if hj.get("demo", {}).get("enabled") else "fail", "high")

    # ---- J5: an authenticated operator still cannot cause a CrowdSec side effect ------------------
    # The server runs with operator credentials configured AND SENTINEL_BLOCK_ENABLED=1, so /execute is a
    # real kick-off attempt rather than a dry run. Execute only renders once an action is APPROVED, so the
    # audit stages one through the product's own respond endpoint first. If fixture mode holds, core refuses.
    import base64 as _b64
    import os
    creds = os.environ.get("AUDIT_OPERATOR_CREDS", "")
    auth = {"Authorization": "Basic " + _b64.b64encode(creds.encode()).decode(),
            "Content-Type": "application/json"} if creds else {}
    stage = re.search(r"esRespond\('([^']+)','([^']+)',true\)", page.content())
    if not creds:
        record("authenticated side effect refused", "operator credentials supplied for the audit",
               "AUDIT_OPERATOR_CREDS unset — the strongest refusal journey was not attempted", "fail", "high")
    elif not stage:
        record("authenticated side effect refused", "a case with an action awaiting approval",
               "no Approve control on the page, so no action could be staged", "fail", "high")
    else:
        cid, rid = stage.group(1), stage.group(2)
        st = page.request.post(f"{BASE}/api/cases/{cid}/respond", headers=auth,
                               data=json.dumps({"request_id": rid, "approved": True}))
        st_j = st.json() if "json" in (st.headers.get("content-type") or "") else {}
        record("an authenticated operator can stage (approve) a recommendation",
               "2xx and recorded (not simulated) — staging is not an edge action",
               f"status={st.status} simulated={st_j.get('simulated')} state={st_j.get('state')}",
               "pass" if st.status == 200 and not st_j.get("simulated") else "fail", "high")

        page3x = ctx.new_page()
        _attach(page3x, "execute")
        page3x.goto(BASE + "/", wait_until="load")
        page3x.wait_for_timeout(500)
        exec_btn = re.search(r"esExecute\('" + re.escape(cid) + r"','([^']+)'", page3x.content())
        record("an approved action offers the operator kick-off", "an Execute control for the staged case",
               f"found={bool(exec_btn)}", "pass" if exec_btn else "fail", "high")
        if exec_btn:
            rr = page3x.request.post(f"{BASE}/api/cases/{cid}/execute", headers=auth,
                                     data=json.dumps({"request_id": exec_btn.group(1)}))
            body_j = rr.json() if "json" in (rr.headers.get("content-type") or "") else {}
            refused = rr.status == 403 and body_j.get("refused") == "demo_mode"
            record("an authenticated operator cannot perform a CrowdSec side effect in fixture mode",
                   "403 refused=demo_mode with a reason; nothing blocked, no receipt claiming execution",
                   f"status={rr.status} body={json.dumps(body_j)[:200]}",
                   "pass" if refused else "fail", "block")
            # The point of refusing before writing: the case must look exactly as the operator left it. A
            # refusal recorded as a failed attempt shows "last attempt failed · retry" — an outage that
            # never happened, and retryable state the operator did not cause.
            page3x.reload()
            page3x.wait_for_timeout(500)
            txt = page3x.inner_text("body")
            pstate = shot(page3x, "after-refusal")
            record("a refusal leaves no failed attempt and no retry state in the UI",
                   'the case still reads "approved · awaiting operator"; "last attempt failed" appears nowhere',
                   f"failed_state_shown={'last attempt failed' in txt.lower()} "
                   f"awaiting_operator_shown={'awaiting operator' in txt.lower()}",
                   "pass" if "last attempt failed" not in txt.lower() and "awaiting operator" in txt.lower()
                   else "fail", "block", [pstate])
        an = page.request.post(f"{BASE}/api/cases/{cid}/execute",
                               headers={"Content-Type": "application/json"},
                               data=json.dumps({"request_id": rid}))
        record("the same call without credentials is not more powerful",
               "401/403/503 refusal, never a 2xx", f"status={an.status}",
               "pass" if an.status in (401, 403, 503) else "fail", "block")
        page3x.close()
    ctx.close()

    # ---- J6: the approve path a visitor can actually click ---------------------------------------
    ctx2, page2, dialogs = new_page(browser, "desktop")
    load(page2, "desktop", "approve-click")
    btn = page2.get_by_role("button", name=re.compile("approve", re.I))
    if btn.count() == 0:
        record("approve control", "an Approve button exists for a staged recommendation", "none found",
               "observe", "low")
    else:
        btn.first.click()
        page2.wait_for_timeout(900)
        p = shot(page2, "after-approve")
        said = " ".join(dialogs).lower()
        record("clicking Approve performs no edge action",
               "a simulated/staged message or a refusal; never a claim that the edge changed",
               f"dialog={said[:200]!r}",
               "pass" if not any(w in said for w in ("blocked", "edge updated", "ban applied")) else "fail",
               "block", [p])
        if "alert(" in page2.content():
            record("native alert() used for outcome feedback",
                   "an in-page, styleable, screen-reader-friendly pattern",
                   "window.alert() blocks the page, is auto-dismissed by automation, and the outcome "
                   "exists only inside the modal", "observe", "low", [p])
    ctx2.close()

    # ---- J7: accessibility basics a screen-reader user depends on ---------------------------------
    ctx3, page3, _d3 = new_page(browser, "desktop")
    load(page3, "desktop", "a11y")
    a11y = page3.evaluate("""() => {
      const name = (el) => (el.getAttribute('aria-label') || el.textContent || '').trim()
        || (el.getAttribute('title') || '').trim();
      const vis = (c) => c.offsetParent !== null;
      const ctrls = [...document.querySelectorAll('button, a[href], input, select')].filter(vis);
      const h = [...document.querySelectorAll('h1,h2,h3,h4')].map(e => +e.tagName[1]);
      let skips = 0;
      for (let i = 1; i < h.length; i++) if (h[i] > h[i-1] + 1) skips++;
      return {controls: ctrls.length,
              unnamed: ctrls.filter(c => !name(c)).length,
              h1: document.querySelectorAll('h1').length,
              main: document.querySelectorAll('main').length,
              lang: document.documentElement.getAttribute('lang'), skips,
              unnamed_tables: [...document.querySelectorAll('table')]
                 .filter(t => !t.querySelector('caption,[aria-label],[aria-labelledby]')).length};
    }""")
    p = shot(page3, "a11y")
    record("every visible control has an accessible name", "no unnamed controls",
           f"{a11y['unnamed']} of {a11y['controls']} unnamed",
           "pass" if a11y["unnamed"] == 0 else "fail", "medium", [p])
    record("one h1 and a main landmark", "exactly one h1 and at least one <main>",
           f"h1={a11y['h1']} main={a11y['main']}",
           "pass" if a11y["h1"] == 1 and a11y["main"] >= 1 else "fail", "medium")
    record("document language declared", "html[lang] set, so a screen reader picks the right voice",
           f"lang={a11y['lang']!r}", "pass" if a11y["lang"] else "fail", "low")
    record("heading levels do not skip", "no h1 -> h3 jumps", f"skips={a11y['skips']}",
           "pass" if a11y["skips"] == 0 else "fail", "low")
    record("tables are identifiable", "each table carries a caption or aria label",
           f"{a11y['unnamed_tables']} unnamed table(s)",
           "pass" if a11y["unnamed_tables"] == 0 else "fail", "low")
    page3.keyboard.press("Tab")
    f1 = page3.evaluate("() => document.activeElement.tagName")
    ring = page3.evaluate("""() => { const s = getComputedStyle(document.activeElement);
        return (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== 'none'; }""")
    record("keyboard focus lands on a control and is visible",
           "Tab reaches an interactive element and a focus ring is drawn",
           f"focused={f1} visible_ring={ring}",
           "pass" if f1 not in ("BODY", "HTML") and ring else "fail", "medium")
    ctx3.close()
    browser.close()

    # ---- cross-journey invariants -------------------------------------------------------------------
    hard = [c for c in CONSOLE if "favicon" not in c["text"].lower()]
    record("no uncaught client-side errors", "zero console errors and zero page exceptions",
           f"{len(hard)} error(s)" + (f", e.g. {hard[0]['text'][:150]}" if hard else ""),
           "pass" if not hard else "fail", "high")
    record("no unexpected HTTP failures on subresources", "no 4xx/5xx other than deliberate refusals",
           f"{len(BAD_STATUS)} seen" + (f", e.g. {BAD_STATUS[:2]}" if BAD_STATUS else ""),
           "pass" if not BAD_STATUS else "fail", "medium")
    # Isolation, evidenced rather than asserted: the point is that a UI audit cannot reach anything real.
    # demo.redevops.io is named because auth.py says the hosted demo shares this operator mechanism, so it
    # is the nearest thing to a real destination an audit could wrongly hit. CROWDSEC_LAPI_URL is unset for
    # the audit, so even a stray core read would default to localhost:8086 — and with the fixture core
    # installed, core's reads and writes are replaced before any request is served.
    hosts = sorted({urlparse(r["url"]).netloc for r in NET})
    loopback_only = bool(hosts) and all(h.startswith("127.0.0.1:") for h in hosts)
    record("every request stayed on loopback", "all requests to 127.0.0.1:<port>; never demo.redevops.io, "
           "never a CrowdSec LAPI, never an outbound host",
           f"{len(NET)} requests to {hosts}", "pass" if loopback_only else "fail", "block")

    env = {"base": BASE, "label": LABEL, "viewports": VIEWPORTS, "requests_seen": len(NET),
           "requests": NET, "console_errors": CONSOLE, "bad_status": BAD_STATUS, **notes}
    (OUT / f"{LABEL}-audit.json").write_text(json.dumps({"environment": env, "findings": findings}, indent=2))
    n = lambda v: sum(1 for f in findings if f["verdict"] == v)
    lines = [f"# Edge Sentinel UI audit — {LABEL}", "",
             f"- target `{BASE}` · headless Chromium · viewports {', '.join(VIEWPORTS)}",
             f"- requests observed: {len(NET)} · console errors: {len(hard)}",
             f"- findings: {n('pass')} pass · {n('fail')} fail · {n('observe')} observed", "",
             "| verdict | severity | area | observed |", "|---|---|---|---|"]
    for f in findings:
        lines.append(f"| {f['verdict']} | {f['severity']} | {f['area']} | "
                     + str(f["observed"]).replace("|", "/").replace("\n", " ")[:190] + " |")
    (OUT / f"{LABEL}-audit.md").write_text("\n".join(lines) + "\n")
    # An evidence manifest: every artefact this run produced, digested, so a screenshot in a review thread
    # can be tied back to the run that made it. Screenshots are not committed (they are multi-megabyte);
    # this is what makes them checkable anyway.
    manifest = [{"file": p.name, "bytes": p.stat().st_size,
                 "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(OUT.glob(f"{LABEL}-*")) if p.name != f"{LABEL}-evidence-manifest.json"]
    (OUT / f"{LABEL}-evidence-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    blockers = [f for f in findings if f["verdict"] == "fail" and f["severity"] in ("block", "high")]
    print(f"\nwrote {OUT / (LABEL + '-audit.md')}")
    print(f"blockers: {len(blockers)}")
    sys.exit(1 if blockers else 0)



