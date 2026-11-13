"""Standalone local demo: ``python -m redevops_infinity.demo.run``.

An EMULATED RAP runtime (written from the specification, see demo/peer.py) invokes governed actions through the bridge on loopback. The
runtime keeps no process per call: it POSTs, receives 200, and holds only a row of data until the signed result arrives. Everything is
synthetic: a mock CRM, no provider, no cloud, no model. The process refuses any connection that is not to a loopback address.
The real Infinity runtime is exercised separately by integrations/infinity/harness (Rust).
"""
from __future__ import annotations

import contextlib
import json
import shutil
import socket
import sys
import tempfile
import time

from ..http_server import RapHttpServer
from .env import AUDIENCE, KEY_ID, SECRET, TENANT, build_env, loopback_origin
from .peer import EmulatedRuntime


@contextlib.contextmanager
def loopback_only():
    real = socket.socket.connect

    def guard(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"the demo refuses a connection to {host!r}")
        return real(self, address, *a, **k)
    socket.socket.connect = guard           # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real        # type: ignore[method-assign]


def _payload(msg: dict) -> dict:
    t = msg["text"]
    return json.loads(t[7:] if t.startswith("Error: ") else t)


def main() -> int:
    work = tempfile.mkdtemp(prefix="rap-demo-")
    checks = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))
        print(f"  check {name} = {bool(ok)}")

    try:
        with loopback_only():
            env = build_env(work)
            srv = RapHttpServer(env.bridge).start()
            rt = EmulatedRuntime(srv.base_url, KEY_ID, SECRET, AUDIENCE, TENANT)
            env.peer.callback_origins = (loopback_origin(rt.cb_port),)
            env.bridge.start()
            try:
                st, ts = rt.discover()
                print(f"discovered {len(ts['tools'])} tools: {[t['name'] for t in ts['tools']]}")
                check("discovery is authenticated and lists the declared operations", st == 200 and {"update_lead", "send_followup"} <= {t["name"] for t in ts["tools"]})

                print("1. a governed write: the runtime hibernates, the signed result wakes it")
                code, c1 = rt.invoke("update_lead", {"lead_id": "lead_dana01", "status": "qualified"})
                m = rt.await_result("thread-root", c1, 10)
                p = _payload(m)
                print(f"   ack={code}  result={p['status']} receipt={p.get('receipt_ref')}")
                check("invoke acknowledged, then one completed result with a downstream receipt", code == 200 and p["status"] == "completed" and p["receipt_ref"])
                check("the effect happened exactly once", env.crm.effect_count == 1)

                print("2. the same intent asked again under a new tool-call id (a model retry)")
                _, c2 = rt.invoke("update_lead", {"lead_id": "lead_dana01", "status": "qualified"})
                p2 = _payload(rt.await_result("thread-root", c2, 10))
                check("replayed from the record, not executed again", p2["reason"] == "replayed" and env.crm.effect_count == 1)

                print("3. a consequential action waits for a named person in the approvals inbox")
                _, c3 = rt.invoke("send_followup", {"contact": "lead_dana01", "body": "Hi Dana, here is the link we discussed."})
                time.sleep(0.8)
                pend = env.inbox.pending()
                check("parked: nothing sent, nothing delivered", len(pend) == 1 and rt.await_result("thread-root", c3, 0.1) is None and env.crm.effect_count == 1)
                env.inbox.approve(pend[0]["id"], by="person:ops-lead")
                env.bridge.resume_awaiting()
                p3 = _payload(rt.await_result("thread-root", c3, 10))
                check("after approval it ran once and returned a receipt", p3["status"] == "completed" and env.crm.effect_count == 2)

                print("4. an answer lost after the effect is outcome_unknown and is not sent again")
                env.crm.lose_answer_once = True
                _, c4 = rt.invoke("update_lead", {"lead_id": "lead_omar02", "status": "nurture"})
                p4 = _payload(rt.await_result("thread-root", c4, 10))
                _, c5 = rt.invoke("update_lead", {"lead_id": "lead_omar02", "status": "nurture"})
                p5 = _payload(rt.await_result("thread-root", c5, 10))
                check("unknown, and the retry did not repeat the write", p4["status"] == p5["status"] == "outcome_unknown" and env.crm.calls.count("update") == 2)

                print("5. a child thread acts under an attenuated identity")
                _, c6 = rt.invoke("update_lead", {"lead_id": "lead_dana01", "status": "nurture"}, group_id="child-1", ancestors=("thread-root",))
                p6 = _payload(rt.await_result("child-1", c6, 10))
                _, c7 = rt.invoke("lookup_lead", {"lead_id": "lead_dana01"}, group_id="child-1", ancestors=("thread-root",))
                p7 = _payload(rt.await_result("child-1", c7, 10))
                check("the child may read but not write", p6["status"] == "denied" and p7["status"] == "completed")
                check("callbacks were all authenticated (none rejected)", rt.rejected == [])
                check("the audit journal is an unbroken hash chain", env.journal.verify().ok)
            finally:
                env.bridge.stop()
                srv.stop()
                rt.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"RESULT {len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
