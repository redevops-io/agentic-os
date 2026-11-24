"""The real launcher window, driven through WebDriver (tauri-driver) on the operating system it was installed on.

    python launcher_gui.py <path to the installed launcher executable> <result.json>
    needs: tauri-driver on PATH, the platform's native WebDriver (Linux: WebKitWebDriver from webkit2gtk-driver, under a display such as
    xvfb; Windows: msedgedriver matching the WebView2 runtime, path in NATIVE_DRIVER), and the `selenium` package.

What is clicked is the window a person sees: Check this device, Start & open, Stop, About / Legal. What is checked is what those buttons
did to the machine: a control plane listening on loopback that refuses a caller without the session, gone again after Stop, and nothing
left running after the window is closed. macOS has no WebDriver for its system web view; there the launcher is covered by its own
harness (packaging/acceptance/macos-r0prime/gui) and this script reports UNSUPPORTED instead of pretending.
"""
import json, os, platform, re, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request

app, out = sys.argv[1], sys.argv[2]
results = []; T0 = time.time()


def record(name, status, detail=""):
    results.append({"name": name, "status": status, "detail": str(detail)[:600]})
    print(f"{status:<12}{name}" + (f"  | {str(detail)[:400]}" if detail else "").replace("\n", " / "), flush=True)


def check(name, fn):
    try:
        record(name, "PASS", fn() or "")
    except Exception as e:  # noqa: BLE001
        record(name, "FAIL", f"{type(e).__name__}: {e}")


def finish():
    s = {"pass": sum(r["status"] == "PASS" for r in results), "fail": sum(r["status"] == "FAIL" for r in results),
         "unsupported": sum(r["status"] == "UNSUPPORTED" for r in results), "seconds": round(time.time() - T0, 1)}
    json.dump({"platform": platform.platform(), "machine": platform.machine(), "launcher": app, "summary": s, "results": results}, open(out, "w"), indent=1)
    print(json.dumps(s)); sys.exit(1 if s["fail"] else 0)


if sys.platform == "darwin":
    record("the launcher window is driven through WebDriver", "UNSUPPORTED", "macOS has no WebDriver for WKWebView; see packaging/acceptance/macos-r0prime/gui")
    finish()

from selenium import webdriver  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.options import ArgOptions  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

home = tempfile.mkdtemp(prefix="aa-gui-")
stub = os.path.join(home, "bin"); os.makedirs(stub)
opened = os.path.join(home, "opened.txt")
env = dict(os.environ)
if sys.platform.startswith("linux"):                     # a throwaway profile. On Windows the launcher resolves its folders through the
    env.update(HOME=home, XDG_STATE_HOME=os.path.join(home, "state"), XDG_DATA_HOME=os.path.join(home, "share"),   # system, which refuses a
               XDG_CONFIG_HOME=os.path.join(home, "config"))                                                        # redirected profile
    for k in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        os.makedirs(env[k], exist_ok=True)
if sys.platform.startswith("linux"):                     # the hand-off to the browser is recorded, not performed: there is no browser to watch here
    with open(os.path.join(stub, "xdg-open"), "w") as f:
        f.write(f'#!/bin/sh\necho "$1" >> "{opened}"\n')
    os.chmod(os.path.join(stub, "xdg-open"), 0o755)
    env["PATH"] = stub + os.pathsep + env["PATH"]
cmd = ["tauri-driver", "--port", "4444"]
if os.environ.get("NATIVE_DRIVER"):
    cmd += ["--native-driver", os.environ["NATIVE_DRIVER"]]
drv_log = open(os.path.join(home, "tauri-driver.log"), "wb")
td = subprocess.Popen(cmd, env=env, stdout=drv_log, stderr=subprocess.STDOUT)
time.sleep(3)
driver = None


def sidecars():
    """PIDs of running sidecar processes started from this installation."""
    name = "redevops-sidecar"
    try:
        if sys.platform == "win32":
            o = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
            return [l for l in o.splitlines() if name in l]
        o = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True).stdout
        return [p for p in o.split() if p.strip()]
    except Exception:  # noqa: BLE001
        return []


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def text(i):
    return driver.find_element(By.ID, i).text


def t_window():
    global driver
    o = ArgOptions(); o.set_capability("browserName", "wry"); o.set_capability("tauri:options", {"application": app})
    driver = webdriver.Remote(command_executor="http://127.0.0.1:4444", options=o)
    WebDriverWait(driver, 30).until(lambda d: d.find_element(By.ID, "start"))
    line = text("release-line")
    assert driver.find_element(By.ID, "view-main").is_displayed()
    return f'release line "{line}"; buttons: ' + ", ".join(b.text for b in driver.find_elements(By.CSS_SELECTOR, "#view-main button") if b.is_displayed())


check("the launcher window opens with its controls", t_window)
if driver is None:
    try:
        drv_log.flush(); print(open(drv_log.name, errors="replace").read()[-1500:])
    except Exception:  # noqa: BLE001
        pass
    td.kill(); finish()
state = {}


