"""Real saved data across an upgrade, and the governed actions of the installed build.

  data_probe.py seed    <sidecar> <home> <seed.json>              with the PREVIOUS build: save a note and connect an agent
  data_probe.py verify  <sidecar> <home> <out.json> [seed.json]    with the CANDIDATE: that data and connection are kept and
                                                                  work; then an agent's whole governed action on the installed
                                                                  binary, kept across a restart
  data_probe.py retained <home> <out.json>                        after uninstalling: the person's data is still there

The sidecar is run as a person runs it (``serve``, per-user directories under <home>), and the person's session comes from
the one-time launch link it prints. Everything is asked over its socket; nothing is read from its stores except to name
the files that must survive. Exit status 1 when any check fails."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

RESULTS: list = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    RESULTS.append({"check": name, "status": "pass" if ok else "fail", "detail": "" if ok else str(detail)[:600]})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{str(detail)[:400]}]"), flush=True)
    return ok


def environment(home: str, port: int) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "USERPROFILE": home, "APPDATA": os.path.join(home, "AppData"),
           "LOCALAPPDATA": os.path.join(home, "Local"), "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
           "XDG_DATA_HOME": os.path.join(home, ".local", "share"), "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
           "TEMP": os.environ.get("TEMP", tempfile.gettempdir()), "TMP": os.environ.get("TMP", tempfile.gettempdir()),
           "LANG": "C.UTF-8", "PROJECTS_PORT": str(port)}
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def request(url: str, *, method: str = "GET", body: object = None, headers: "dict | None" = None, token: str = ""):
    h = {"content-type": "application/json", **(headers or {}), **({"authorization": "Bearer " + token} if token else {})}
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class App:
    """The installed sidecar in one per-user home, started and stopped."""

    def __init__(self, sidecar: str, home: str):
        self.sidecar, self.home, self.runs = sidecar, os.path.abspath(home), 0
        os.makedirs(self.home, exist_ok=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.env, self.url, self.session, self.proc = environment(self.home, self.port), f"http://127.0.0.1:{self.port}", {}, None

    def start(self) -> None:
        self.runs += 1
        path = os.path.join(tempfile.mkdtemp(prefix="probe-log-"), "serve.log")
        with open(path, "wb") as log:
            self.proc = subprocess.Popen([self.sidecar, "serve", "--exit-on-stdin-eof"], stdin=subprocess.PIPE, stdout=log,
                                         stderr=subprocess.STDOUT, env=self.env)
        code = None
        for _ in range(240):
            with open(path, encoding="utf-8", errors="replace") as fh:
                found = re.search(r"#lc=([A-Za-z0-9_-]{43})", fh.read())
            if found:
                code = found.group(1)
                break
            if self.proc.poll() is not None:
                break
            time.sleep(0.5)
        if code is None:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raise SystemExit("the app did not start:\n" + fh.read()[-2000:])
        for _ in range(60):
            try:
                status, made = request(self.url + "/api/local/session", method="POST", body={"code": code})
                break
            except OSError:
                time.sleep(0.5)
        if status != 200:
            raise SystemExit(f"no session ({status}): {made}")
        self.session = {"x-agentic-session": made["session_token"], "x-agentic-csrf": made["csrf_token"]}

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=40)
        except Exception:  # noqa: BLE001
            self.proc.kill()

    def person(self, path: str, method: str = "GET", body: object = None):
        return request(self.url + path, method=method, body=body, headers=self.session)

    def agent(self, token: str, tool: str, **args):
        return request(self.url + f"/api/agent-gateway/capabilities/{tool}/invoke", method="POST", body=args, token=token)

    def command(self, *args: str) -> dict:
        r = subprocess.run([self.sidecar, "agent", *args], env=self.env, capture_output=True, text=True, timeout=120)
        try:
            return json.loads(r.stdout[r.stdout.index("{"):])
        except ValueError:
            return {"error": (r.stdout + r.stderr)[-600:]}

    def files(self) -> list:
        return sorted(os.path.relpath(os.path.join(d, f), self.home).replace("\\", "/") for d, _, fs in os.walk(self.home) for f in fs)


def seed(sidecar: str, home: str, out: str) -> None:
    app = App(sidecar, home)
    app.start()
    try:
        _, projects = app.person("/api/projects")
        project = projects[0]["id"]
        note = f"Remind me to call the Alvarez account about the renewal {uuid.uuid4().hex[:8]}"
        status, saved = app.person(f"/api/projects/{project}/capture", "POST", {"content": note})
        check("previous build: a note is saved", status == 200, [status, saved])
        made = app.command("connect", "--name", "upgrade-agent", "--grants", "projects.read")
        check("previous build: an agent is connected with a read permission", "agent" in made, made)
    finally:
        app.stop()
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"project": project, "note": note, "agent": made.get("agent", {}).get("id"), "token_file": made.get("token_file"),
                   "files": app.files()}, fh, indent=1)


def definition() -> dict:
    return {"schema": "aos.app-definition/v1", "id": "acceptance-notes", "name": "Acceptance notes", "version": 1, "summary": "Notes kept by the acceptance run.",
            "capabilities": [], "state": {"collections": [{"name": "notes", "max_records": 20, "fields": [{"name": "text", "type": "text", "required": True}]}]},
            "views": [{"id": "main", "title": "Notes", "blocks": [{"type": "records", "collection": "notes", "columns": ["text"]},
                                                                  {"type": "form", "collection": "notes", "submit": "Add"}]}]}


def verify(sidecar: str, home: str, seed_file: str) -> None:
    app = App(sidecar, home)
    app.start()
    try:
        if seed_file:
            with open(seed_file, encoding="utf-8") as fh:
                old = json.load(fh)
            lost = [f for f in old["files"] if f not in app.files() and not f.endswith(("-wal", "-shm", ".log")) and "/launch/" not in f
                    and not f.endswith("gateway.json")]
            check("upgrade: no file the previous build saved is gone", not lost, lost)
            _, kept = app.person(f"/api/projects/{old['project']}/capture")
            check("upgrade: the note saved by the previous build is there", any(old["note"] in json.dumps(c) for c in (kept if isinstance(kept, list) else [])), kept)
            _, agents = app.person("/api/local-agents")
            mine = [a for a in agents.get("agents", []) if a["id"] == old["agent"]]
            check("upgrade: the agent connected by the previous build is listed with the same permissions",
                  len(mine) == 1 and mine[0]["active"] and mine[0]["grants"] == ["projects.read"], mine)
            with open(old["token_file"], encoding="utf-8") as fh:
                token = fh.read().strip()
            status, out = app.agent(token, "projects.list")
            check("upgrade: its token and stored authority still work", status == 200 and out.get("status") == "ok", [status, out])
            status, out = app.agent(token, "apps.request_install", definition=definition(), idempotency_key="upgrade-000001")
            check("upgrade: it gained no permission to change anything", status != 200 or out.get("status") != "ok", [status, out])

        status, features = app.person("/api/desktop")
        check("the installed app offers approvals, agents and applications", status == 200 and all(features.values()), [status, features])
        refused = app.command("connect", "--name", "self-made", "--grants", "apps.install")
        check("the command line cannot give a permission that changes something", "only on the app" in str(refused.get("error", "")), refused)
        status, made = app.person("/api/local-agents", "POST", {"name": "acceptance", "grants": [
            "apps.use", "apps.records.read", "apps.records.write", "apps.install"], "days": 1})
        if not check("the person connects an agent with scoped permissions", status == 200, [status, made]):
            return
        with open(made["token_file"], encoding="utf-8") as fh:
            token = fh.read().strip()
        status, out = app.agent(token, "apps.request_install", definition=definition(), idempotency_key="acceptance-000001")
        asked = out.get("output", {})
        check("the agent proposes and nothing is installed yet", asked.get("state") == "pending"
              and app.person("/api/applications")[1].get("applications") == [], out)
        rid = asked.get("request_id", "")
        _, card = app.person(f"/api/approvals/writes/{rid}")
        check("the approval card explains it and folds the definition", card.get("waiting") is True and bool(card.get("technical")), card)
        status, done = app.person(f"/api/approvals/writes/{rid}/decision", "POST", {"decision": "approve", "digest": card.get("digest", "")})
        check("approving executes it, read back", status == 200 and done.get("state") == "executed_verified", [status, done])
        status, again = app.person(f"/api/approvals/writes/{rid}/decision", "POST", {"decision": "approve", "digest": card.get("digest", "")})
        check("the same approval cannot run it twice", status == 409, [status, again])
        _, out = app.agent(token, "apps.get", app_id="acceptance-notes")
        check("the agent reads the installed application back", out.get("output", {}).get("version") == 1, out)
        _, out = app.agent(token, "apps.state.add", app_id="acceptance-notes", collection="notes", values={"text": "from the agent"},
                           idempotency_key="acceptance-record-1")
        status, mine = app.person("/api/applications/acceptance-notes/collections/notes/records", "POST", {"values": {"text": "from the person"}})
        check("the agent and the person each add a record", out.get("status") == "ok" and status == 200, [out, mine])
        _, other = app.agent(token, "apps.request_install", definition={**definition(), "id": "to-reject", "name": "To reject"}, idempotency_key="acceptance-000002")
        other_id = other.get("output", {}).get("request_id", "")
        _, card = app.person(f"/api/approvals/writes/{other_id}")
        status, no = app.person(f"/api/approvals/writes/{other_id}/decision", "POST", {"decision": "reject", "digest": card.get("digest", "")})
        check("rejecting installs nothing", status == 200 and no.get("state") == "rejected"
              and [a["app_id"] for a in app.person("/api/applications")[1]["applications"]] == ["acceptance-notes"], [status, no])
        _, before = app.person("/api/applications/acceptance-notes")
        app.stop()
        app.start()
        _, after = app.person("/api/applications/acceptance-notes")
        check("after a restart the application, its records and the decision are kept", after.get("records") == before.get("records")
              and app.person(f"/api/approvals/writes/{rid}")[1].get("state") == "executed_verified", [before.get("records"), after.get("records")])
        status, bundle = app.person("/api/applications/acceptance-notes/export")
        check("the application exports with its records", status == 200 and len(bundle.get("state", {}).get("notes", [])) == 2, [status])
        status, _ = app.person(f"/api/local-agents/{made['agent']['id']}/revoke", "POST", {})
        gone, _ = app.agent(token, "apps.list")
        check("revoking the agent ends its access at once", status == 200 and gone == 401, [status, gone])
    finally:
        app.stop()


def retained(home: str) -> None:
    kept = [os.path.relpath(os.path.join(d, f), home).replace("\\", "/") for d, _, fs in os.walk(home) for f in fs]
    for needle in ("authority.db", "agents.json", "requests.sqlite", "apps.sqlite", "capture.db"):
        check(f"after uninstall the person's {needle} is still there", any(k.endswith(needle) for k in kept), sorted(kept)[:40])


def main() -> int:
    mode = sys.argv[1]
    if mode == "seed":
        seed(sys.argv[2], sys.argv[3], sys.argv[4])
        out = sys.argv[4] + ".checks.json"
    elif mode == "verify":
        out = sys.argv[4]
        try:
            verify(sys.argv[2], sys.argv[3], sys.argv[5] if len(sys.argv) > 5 else "")
        except Exception as e:  # noqa: BLE001 - a crash of the run is a failed run, recorded
            check("the run reached its end", False, f"{type(e).__name__}: {e}")
    elif mode == "retained":
        retained(sys.argv[2])
        out = sys.argv[3]
    else:
        raise SystemExit(__doc__)
    failed = sum(r["status"] == "fail" for r in RESULTS)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"results": RESULTS, "summary": {"pass": len(RESULTS) - failed, "fail": failed}}, fh, indent=1)
    print(f"{len(RESULTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
