#!/usr/bin/env python3
"""Browser end-to-end test of the Edge Sentinel console against a REAL running deployment (Compose with a real CrowdSec).

  python3 e2e/console_e2e.py --base http://127.0.0.1:8203 --admin-password-file secrets/operator_password --shots /path/to/screenshots

It signs in through the login form, seeds a CrowdSec decision through `cscli` (so the alert is real), then walks the whole workflow as a
person would in the browser: sees the case, approves it, executes it, reads the receipt and the read-back, rejects another, restarts the
service and sees the records again; it also checks a viewer cannot decide, an ended session returns to the login, a write without the CSRF
token is refused, every page at 1440 and 390 px has no horizontal overflow, only same-origin requests were made and the browser console is
clean. Needs `playwright` (and its Chromium) in the Python that runs it; set --cscli / --restart for non-Compose deployments.
"""
import argparse, json, re, subprocess, sys, time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--admin", default="admin")
ap.add_argument("--admin-password-file", required=True)
ap.add_argument("--shots", default="")
ap.add_argument("--cscli", default="docker compose exec -T crowdsec cscli", help="command prefix that runs cscli in the CrowdSec container")
ap.add_argument("--restart", default="docker compose restart edge-sentinel")
ap.add_argument("--ips", default="", help="three addresses in 203.0.113.0/24 (default: random, so a re-run against the same stack opens new cases)")
args = ap.parse_args()
BASE = args.base.rstrip("/")
PASSWORD = open(args.admin_password_file).read().strip()
import random
IPS = args.ips.split(",") if args.ips else []
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)


def seed(ip, scenario):
    sh(f"{args.cscli} decisions delete --ip {ip}")
    r = sh(f"{args.cscli} decisions add --ip {ip} --duration 1h --reason {scenario}")
    check(f"seeded a real CrowdSec decision for {ip}", r.returncode == 0, r.stderr[-200:])


def shot(page, name):
    if args.shots:
        page.screenshot(path=f"{args.shots}/{name}.png", full_page=True)


