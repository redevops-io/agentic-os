"""Playwright drive of the Projects UI against a running Projects server that is wired to a REAL Edge Sentinel.
usage: ui_flow.py <projects_url> <sentinel_url> <outdir> [--propose-via-sidekick]
Writes screenshots (1440x900 and 390x844) and a JSON transcript. Fails loudly on any unmet expectation."""
import json, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

projects, sentinel, out = sys.argv[1].rstrip("/"), sys.argv[2].rstrip("/"), Path(sys.argv[3])
import httpx, base64
def opt(name, default=""):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
IP = opt("--ip", "203.0.113.77")
SBASIC = opt("--sentinel-basic")
EXPECT_LIVE = "--live" in sys.argv
out.mkdir(parents=True, exist_ok=True)
T = {"steps": [], "console_errors": [], "external_requests": []}
VIEWPORTS = {"desktop-1440x900": (1440, 900), "mobile-390x844": (390, 844)}


def step(name, **kw):
    T["steps"].append({"step": name, "at": time.strftime("%H:%M:%S"), **kw}); print("STEP", name, kw or "")


def shot(page, name):
    for tag, (w, h) in VIEWPORTS.items():
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / f"{name}-{tag}.png"))
        sw = page.evaluate("document.documentElement.scrollWidth")
        T.setdefault("overflow", {})[f"{name}-{tag}"] = sw - w
    page.set_viewport_size({"width": 1440, "height": 900})


with sync_playwright() as p:
    b = p.chromium.launch()
    creds = None
    if "--basic" in sys.argv:
        u, _, pw = sys.argv[sys.argv.index("--basic") + 1].partition(":")
        creds = {"username": u, "password": pw}
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, http_credentials=creds)
    page = ctx.new_page()
    page.on("console", lambda m: T["console_errors"].append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: T["external_requests"].append(r.url) if not r.url.startswith(projects) and not r.url.startswith("data:") else None)

    if opt("--launch-url"):                       # desktop sidecar: the one-time launch URL establishes the session
        page.goto(opt("--launch-url")); page.wait_for_timeout(2500)
    # 1. Apps: the product is listed with live health
    page.goto(projects + "/#/apps")
    expect(page.get_by_text("Edge Sentinel").first).to_be_visible(timeout=15000)
    step("apps-row-visible"); shot(page, "01-apps")

    # 2. Sidekick: ask for the security status (real numbers from the real product)
    page.goto(projects + "/#/today")
    page.get_by_label("Ask Sidekick").fill("show Edge Sentinel posture")
    page.keyboard.press("Enter")
    msg = page.get_by_text(re.compile(r"Edge Sentinel posture:")).last
    expect(msg).to_be_visible(timeout=15000)
    posture = msg.inner_text(); step("sidekick-posture", text=posture); shot(page, "02-sidekick-posture")

    # 3. a ban proposal staged from Sidekick appears as an approval
    if "--propose-via-sidekick" in sys.argv:
        page.get_by_label("Ask Sidekick").fill("propose a ban for 203.0.113.77 for 2 hours because ssh brute force")
        page.keyboard.press("Enter")
        expect(page.get_by_text("awaiting a person's approval").last).to_be_visible(timeout=15000)
        step("sidekick-proposed", text=page.get_by_text("awaiting a person's approval").last.inner_text()); shot(page, "03-sidekick-proposal")

    # 4. Attention shows it with consequence; Today carries the approve control
    page.goto(projects + "/#/attention"); page.reload()
    expect(page.get_by_text(f"Approve ban of {IP}").first).to_be_visible(timeout=15000)
    step("attention-approval", text=page.get_by_text(f"Approve ban of {IP}").first.inner_text()); shot(page, "04-attention")
    page.goto(projects + "/#/today"); page.reload()
    page.locator("button.trow", has_text=f"Approve ban of {IP}").first.click()
    expect(page.get_by_text("Records your approval in the product").first).to_be_visible(timeout=15000)
    shot(page, "05-today-approval")
    btn = page.get_by_role("button", name=re.compile(r"Approve & send"))
    btn.click()
    expect(page.get_by_text("click again to execute").first).to_be_visible(timeout=20000)
    step("decided-in-projects"); shot(page, "06-today-approved")
    page.get_by_role("button", name=re.compile(r"Approve & send")).click()
    hdr0 = {"Authorization": "Basic " + base64.b64encode(SBASIC.encode()).decode()} if SBASIC else {}
    t0 = time.time()
    while SBASIC and time.time() - t0 < 40:
        got = [x for x in httpx.get(sentinel + "/api/v1/actions", headers=hdr0).json()["items"] if x["params"].get("ip") == IP]
        if got and got[0].get("verification"):
            break
        page.wait_for_timeout(500)
    page.wait_for_timeout(1500)
    step("executed-click-returned", note=page.locator(".banner").first.inner_text() if page.locator(".banner").count() else ""); shot(page, "07-today-executed")

    # 5. Missions shows the completed mission with the real lifecycle
    page.goto(projects + "/#/missions"); page.wait_for_timeout(1500)
    expect(page.get_by_text(f"Ban {IP}").first).to_be_visible(timeout=15000)
    step("mission-row", text=page.locator("body").inner_text()[:0] or "visible"); shot(page, "08-missions")
    page.get_by_text(f"Ban {IP}").first.click(); page.wait_for_timeout(1500); shot(page, "09-mission-detail")
    page.goto(projects + "/#/activity"); page.wait_for_timeout(2000); shot(page, "10-activity")
    b.close()
# the SAME records in the product's own API
if SBASIC:
    hdr = {"Authorization": "Basic " + base64.b64encode(SBASIC.encode()).decode()}
    acts = httpx.get(sentinel + "/api/v1/actions", headers=hdr).json()["items"]
    a = next(x for x in acts if x["params"].get("ip") == IP)
    full = httpx.get(sentinel + f"/api/v1/actions/{a['request_id']}", headers=hdr).json()
    case = httpx.get(sentinel + f"/api/v1/cases/{a['case_id']}", headers=hdr).json()
    ch = httpx.get(sentinel + "/api/v1/changes?after=0&limit=200", headers=hdr).json()
    T["product_record"] = {"action": full, "case_summary": case.get("summary"), "changes": [c for c in ch["items"] if c.get("request_id") == a["request_id"]]}
    assert full["state"] in ("EXECUTED", "APPROVED") and full["verification"], full      # a dry run leaves the action APPROVED
    assert full["decision"]["actor"] == "user:alex" and full["decision"]["via"] == "projects", full["decision"]
    assert full["decision"]["external_actor"] == opt("--expect-actor", "projects:operator"), full["decision"]
    assert full["receipt"] and full["verification"], full
    want = "verified" if EXPECT_LIVE else "dry_run"
    assert full["verification"]["state"] == want, full["verification"]
    step("product-record-matches", action=a["request_id"], case=a["case_id"], verification=full["verification"]["state"])
json.dump(T, open(out / "transcript.json", "w"), indent=2)
assert not T["external_requests"], T["external_requests"]
print("OK")
