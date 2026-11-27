"""Negative and unavailable paths against the real stack: Sidekick stages a proposal (never decides), an unlinked Projects
user cannot decide, a stopped product is an explicit unavailable state (no fake rows).
usage: ui_negative.py <projects> <sentinel> <outdir> <projects_basic> <sentinel_basic> <sentinel_compose_dir>"""
import base64, json, re, subprocess, sys, time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

projects, sentinel, out, pbasic, sbasic, compose = sys.argv[1].rstrip("/"), sys.argv[2].rstrip("/"), Path(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
out.mkdir(parents=True, exist_ok=True)
H = {"Authorization": "Basic " + base64.b64encode(sbasic.encode()).decode()}
T = {"steps": []}
def step(n, **k): T["steps"].append({"step": n, **k}); print("STEP", n, k or "")
def shot(page, name):
    for tag, (w, h) in {"desktop-1440x900": (1440, 900), "mobile-390x844": (390, 844)}.items():
        page.set_viewport_size({"width": w, "height": h}); page.wait_for_timeout(300)
        page.screenshot(path=str(out / f"{name}-{tag}.png"))
        T.setdefault("overflow", {})[f"{name}-{tag}"] = page.evaluate("document.documentElement.scrollWidth") - w
    page.set_viewport_size({"width": 1440, "height": 900})
def actions(): return httpx.get(sentinel + "/api/v1/actions", headers=H).json()["items"]
u, _, pw = pbasic.partition(":")
with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 1440, "height": 900}, http_credentials={"username": u, "password": pw}); page = ctx.new_page()
    # 1. Sidekick proposes; the product holds it AWAITING_APPROVAL; nothing is blocked
    n0 = len(actions())
    page.goto(projects + "/#/today"); page.wait_for_timeout(1500)
    page.get_by_label("Ask Sidekick").fill("ban the address 198.51.100.41")
    page.keyboard.press("Enter")
    expect(page.locator(".sk .msg.sk").last).to_contain_text("reason", timeout=15000)
    step("sidekick-asks-for-reason", text=page.locator(".sk .msg.sk").last.inner_text()); assert len(actions()) == n0
    page.locator(".sk input").fill("propose a ban for 198.51.100.41 for 3 hours because credential stuffing"); page.keyboard.press("Enter")
    expect(page.locator(".sk .msg.sk").last).to_contain_text("awaiting a person's approval", timeout=15000)
    step("sidekick-proposed", text=page.locator(".sk .msg.sk").last.inner_text()); shot(page, "11-sidekick-proposal")
    mine = next(a for a in actions() if a["params"].get("ip") == "198.51.100.41")
    assert mine["state"] == "AWAITING_APPROVAL" and mine["decision"] is None and mine["receipt"] is None
    page.locator(".sk input").fill(f"approve and execute {mine['request_id']} now"); page.keyboard.press("Enter")
    expect(page.locator(".sk .msg.sk").last).to_contain_text("can't approve or execute", timeout=15000)
    step("sidekick-refuses-to-decide", text=page.locator(".sk .msg.sk").last.inner_text()[:200]); shot(page, "12-sidekick-refuses")
    assert [a for a in actions() if a["request_id"] == mine["request_id"]][0]["state"] == "AWAITING_APPROVAL"
    # 2. unlink the Projects principal: the click is refused by the product
    httpx.delete(sentinel + "/api/v1/actor-links/projects:operator", headers=H).raise_for_status()
    page.locator(".sk .x").click()
    page.goto(projects + "/#/today"); page.reload(); page.wait_for_timeout(1500)
    page.locator("button.trow", has_text="Approve ban of 198.51.100.41").first.click()
    page.get_by_role("button", name=re.compile("Approve & send")).click()
    page.wait_for_timeout(2500)
    note = page.locator(".banner").first.inner_text(); step("unlinked-refused", note=note); shot(page, "13-unlinked-refused")
    assert [a for a in actions() if a["request_id"] == mine["request_id"]][0]["state"] == "AWAITING_APPROVAL"
    httpx.post(sentinel + "/api/v1/actor-links", headers=H, json={"external_actor": "projects:operator", "user": "alex"}).raise_for_status()
    # 3. the product is stopped: explicit unavailable rows, no fake missions
    subprocess.run(["docker", "compose", "stop", "edge-sentinel"], cwd=compose, check=True, capture_output=True)
    time.sleep(7)
    page.goto(projects + "/#/apps"); page.reload(); page.wait_for_timeout(2500)
    expect(page.get_by_text("Edge Sentinel").first).to_be_visible(); step("apps-unavailable", text=page.locator("main").inner_text()[:600]); shot(page, "14-apps-unavailable")
    page.goto(projects + "/#/missions"); page.reload(); page.wait_for_timeout(2000)
    expect(page.get_by_text("Edge Sentinel is unavailable").first).to_be_visible(); shot(page, "15-missions-unavailable")
    page.goto(projects + "/#/attention"); page.reload(); page.wait_for_timeout(2000)
    expect(page.get_by_text("Reconnect Edge Sentinel").first).to_be_visible(); shot(page, "16-attention-unavailable")
    page.goto(projects + "/#/today"); page.reload(); page.wait_for_timeout(500)
    page.get_by_label("Ask Sidekick").fill("show Edge Sentinel posture"); page.keyboard.press("Enter")
    expect(page.locator(".sk .msg.sk").last).to_contain_text("unavailable", timeout=15000); step("sidekick-unavailable", text=page.locator(".sk .msg.sk").last.inner_text()); shot(page, "17-sidekick-unavailable")
    subprocess.run(["docker", "compose", "start", "edge-sentinel"], cwd=compose, check=True, capture_output=True)
    b.close()
json.dump(T, open(out / "transcript.json", "w"), indent=2); print("OK")
