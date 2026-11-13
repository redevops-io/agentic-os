"""The REAL Infinity runtime (infinity-agent-core 0.2.0, embedded by harness/) against this bridge, on loopback.

Real: the Infinity event processor and thread model, its built-in spawn_thread, its RAP tool dispatch and toolset loader, its callback-to-input
conversion, and the wire format it produces (including ``thread_ancestors`` for a child thread). Scripted: the model (no LLM, no credentials).
Selected with ``-m sidecar`` and the path of the built harness in RAP_HARNESS_BIN; not part of the default run (it needs a Rust build).
"""
import http.client
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse

import pytest

from redevops_infinity import CallbackClient, RapHttpServer
from redevops_infinity.callback_auth import sign_request
from redevops_infinity.demo.env import AUDIENCE, KEY_ID, SECRET, TENANT, build_env, loopback_origin

pytestmark = pytest.mark.sidecar
BIN = os.environ.get("RAP_HARNESS_BIN", "")


class Recording(CallbackClient):
    def __init__(self):
        super().__init__()
        self.sent = []

    def post(self, url, body, headers, origins):
        out = super().post(url, body, headers, origins)
        self.sent.append((url, body, dict(headers), out))
        return out


@pytest.fixture
def stack(tmp_path):
    if not BIN or not os.path.exists(BIN):
        pytest.fail("RAP_HARNESS_BIN must name the built harness binary")
    rec = Recording()
    env = build_env(str(tmp_path / "w"), clock=time.time, callbacks=rec)
    srv = RapHttpServer(env.bridge).start()
    env.bridge.start()
    yield env, srv, rec, tmp_path
    env.bridge.stop()
    srv.stop()


def run_harness(env, srv, tmp_path, scenario, tool, args, hold=0, attack=None):
    go = str(tmp_path / "go")
    penv = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HARNESS_BRIDGE_URL": srv.base_url, "HARNESS_KEY_ID": KEY_ID, "HARNESS_SECRET": SECRET.decode(),
            "HARNESS_AUDIENCE": AUDIENCE, "HARNESS_TENANT": TENANT, "HARNESS_SCENARIO": scenario, "HARNESS_TOOL": tool, "HARNESS_ARGS": json.dumps(args),
            "HARNESS_GO_FILE": go, "HARNESS_HOLD_S": str(hold)}
    p = subprocess.Popen([BIN], env=penv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines, origin, summary = [], None, None
    try:
        for line in p.stdout:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            lines.append(o)
            if o.get("harness") == "ready":
                origin = o["callback_origin"]
                env.peer.callback_origins = (loopback_origin(int(origin.rsplit(":", 1)[1])),)
                open(go, "w").close()
            if "summary" in o:
                summary = o["summary"]
                if attack:
                    attack(origin, lines)
        p.wait(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    return lines, summary, p.returncode, p.stderr.read()


def test_the_real_runtime_calls_a_governed_write_hibernates_and_resumes_on_the_signed_result(stack):
    env, srv, rec, tmp = stack
    lines, summary, rc, err = run_harness(env, srv, tmp, "single", "update_lead", {"lead_id": "lead_dana01", "status": "qualified"})
    assert rc == 0 and summary["ok"] is True, (rc, err[-800:], lines[-5:])
    assert summary["callbacks_rejected"] == []
    kinds = [(o.get("event") or o.get("callback") or o.get("discovered")) for o in lines]
    assert "update_lead" in kinds and kinds.index("tool_call") < kinds.index("accepted") < kinds.index("agent_finished")
    assert env.crm.effect_count == 1
    rows = [dict(r) for r in env.store._conn().execute("select * from actions")]
    assert len(rows) == 1 and rows[0]["group_id"] == "root-thread" and rows[0]["status"] == "completed" and rows[0]["state"] == "decided"
    assert next(o for o in lines if o.get("event") == "agent_finished")["text"].find("completed") >= 0
    assert len(rec.sent) == 1 and rec.sent[0][3].kind == "delivered"


def test_a_child_thread_of_the_real_runtime_gets_an_attenuated_identity(stack):
    env, srv, rec, tmp = stack
    lines, summary, rc, err = run_harness(env, srv, tmp, "child", "lookup_lead", {"lead_id": "lead_dana01"})
    assert rc == 0 and summary["ok"] is True and len(summary["finished_threads"]) == 2, (rc, err[-800:], lines[-6:])
    row = [dict(r) for r in env.store._conn().execute("select * from lineage")]
    child = [r for r in row if r["child_id"]]
    assert len(child) == 1 and json.loads(child[0]["ancestors_json"]) == ["root-thread"]       # thread_ancestors as the real runtime sent it
    tree = env.spawner.tree(f"{TENANT}:infinity-runtime")
    assert len(tree) == 1 and tree[0]["principal"].startswith("agent:")


def test_the_real_runtime_child_cannot_use_an_operation_the_owner_withheld_from_children(stack):
    env, srv, rec, tmp = stack
    lines, summary, rc, err = run_harness(env, srv, tmp, "child", "update_lead", {"lead_id": "lead_dana01", "status": "qualified"})
    assert rc == 0 and summary["ok"] is True, (rc, err[-800:])
    assert env.crm.effect_count == 0 and env.crm.calls == []
    texts = " ".join(o.get("segments", "") for o in lines if o.get("event") == "tool_result")
    assert "policy_denied" in texts


def test_the_real_runtimes_receiver_rejects_replayed_forged_and_substituted_callbacks(stack):
    env, srv, rec, tmp = stack

    def attack(origin, lines):
        u = urllib.parse.urlsplit(rec.sent[0][0])
        url, body, headers, _ = rec.sent[0]

        def post(path, b, h):
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
            c.request("POST", path, body=b, headers={"Content-Type": "application/json", **h})
            r = c.getresponse()
            r.read()
            c.close()
            return r.status
        assert post(u.path, body, headers) == 401                                         # the exact captured request replayed: same nonce
        assert post(u.path, body, {k: v for k, v in headers.items() if k != "X-Rap-Signature"}) == 401   # signature removed
        assert post(u.path, body, {**headers, "X-Rap-Tenant": "other-tenant"}) == 401     # cross-tenant substitution: the signature binds the tenant
        assert post(u.path + "x", body, headers) == 404                                   # a guessed path
        forged = json.dumps({"type": "tool_result", "group_id": "root-thread", "id": "x", "text": "pwned"}).encode()
        assert post(u.path, forged, {}) == 401                                            # a forged result with no signature
        from redevops_infinity.callback_auth import callback_headers
        h2 = callback_headers(env.peer, url=url, body=body, event_id=headers["X-Rap-Event-Id"], operation="update_lead", group_id="root-thread",
                              tool_call_id=headers["X-Rap-Tool-Call-Id"], expires_at=float(headers["X-Rap-Expires"]))
        assert post(u.path, body, h2) == 200                                              # a legitimately re-signed retry of the same event: acknowledged, not applied twice

    lines, summary, rc, err = run_harness(env, srv, tmp, "single", "update_lead", {"lead_id": "lead_dana01", "status": "qualified"}, hold=4, attack=attack)
    assert rc == 0 and summary["ok"]
    verdicts = [o for o in lines if "callback" in o]
    assert sum(1 for o in verdicts if o["callback"] == "accepted") == 1
    assert sum(1 for o in verdicts if o["callback"] == "duplicate_dropped") == 1
    assert {o.get("why") for o in verdicts if o["callback"] == "rejected"} >= {"replayed_nonce", "malformed", "bad_signature"}
    assert sum(1 for o in lines if o.get("event") == "agent_finished") == 1
    assert env.crm.effect_count == 1
