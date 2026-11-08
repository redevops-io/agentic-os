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

# The contract lives next to this script, not on an installed path: the audit is run from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_audit_contract     # noqa: E402 — the agentic-tests evidence contract, projected for this repo

BASE = sys.argv[1].rstrip("/")
OUT = Path(sys.argv[2])
LABEL = sys.argv[3]
OUT.mkdir(parents=True, exist_ok=True)

#: `narrow` is 320 CSS px, the width SC 1.4.10 (Reflow) actually measures at. Without it the audit could
#: report a reflow pass while a 947 px table sat just below the smallest width it tested — which is what
#: happened on the first run.
VIEWPORTS = {"narrow": (320, 720), "mobile": (390, 844), "tablet": (834, 1112), "desktop": (1440, 900)}
findings: list = []
notes: dict = {}
# Every journey in this harness runs against one surface, and the signature has to say so: the same defect
# on the dashboard and on a plane panel is two different regressions.
notes["current_surface"] = "dashboard"
CONTRACT_ERRORS: list = []         # every way this run's evidence fails the contract; gates the exit code
BAD_STATUS: list = []          # any 4xx/5xx seen on a subresource
CONSOLE: list = []             # console errors + pageexceptions, all journeys
NET: list = []                 # every request the page made


def _px(value):
    """`'947px'` -> 947.0; anything not a length (auto, normal, NaN from a missing table) -> 0."""
    try:
        return float(str(value).replace("px", "").strip())
    except ValueError:
        return 0.0