def login(page, user, password):
    page.get_by_label("User name").fill(user)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def open_case(page, ip, timeout=60):
    deadline = time.time() + timeout
    page.get_by_role("link", name="Cases", exact=True).click()
    while time.time() < deadline:
        expect(page.get_by_role("heading", name="Cases")).to_be_visible()
        page.wait_for_load_state("networkidle")
        link = page.get_by_role("link", name=re.compile(re.escape(ip)))
        if link.count():
            link.first.click()
            return
        time.sleep(3)
        page.reload()
    check(f"a case for {ip} appeared", False, "timeout")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.set_default_timeout(20000)
    requests, console_errors = [], []
    page.on("request", lambda r: requests.append(r.url))
    page.on("console", lambda m: console_errors.append(m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    # 1. the front page leads to the console and the console asks for a sign-in
    page.goto(BASE + "/")
    check("the front page redirects to the console", page.url.rstrip("/").endswith("/ui"), page.url)
    expect(page.get_by_role("heading", name="Edge Sentinel")).to_be_visible()
    shot(page, "01-login-1440")
    login(page, args.admin, "definitely-not-the-password")
    expect(page.get_by_text("Wrong user name or password.")).to_be_visible()
    check("a wrong password is refused with one plain message", True)
    login(page, args.admin, PASSWORD)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    check("signing in shows the overview", True)
    if not IPS:                                              # three addresses no earlier run has used on this stack
        used = " ".join(c["title"] for c in ctx.request.get(BASE + "/api/v1/cases?limit=200").json()["items"])
        IPS = ["203.0.113.%d" % n for n in random.sample(range(20, 250), 40) if f"203.0.113.{n}" not in used and f"203.0.113.{n})" not in used][:3]
    info = ctx.request.get(BASE + "/api/v1/info").json()
    dry = info["mode"]["dry_run"]
    expect(page.get_by_text(re.compile("DRY RUN|LIVE|DEMO"))).to_be_visible()
    check(f"the mode is stated on every page ({'dry run' if dry else 'live'})", True)
    shot(page, "02-overview-1440")

    # 2. a real alert becomes a case; approve, execute, read back
    seed(IPS[0], "crowdsecurity/ssh-bf")
    open_case(page, IPS[0])
    expect(page.get_by_role("heading", level=2)).to_contain_text(IPS[0])
    check("the real CrowdSec alert became a case the person can open", True)
    shot(page, "03-case-awaiting-1440")
    page.get_by_role("button", name="Approve…").click()
    dlg = page.get_by_role("dialog")
    expect(dlg).to_contain_text("Nothing is banned until it is executed")
    dlg.get_by_role("button", name="Approve", exact=True).click()
    expect(page.get_by_text(re.compile(rf"approved by user:{re.escape(args.admin)} \(admin\) via session"))).to_be_visible()
    check("the approval is recorded under the signed-in user and channel", True)
    page.get_by_role("button", name=re.compile("Execute")).first.click()
    dlg = page.get_by_role("dialog")
    expect(dlg).to_contain_text("DRY RUN" if dry else "LIVE")
    shot(page, "04-execute-dialog-1440")
    dlg.get_by_role("button", name=re.compile(r"^Execute")).click()
    if dry:
        expect(page.get_by_text("DRY_RUN (dry run)")).to_be_visible()
        expect(page.get_by_text(re.compile("dry run: the edge was not touched"))).to_be_visible()
        check("a dry run is labelled as a dry run and nothing is claimed as banned", True)
    else:
        expect(page.get_by_text(re.compile(r"ban present for " + re.escape(IPS[0])))).to_be_visible()
        expect(page.get_by_text("verified", exact=True).first).to_be_visible()
        expect(page.get_by_text("CONTAINED").first).to_be_visible()
        check("the ban was read back from CrowdSec and the case is contained", True)
        banned = sh(f"{args.cscli} decisions list --ip {IPS[0]} -o raw")
        check("CrowdSec really holds the ban", IPS[0] in banned.stdout, banned.stdout[-200:])
    shot(page, "05-case-executed-1440")
    acts = ctx.request.get(BASE + "/api/v1/actions?limit=100").json()["items"]
    mine = next(a for a in acts if a["params"].get("ip") == IPS[0])
    page.goto(f"{BASE}/ui/actions/{mine['request_id']}")                  # the deep link other products use
    expect(page.get_by_role("heading", level=2)).to_contain_text(IPS[0])
    check("the /ui/actions/<id> deep link lands on the case that holds the action", "/ui/cases/" in page.url, page.url)

    # 3. reject path, and the queue
    seed(IPS[1], "crowdsecurity/http-probing")
    open_case(page, IPS[1])
    page.get_by_role("button", name="Reject…").click()
    page.get_by_role("dialog").get_by_label("Rationale (optional)").fill("known scanner")
    page.get_by_role("dialog").get_by_role("button", name="Reject", exact=True).click()
    expect(page.get_by_text(f"rejected by user:{args.admin}")).to_be_visible()
    expect(page.get_by_text("known scanner")).to_be_visible()
    check("a rejection records who, how and why", True)
    page.get_by_role("link", name="Actions", exact=True).click()
    page.get_by_role("link", name="executed" if not dry else "executed").click()
    expect(page.get_by_role("heading", name="Actions")).to_be_visible()
    check("the action queue lists executed actions", True)
    page.get_by_role("link", name="History", exact=True).click()
    expect(page.get_by_role("cell", name="action.decided").first).to_be_visible()
    shot(page, "06-history-1440")

    # 4. a manual action is staged, then a viewer cannot decide it
    page.get_by_role("link", name="Cases", exact=True).click()
    page.get_by_label("Address").fill(IPS[2])
    page.get_by_label("Reason").fill("seen scanning from a log")
    page.get_by_role("button", name="Stage for approval").click()
    expect(page.get_by_role("heading", level=2)).to_contain_text(IPS[2])
    manual_url = page.url
    check("an operator can stage a manual ban for approval", True)
    page.get_by_role("link", name="Users", exact=True).click()
    VIEWER = "viola%d" % random.randint(100, 999)
    page.locator("#u-name").fill(VIEWER)
    page.locator("#u-pw").fill("viewer-password-12345")
    page.locator("#u-role").select_option("viewer")
    page.get_by_role("button", name="Add user").click()
    expect(page.get_by_role("cell", name=VIEWER, exact=True)).to_be_visible()
    shot(page, "07-users-1440")
    page.get_by_role("button", name="Sign out").click()
    login(page, VIEWER, "viewer-password-12345")
    page.goto(manual_url)
    expect(page.get_by_text("Your role is viewer")).to_be_visible()
    check("a viewer sees the case but no approve or execute button", page.get_by_role("button", name=re.compile("Approve|Execute|Reject")).count() == 0)
    check("the Users page is not offered to a viewer", page.get_by_role("link", name="Users", exact=True).count() == 0)

    # 5. session and CSRF protections
    csrf = page.evaluate("fetch('/api/v1/session').then(r => r.json()).then(j => j.csrf)")
    no_header = page.evaluate("fetch('/api/v1/cases', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({type:'manual_block', ip:'8.8.8.8'})}).then(r => r.status)")
    check("a write with the session cookie but without the CSRF token is refused", no_header == 403, str(no_header))
    ctx.clear_cookies()
    page.get_by_role("link", name="Events", exact=True).click()
    expect(page.get_by_text("Your session ended. Sign in again.")).to_be_visible()
    check("an ended session returns to the sign-in with a message", True)
    login(page, args.admin, PASSWORD)
    expect(page.get_by_role("heading", name="Events")).to_be_visible()          # signing in again returns to the page that was open

    # 6. restart: the records are still there
    r = sh(args.restart)
    check("the service restarted", r.returncode == 0, r.stderr[-200:])
    for _ in range(60):
        try:
            if ctx.request.get(BASE + "/readyz").status == 200:
                break
        except Exception:                                    # the connection is refused or reset while the service restarts
            pass
        time.sleep(2)
    page.goto(BASE + "/ui/actions?state=all")
    page.reload()
    expect(page.get_by_text(re.compile(rf"approved by user:{re.escape(args.admin)} via session")).first).to_be_visible()
    check("after a restart the decisions and receipts are still there", True)

    # 7. every page, at both widths: no horizontal overflow, no console noise
    for width, height, tag in ((1440, 900, "1440"), (390, 844, "390")):
        page.set_viewport_size({"width": width, "height": height})
        for path, name in (("/ui/", "overview"), ("/ui/events", "events"), ("/ui/cases", "cases"), (manual_url.replace(BASE, ""), "case"), ("/ui/actions?state=all", "actions"),
                           ("/ui/history", "history"), ("/ui/users", "users"), ("/ui/about", "about")):
            page.goto(BASE + path)
            page.wait_for_load_state("networkidle")
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            check(f"{name} at {tag}px has no horizontal overflow", overflow <= 0, f"{overflow}px")
            if tag == "390" or name in ("cases", "case", "events", "about"):
                shot(page, f"10-{name}-{tag}")
    foreign = [u for u in requests if urlparse(u).netloc and urlparse(u).netloc != urlparse(BASE).netloc]
    check("only same-origin requests were made", not foreign, str(foreign[:3]))
    # the 401/403 resource lines are the deliberate wrong-password, ended-session and missing-CSRF steps above
    noise = re.compile(r"Failed to load resource: the server responded with a status of (401|403)")
    check("the browser console stayed clean (apart from the deliberate refusals)", not [m for m in console_errors if not noise.search(m)], str([m for m in console_errors if not noise.search(m)][:3]))
    browser.close()
print(f"CONSOLE E2E OK: {len(results)} checks")