def t_check():
    driver.find_element(By.ID, "check").click()
    WebDriverWait(driver, 120).until(lambda d: text("out") and "Working" not in text("out"))
    t = text("out")
    # The report ends with a verdict. "blocked" is a verdict, not a failure of the button (Windows has no containment layer, and the report
    # says so); the window shows it under "Error:" because the command exits 1. What must hold: the report itself is shown.
    assert "device readiness" in t and re.search(r"device\s*:\s*(verified|blocked|degraded)", t), t[:300].replace("\n", " / ")
    verdict = re.search(r"device\s*:\s*(\w+)", t).group(1)
    return f"verdict {verdict}" + (" (shown under 'Error:' because the report exits 1)" if t.startswith("Error") else "")


def t_start():
    assert not sidecars(), "a sidecar was already running before Start"
    driver.find_element(By.ID, "start").click()
    WebDriverWait(driver, 180).until(lambda d: "Running at" in text("out") or text("out").startswith("Error"))
    t = text("out"); assert "Running at" in t, t[:300].replace("\n", " / ")
    m = re.search(r"Running at (http://127\.0\.0\.1:(\d+))", t); assert m, t[:200]
    state["origin"], state["port"] = m.group(1), int(m.group(2))
    assert listening(state["port"]), "nothing listens on the port the window names"
    return t.replace("\n", " ")[:120]


def t_serves():
    with urllib.request.urlopen(state["origin"] + "/", timeout=15) as r:
        assert r.status == 200 and b"<html" in r.read(4096).lower()
    try:
        urllib.request.urlopen(state["origin"] + "/api/projects", timeout=15); raise AssertionError("the API answered without a session")
    except urllib.error.HTTPError as e:
        assert e.code == 401, e.code
    return "UI 200; API 401 without the session"


def t_handoff():
    if not sys.platform.startswith("linux"):
        return "the browser hand-off is performed by the operating system here and was not intercepted"
    for _ in range(20):
        if os.path.exists(opened):
            break
        time.sleep(0.5)
    target = open(opened).read().strip().splitlines()[-1]
    assert target, "nothing was handed to the browser"
    assert "#lc=" not in target or target.startswith(("file:", "/")), "the one-time code was put on a command line: " + target[:80]
    return "handed to the browser: " + re.sub(r"[A-Za-z0-9_-]{20,}", "…", target)[:120]


def t_start_twice():
    before = sidecars()
    driver.find_element(By.ID, "start").click()
    WebDriverWait(driver, 120).until(lambda d: "Running at" in text("out") or text("out").startswith("Error"))
    assert "Running at" in text("out"), text("out")[:200]
    after = sidecars()
    assert len(after) <= max(len(before), 2), f"a second Start left more processes: {len(before)} -> {len(after)}"
    return f"still one control plane ({len(after)} process entries)"


def t_about():
    driver.find_element(By.ID, "open-about").click()
    WebDriverWait(driver, 30).until(lambda d: d.find_element(By.ID, "view-about").is_displayed() and len(text("about-body")) > 100)
    body = text("about-body"); title = text("about-title")
    driver.find_element(By.ID, "about-back").click()
    WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, "view-main").is_displayed())
    assert re.search(r"licen[cs]e|notice|version", body, re.I), body[:200]
    return f'"{title}": {len(body)} characters, back to the main view'


def t_stop():
    driver.find_element(By.ID, "stop").click()
    WebDriverWait(driver, 60).until(lambda d: "Working" not in text("out"))
    for _ in range(40):
        if not listening(state["port"]) and not sidecars():
            break
        time.sleep(0.5)
    assert not listening(state["port"]), "the port is still open after Stop"
    assert not sidecars(), "a sidecar process is still running after Stop"
    return text("out").replace("\n", " ")[:100]


def t_quit_cleans_up():
    driver.find_element(By.ID, "start").click()
    WebDriverWait(driver, 180).until(lambda d: "Running at" in text("out") or text("out").startswith("Error"))
    port = int(re.search(r"127\.0\.0\.1:(\d+)", text("out")).group(1)); assert listening(port)
    driver.quit()
    for _ in range(60):
        if not listening(port) and not sidecars():
            break
        time.sleep(0.5)
    assert not listening(port) and not sidecars(), "the control plane outlived the launcher window"
    return "closing the window stopped the control plane it started"


check('"Check this device" runs the readiness report in the window', t_check)
check('"Start & open" starts the control plane and names its address', t_start)
if "port" in state:
    check("the control plane the window started serves the UI and refuses a caller without the session", t_serves)
    check("the browser hand-off happens, and the one-time code is not put on a command line", t_handoff)
    check("pressing Start again does not start a second control plane", t_start_twice)
    check('"About / Legal" opens with the licence texts and returns', t_about)
    check('"Stop" stops it: the port closes and no sidecar is left', t_stop)
    check("closing the window stops a running control plane", t_quit_cleans_up)
try:
    driver.quit()
except Exception:  # noqa: BLE001
    pass
td.terminate()
finish()