def record(area, expected, observed, verdict, severity="info", evidence=None, *,
           criterion="", target="", surface="", certainty=None, layer=None,
           evidence_ref="", recommendation=""):
    """Record one journey result, in the agentic-tests evidence contract's terms.

    Defaults are honest rather than permissive: every check in this harness measures a fact out of the DOM
    or the HTTP exchange, so a deterministic check is CONFIRMED by construction — it observed the thing it
    reports. A check that *interprets* (an error-artefact pattern, an axe needs-review) must pass
    `layer=HEURISTIC`, which makes it UNCERTAIN by default and takes away its power to mark a release
    blocker. `criterion`/`target` stay empty on checks that can only pass: they produce no finding and so
    need no signature. The gate rejects an issue that cites neither, so a *failure* cannot hide behind a
    blank field.
    """
    layer = layer or ui_audit_contract.Layer.DETERMINISTIC
    if certainty is None:
        certainty = (ui_audit_contract.Certainty.UNCERTAIN if layer != ui_audit_contract.Layer.DETERMINISTIC
                     else ui_audit_contract.Certainty.CONFIRMED)
    refs = [e for e in (evidence or []) if ui_audit_contract.is_valid_ref_name(str(e))]
    f, errors = ui_audit_contract.finding(
        area, expected, observed, verdict, severity, criterion=criterion, target=target,
        surface=surface or notes.get("current_surface", ""), certainty=certainty, layer=layer,
        evidence_ref=evidence_ref or (refs[0] if refs else ""), recommendation=recommendation)
    f["expected"], f["observed"], f["evidence"] = expected, observed, list(evidence or [])
    f["area"] = area          # the report and the notes key on `area`; the contract calls it `dimension`
    # An issue that cites no criterion and no target cannot be clustered, compared or asserted on later, so
    # it is not evidence of a defect — it is a note that something looked wrong.
    if verdict == "fail" and not criterion and not target:
        CONTRACT_ERRORS.append(f"{area}: a reported issue must cite a criterion and/or a target")
    findings.append(f)
    CONTRACT_ERRORS.extend(errors)
    mark = {"pass": "PASS", "fail": "FAIL", "observe": "OBSV"}.get(verdict, verdict.upper())
    text = str(observed)
    print(f"[{mark:4}] {severity:7} {area}: {text if len(text) < 220 else text[:220] + '…'}")



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
                            tl: getComputedStyle(e).tableLayout, w: Math.round(e.getBoundingClientRect().width),
                            txt: (e.textContent || '').trim().slice(0, 46)}))
                .filter(o => o.right > cw || o.over > 0)
                .sort((a, b) => b.right - a.right || b.over - a.over).slice(0, 12)""", cw)
            notes[f"overflow_{vp}"] = culprit
        detail = f"scrollWidth={sw} clientWidth={cw}"
        if sw <= cw + 1:
            record("no horizontal overflow", f"scrollWidth <= clientWidth at {vp}", detail, "pass",
                   "info", criterion="1.4.10", target="document", evidence_ref=p)
        else:
            # The element that is wide is usually not the element that *cannot shrink*: a `section.shell` or
            # a `div.card` is wide because a descendant forces the page, and reporting it as the defect
            # produces a signature that comes back for every unrelated overflow. A cause is an element that
            # overflows its own content box, carries a min-width, or is an auto-layout table wider than the
            # viewport — the three things that actually refuse to shrink.
            offenders = {}
            for o in notes.get(f"overflow_{vp}", []):
                cls = (o["cls"] or "").split()[0] if o["cls"] else ""
                sel = f"{o['tag']}.{cls}" if cls else o["tag"]
                refuses_to_shrink = (o["over"] > 0 or _px(o["mw"]) > 0
                                     or (o["tag"] == "table" and o["tl"] == "auto" and o["w"] > cw))
                if refuses_to_shrink:
                    offenders.setdefault(sel, o)
            if not offenders:
                # nothing refuses to shrink, yet the page still overflows: that is worth reporting, but as a
                # page-level finding with the widest suspects named, because no element can be blamed yet
                widest = notes.get(f"overflow_{vp}", [])[:2]
                offenders["document"] = widest[0] if widest else {"right": sw, "over": sw - cw,
                                                                  "ws": "?", "mw": "?"}
            for sel, o in offenders.items():
                record("no horizontal overflow", f"no element refuses to shrink below {cw}px at {vp}",
                       f"{sel} width/right={o['right']} self-overflow={o['over']} min-width={o['mw']} "
                       f"table-layout={o.get('tl', 'n/a')} white-space={o['ws']} "
                       f"(viewport {cw}px; page scrollWidth {sw})",
                       "fail", "medium" if vp in ("mobile", "narrow") else "low",
                       criterion="1.4.10", target=sel, evidence_ref=p,
                       recommendation="let this element wrap or scroll inside its container instead of "
                                      "pushing the page; automatic table layout and fixed widths are the "
                                      "usual causes")
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
           f"found={artefacts or 'none'}", "pass" if not artefacts else "fail", "medium",
           criterion="no-error-artefacts", target="body",
           # Heuristic on purpose: this check pattern-matches text, so it can flag review but must never be
           # the thing that blocks a release — a confirmed deterministic check owns that call.
           layer=ui_audit_contract.Layer.HEURISTIC)
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
           "pass" if a11y["unnamed"] == 0 else "fail", "medium", [p],
           criterion="4.1.2", target="button,a,input,select", evidence_ref=p)
    # One criterion per record. These two used to be a single journey, which meant a missing <main> and a
    # duplicated <h1> shared one finding, one severity and one signature — so the report could not tell you
    # which page defect you had, and neither could a regression check.
    record("exactly one h1", "exactly one <h1> on the page", f"h1={a11y['h1']}",
           "pass" if a11y["h1"] == 1 else "fail", "medium", [p],
           criterion="1.3.1", target="h1", evidence_ref=p)
    record("a main landmark exists", "at least one <main>, so a screen reader can skip to the content",
           f"main={a11y['main']}",
           "pass" if a11y["main"] >= 1 else "fail", "medium", [p],
           criterion="1.3.1", target="body", evidence_ref=p)
    record("document language declared", "html[lang] set, so a screen reader picks the right voice",
           f"lang={a11y['lang']!r}", "pass" if a11y["lang"] else "fail", "low",
           criterion="3.1.1", target="html", evidence_ref=p)
    record("heading levels do not skip", "no h1 -> h3 jumps", f"skips={a11y['skips']}",
           "pass" if a11y["skips"] == 0 else "fail", "low",
           criterion="1.3.1", target="h1,h2,h3,h4", evidence_ref=p)
    record("tables are identifiable", "each table carries a caption or aria label",
           f"{a11y['unnamed_tables']} unnamed table(s)",
           "pass" if a11y["unnamed_tables"] == 0 else "fail", "low",
           criterion="1.3.1", target="table", evidence_ref=p)
    page3.keyboard.press("Tab")
    f1 = page3.evaluate("() => document.activeElement.tagName")
    ring = page3.evaluate("""() => { const s = getComputedStyle(document.activeElement);
        return (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== 'none'; }""")
    record("keyboard focus lands on a control and is visible",
           "Tab reaches an interactive element and a focus ring is drawn",
           f"focused={f1} visible_ring={ring}",
           "pass" if f1 not in ("BODY", "HTML") and ring else "fail", "medium",
           criterion="2.4.7", target=":focus", evidence_ref=p)
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
           f"{len(NET)} requests to {hosts}", "pass" if loopback_only else "fail", "block",
           criterion="no-side-effect", target="document",
           recommendation="an audit run that reached a host outside loopback has contacted a real service; "
                          "treat the run as a disclosure incident, not a test failure")

    env = {"base": BASE, "label": LABEL, "viewports": VIEWPORTS, "requests_seen": len(NET),
           "requests": NET, "console_errors": CONSOLE, "bad_status": BAD_STATUS, **notes}
    (OUT / f"{LABEL}-audit.json").write_text(json.dumps({"environment": env, "findings": findings}, indent=2))
    n = lambda v: sum(1 for f in findings if f["verdict"] == v)
    lines = [f"# Edge Sentinel UI audit — {LABEL}", "",
             f"- target `{BASE}` · headless Chromium · viewports {', '.join(VIEWPORTS)}",
             f"- requests observed: {len(NET)} · console errors: {len(hard)}",
             f"- findings: {n('pass')} pass · {n('fail')} fail · {n('observe')} observed", "",
             "| verdict | severity | criterion | target | evidence | area | observed |", "|---|---|---|---|---|---|---|"]
    for f in findings:
        lines.append(f"| {f['verdict']} | {f['severity_label']} | {f['criterion'] or '—'} | "
                     f"{f['target'] or '—'} | {f['evidence_ref'] or '—'} | {f['area']} | "
                     + str(f["observed"]).replace("|", "/").replace("\n", " ")[:150] + " |")
    (OUT / f"{LABEL}-audit.md").write_text("\n".join(lines) + "\n")
    # ---- the contract gate ---------------------------------------------------------------------------
    # The verdict is now the contract's, not a count of scary words: blockers come only from confirmed
    # deterministic severity-0 findings, and the run cannot pass on evidence it cannot verify.
    verdict = ui_audit_contract.project(
        findings, url=BASE, journey="edge-sentinel fixture dashboard",
        surfaces=list(VIEWPORTS))
    evidence_errors = ui_audit_contract.validate_evidence(findings, OUT)
    hits = ui_audit_contract.regression_hits(verdict["signatures"])
    contract_errors = CONTRACT_ERRORS + evidence_errors
    (OUT / f"{LABEL}-contract.json").write_text(
        json.dumps({"verdict": verdict, "contract_errors": contract_errors,
                    "known_defect_signatures_still_present": hits}, indent=2) + "\n")
    # An evidence manifest: every artefact this run produced, digested, so a screenshot in a review thread
    # can be tied back to the run that made it. Screenshots are not committed (they are multi-megabyte);
    # this is what makes them checkable anyway. It is written LAST, over everything the run produced: on a
    # re-run into an existing directory, a manifest written before the contract file would list the previous
    # run's contract JSON and then watch it be overwritten — a self-reported hash mismatch on a run in which
    # nothing was wrong, which is exactly the kind of finding that teaches people to ignore the manifest.
    manifest = [{"file": p.name, "bytes": p.stat().st_size,
                 "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(OUT.glob(f"{LABEL}-*")) if p.name != f"{LABEL}-evidence-manifest.json"]
    (OUT / f"{LABEL}-evidence-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    blockers = verdict["blockers"]
    print(f"\nwrote {OUT / (LABEL + '-audit.md')}")
    print(f"contract status: {verdict['status']} · blockers: {len(blockers)} · "
          f"escalations: {len(verdict['escalations'])} · measured: {verdict['measured']}")
    for e in contract_errors:
        print(f"CONTRACT VIOLATION: {e}")
    for h in hits:
        print(f"REGRESSION: known defect signature still reported: {h}")
    # Green requires three things, not one: nothing to fix, evidence behind every claim, and no known
    # defect coming back. A run that reports PASS while its own findings have no artifacts is the failure
    # mode this gate exists to make impossible.
    sys.exit(0 if (verdict["status"] == ui_audit_contract.Status.PASS
                   and not contract_errors and not hits) else 1)



