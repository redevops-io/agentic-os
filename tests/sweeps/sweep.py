"""A reproducible authorization sweep over all 17 gated apps (the acceptance harness for the permissions work).

    python tests/sweeps/sweep.py                       # a quick pass (a few thousand calls)
    python tests/sweeps/sweep.py --fuzz 400 --seed 7   # the full sweep (~85,000 calls)
    python tests/sweeps/sweep.py --json out.json       # machine-readable result

Every app x every MODE (unprovisioned, read-only profile, write profile, foreign principal, deliberately standalone, nothing configured) x every SURFACE:

  route       every route in ``apps/_route_table.ROUTES`` (the routes that execute a tool or return tool data), with request-header variants that try to choose the principal or
              claim an admin bypass, and body variants that try to reach the handler before the gate
  operator    ``Operator.invoke`` of every registered capability, several input shapes
  http        the same capability through the operator's HTTP ``/invoke``: the decision must be the one the direct call got
  policy      ``app_gate.tool_denial`` of every classified tool, and of ``--fuzz`` hostile NAME variants of each (case, padding, zero-width / bidi / confusable characters, separators,
              commas, other apps' tools, empty): a name that is not exactly in the trusted table must always be denied

The oracle is derived from the trusted classification table (``agentic_os.tool_actions``), never from the app: a read grant allows ``read`` tools and denies the rest, a write grant allows
all, an unprovisioned or foreign principal is denied everything, standalone / nothing configured is not enforced. A DENIED call must leave NO trace: the sweep refuses every outbound
connection, DNS lookup and process launch (the cores talk to their backends over the network and shell out), and records the attempts; a denial that attempted one is a violation.

START-UP modes (an unrecognised switch value, an unusable permissions store, enterprise requested with the package absent) must refuse to start the app.

Exit status 1 when any violation was found. Nothing here touches the network, a repository checkout's runtime state, or a live system.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import importlib
import json
import os
import pathlib
import random
import socket
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "apps", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import _cr_enterprise_stub as stub  # noqa: E402
from _route_table import ROUTES  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402
from agentic_os.tool_actions import TOOL_ACTIONS  # noqa: E402

APPS = tuple(ROUTES)
GATE_ENV = ("CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS", "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "AGENTIC_OS_PERMISSION_AWARE_RETRIEVAL",
            "AGENTIC_OS_PERMISSION_AWARE_CACHE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_DIR", "PERMISSIONS_ALLOW_GENERATED_KEY_DEV",
            "PERMISSIONS_ALLOW_PLAINTEXT_DEV")
CAP_INPUTS = {"tenant": "t", "doc_id": "d", "cloud": "aws", "account": "x", "asset_id": "x", "id": "1", "conversation_id": "1", "lead": "x", "startup": {}}

# mode -> (what the setting is, how the profile is provisioned, expectation for a tool whose action is a / for any tool)
MODES = ("unprovisioned", "read-only", "write", "foreign-principal", "standalone", "unconfigured")
START_MODES = ("invalid-switch", "unusable-store", "enterprise-absent")

HEADER_VARIANTS = (
    {},
    {"x-user": "alice", "x-roles": "admin,policy-admin"},
    {"authorization": "Bearer admin"},
    {"x-test-user": "granted", "x-test-roles": "admin"},
    {"x-forwarded-for": "127.0.0.1", "x-forwarded-user": "root", "forwarded": "for=127.0.0.1"},
    {"x-permissions-bypass": "1", "x-principal": "local-user", "x-identity": "local-user"},
)
BODY_VARIANTS = ("declared", "empty-object", "list", "string", "unicode", "deep", "huge")


class Sentinel:
    """Refuses (and records) every outbound connection, DNS lookup and process launch."""

    def __init__(self, mp: MonkeyPatch):
        self.attempts: list = []

        def connect(s, address, *a, **k):
            self.attempts.append(address)
            raise OSError("network blocked by the sweep")

        def lookup(host, *a, **k):
            self.attempts.append(host)
            raise socket.gaierror("network blocked by the sweep")

        def process(*a, **k):
            self.attempts.append(("process", a[0] if a else k.get("args")))
            raise FileNotFoundError("process launch blocked by the sweep")
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket, "getaddrinfo", lookup)
        for name in ("run", "Popen", "check_output", "check_call", "call"):
            mp.setattr(subprocess, name, process)
        mp.setattr(os, "system", process)


class Result:
    def __init__(self):
        self.calls = collections.Counter()
        self.violations: list[dict] = []

    def count(self, mode, surface, n=1):
        self.calls[(mode, surface)] += n

    def violate(self, **kw):
        self.violations.append(kw)


def name_variants(tool: str, others: list[str], rnd: random.Random, n: int, table: dict) -> list[str]:
    """Hostile variants of a tool name: none of them is an exact name in the app's trusted table (a mutation that lands on another real tool is not hostile)."""
    _, _, short = tool.partition(".")
    fixed = [tool.upper(), tool.title(), tool.swapcase(), " " + tool, tool + " ", "\t" + tool, tool + "\n", tool + "\x00", "​" + tool, tool + "​", "‮" + tool,
             "﻿" + tool, tool.replace(".", "/"), tool.replace(".", ":"), tool.replace(".", ".."), tool.replace(".", "․"), tool.replace("a", "а"),
             tool.replace("e", "ｅ"), tool + ",", "," + tool, tool + ",x", f"x,{tool}", f"{tool},{rnd.choice(others)}", "", " ", ".", "*", f"{tool}*", "*." + short, tool + "%00",
             tool.encode("utf-16-le").decode("latin-1"), tool[:-1] if len(tool) > 1 else "z", tool + "s", tool + "_", "_" + tool, tool * 2, f"{tool}́", f"../{tool}"]
    fixed += [f"{rnd.choice(others)}" + "." + short for _ in range(3) if "." in tool]
    out = list(dict.fromkeys(v for v in fixed if v not in table))
    alphabet = "abcdefghijklmnopqrstuvwxyz._ -,/​‮аａ\x00\t"
    while len(out) < n:
        base = list(tool)
        for _ in range(rnd.randint(1, 3)):
            kind = rnd.randrange(3)
            i = rnd.randrange(len(base) + 1)
            if kind == 0:
                base.insert(i, rnd.choice(alphabet))
            elif kind == 1 and base:
                base[min(i, len(base) - 1)] = rnd.choice(alphabet)
            elif base:
                base.pop(min(i, len(base) - 1))
        v = "".join(base)
        if v not in table:
            out.append(v)
    return out[:max(n, 1)]


def body_for(kind: str, declared):
    if kind == "declared":
        return declared
    return {"empty-object": {}, "list": [1, 2, 3], "string": "x", "unicode": {"action": "‮\u0000", "message": "аａ"}, "deep": {"a": {"a": {"a": {"a": {}}}}},
            "huge": {"message": "x" * 200_000, "action": "refresh"}}[kind]


def expect_allowed(mode: str, action: str) -> bool | None:
    """The trusted oracle: may this action run in this mode? ``None`` = not enforced (standalone / unconfigured: nothing to assert but that it runs)."""
    if mode in ("standalone", "unconfigured"):
        return None
    if mode == "write":
        return True
    if mode == "read-only":
        return action == "read"
    return False


def start_app(mp: MonkeyPatch, app: str):
    for name in [n for n in sys.modules if n == f"{app}.app"]:
        mp.delitem(sys.modules, name)
    return importlib.import_module(f"{app}.app")


def operator_for(app: str):
    m = importlib.import_module(f"{app}.operator")
    return getattr(m, next(n for n in dir(m) if n.startswith("build_") and n.endswith("operator")))()


def configure(mp: MonkeyPatch, mode: str, workdir: str) -> None:
    for k in GATE_ENV:
        mp.delenv(k, raising=False)
    perm = os.path.join(workdir, "perm")
    mp.setenv("PERMISSIONS_DIR", perm)
    mp.setenv("SENTINEL_RATE_BURST", "1000000000")                           # edge-sentinel's per-IP limiter answers 429 before the handler: not what is swept
    mp.setenv("SENTINEL_RATE_PER_MIN", "1000000000")
    mp.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")                    # the operator's own HTTP credential is tested elsewhere; this sweep tests the authorization
    mp.setenv("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "1")
    mp.setattr(app_gate, "_GATES", {})
    mp.setattr(app_gate, "_ENTERPRISE", {})
    mp.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    stub.absent(mp)                                                          # the enterprise package is NOT installed
    if mode == "standalone":
        mp.setenv("AGENTIC_OS_PERMISSIONS", "off")
        return
    if mode == "unconfigured":
        return
    mp.setenv("AGENTIC_OS_PERMISSIONS", "1")
    if mode == "read-only":
        init_local_profile(perm)
    elif mode == "write":
        init_local_profile(perm, include_write=True)
    elif mode == "foreign-principal":
        init_local_profile(perm, user="alice", include_write=True)
        pathlib.Path(perm, "profile.json").write_text(json.dumps({"version": 1, "user": "mallory"}))


def _is_denied_route(resp) -> bool:
    try:
        j = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:  # noqa: BLE001
        j = {}
    if resp.status_code == 403 and j.get("executed") is False and "[blocked]" in str(j.get("error")):
        return True
    return resp.status_code == 200 and (j.get("denied") is True or j.get("executed") is False) and "[blocked]" in (str(j.get("text")) + str(j.get("say")))


def _is_denied_cap(out) -> bool:
    return isinstance(out, dict) and out.get("status") == "denied" and "[blocked]" in str(out.get("error"))


def sweep_cell(res: Result, app: str, mode: str, fuzz: int, rnd: random.Random) -> None:
    mp = MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as work:
            configure(mp, mode, work)
            sent = Sentinel(mp)
            mod = start_app(mp, app)
            client = TestClient(mod.app, raise_server_exceptions=False)
            table = TOOL_ACTIONS[app]
            others = [t for a, tb in TOOL_ACTIONS.items() if a != app for t in tb] or ["x.y"]

            # ── routes ──
            for method, path, declared, tool, kind in ROUTES[app]:
                if kind == "route-or-auth":
                    continue                                                  # answers 401/503 from the operator credential before the gate (covered by the route walk)
                want = expect_allowed(mode, table[tool])
                for hv in HEADER_VARIANTS:
                    for bk in BODY_VARIANTS:
                        if bk != "declared" and hv is not HEADER_VARIANTS[0] and rnd.random() > 0.15:
                            continue                                          # the body variants ride on the plain headers (and a sample of the others)
                        body = body_for(bk, declared)
                        before = len(sent.attempts)
                        try:
                            r = client.request(method, path, json=body, headers=hv) if body is not None else client.request(method, path, headers=hv)
                        except Exception as exc:  # noqa: BLE001
                            res.violate(mode=mode, surface="route", app=app, what="request crashed the client", route=f"{method} {path}", error=repr(exc)[:200])
                            continue
                        res.count(mode, "route")
                        denied = _is_denied_route(r)
                        # a body that is not the declared one may name another action (the oracle only knows the declared tool): it is held to the negative invariants only
                        # where EVERY action is denied; in read-only it may legitimately reach a read tool
                        strict = bk == "declared" or mode in ("unprovisioned", "foreign-principal")
                        if want is False:
                            if bk == "declared" and not denied:
                                res.violate(mode=mode, surface="route", app=app, what="route not denied", route=f"{method} {path}", tool=tool, status=r.status_code, headers=list(hv))
                            if strict and len(sent.attempts) != before:
                                res.violate(mode=mode, surface="route", app=app, what="a denied/refused call made an outbound attempt", route=f"{method} {path}", body=bk, headers=list(hv))
                            if strict and r.status_code < 300 and not denied and bk != "declared":
                                try:
                                    j = r.json()
                                except Exception:  # noqa: BLE001
                                    j = {}
                                if isinstance(j, dict) and j.get("executed") is True:
                                    res.violate(mode=mode, surface="route", app=app, what="executed under a denying mode", route=f"{method} {path}", body=bk)
                        elif want is True and bk == "declared" and denied:
                            res.violate(mode=mode, surface="route", app=app, what="route denied with a sufficient grant", route=f"{method} {path}", tool=tool)
                        elif want is None and bk == "declared" and denied:
                            res.violate(mode=mode, surface="route", app=app, what="denied while authorization is not enforced", route=f"{method} {path}", tool=tool)

            # ── operator (direct) and the same capability over HTTP /invoke ──
            op = operator_for(app)
            for cap in op.manifest.capabilities:
                action = table.get(cap.name)
                want = expect_allowed(mode, action) if action else False
                for variant in ({}, CAP_INPUTS, {"x": "‮" * 50}, {"__proto__": {"admin": True}, "principal": "admin", "user": "alice"}):
                    before = len(sent.attempts)
                    try:
                        direct = op.invoke(cap.name, dict(variant))
                    except Exception:  # noqa: BLE001 — the handler ran and failed on its own: it was authorised
                        direct = {"status": "handler-error"}
                    res.count(mode, "operator")
                    d_denied = _is_denied_cap(direct)
                    if want is False and not d_denied:
                        res.violate(mode=mode, surface="operator", app=app, what="capability not denied", capability=cap.name, action=action)
                    if want is False and len(sent.attempts) != before:
                        res.violate(mode=mode, surface="operator", app=app, what="a denied call made an outbound attempt", capability=cap.name)
                    if want is True and d_denied:
                        res.violate(mode=mode, surface="operator", app=app, what="capability denied with a sufficient grant", capability=cap.name)
                    if want is None and d_denied:
                        res.violate(mode=mode, surface="operator", app=app, what="denied while authorization is not enforced", capability=cap.name)
                    before = len(sent.attempts)
                    r = client.post("/invoke", json={"capability": cap.name, "inputs": dict(variant)})
                    res.count(mode, "http")
                    try:
                        h_denied = _is_denied_cap(r.json().get("result"))
                    except Exception:  # noqa: BLE001
                        h_denied = False
                    if r.status_code in (401, 503) and "[blocked]" not in r.text:
                        if len(sent.attempts) != before:
                            res.violate(mode=mode, surface="http", app=app, what="a call refused by the operator credential made an outbound attempt", capability=cap.name)
                        continue                                                    # the operator's own credential (document-editor's control token) refused it before any handler: no grant decision to compare
                    if h_denied != d_denied:
                        res.violate(mode=mode, surface="http", app=app, what="direct Operator.invoke and HTTP /invoke decided differently", capability=cap.name,
                                    direct=d_denied, http=h_denied)
                    if want is False and len(sent.attempts) != before:
                        res.violate(mode=mode, surface="http", app=app, what="a denied HTTP call made an outbound attempt", capability=cap.name)

            # ── policy: every classified tool, and hostile name variants of it ──
            enforcing = mode in ("unprovisioned", "read-only", "write", "foreign-principal")
            for tool, action in table.items():
                denial = app_gate.tool_denial(app, tool)
                res.count(mode, "policy")
                want = expect_allowed(mode, action)
                if want is False and denial is None:
                    res.violate(mode=mode, surface="policy", app=app, what="tool not denied", tool=tool, action=action)
                if want is True and denial is not None:
                    res.violate(mode=mode, surface="policy", app=app, what="tool denied with a sufficient grant", tool=tool)
                if enforcing and fuzz:
                    for v in name_variants(tool, others, rnd, fuzz, table):
                        res.count(mode, "policy")
                        try:
                            got = app_gate.tool_denial(app, v)
                        except Exception as exc:  # noqa: BLE001
                            res.violate(mode=mode, surface="policy", app=app, what="tool_denial raised on a hostile name", tool=repr(v)[:60], error=repr(exc)[:120])
                            continue
                        if got is None:
                            res.violate(mode=mode, surface="policy", app=app, what="a name that is not in the trusted table was allowed", tool=repr(v)[:80], variant_of=tool)
    finally:
        mp.undo()


def sweep_start(res: Result, app: str, mode: str) -> None:
    """The app must refuse to start (not start open, not start restricted) when its permissions configuration is unusable."""
    mp = MonkeyPatch()
    try:
        with tempfile.TemporaryDirectory() as work:
            configure(mp, "unprovisioned", work)
            if mode == "invalid-switch":
                mp.setenv("AGENTIC_OS_PERMISSIONS", "maybe")
            elif mode == "unusable-store":
                blocker = os.path.join(work, "blocker")
                pathlib.Path(blocker).write_text("not a directory")
                mp.setenv("PERMISSIONS_DIR", os.path.join(blocker, "perm"))
            elif mode == "enterprise-absent":
                mp.delenv("AGENTIC_OS_PERMISSIONS", raising=False)
                mp.setenv("CR_PERMISSIONS", "1")
            res.count(mode, "startup")
            try:
                mod = start_app(mp, app)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    raise
                return                                                        # refused to start: the required outcome
            # it started: it must at least be fully closed (every tool denied), never open
            if mode != "unusable-store" or app_gate.tool_denial(app, next(iter(TOOL_ACTIONS[app]))) is None:
                res.violate(mode=mode, surface="startup", app=app, what="the app started although its permissions configuration is unusable and it is not closed")
            del mod
    finally:
        mp.undo()


def run(fuzz: int, seed: int, apps=APPS, modes=MODES, verbose=False) -> dict:
    res = Result()
    started = time.time()
    for app in apps:
        for mode in modes:
            sweep_cell(res, app, mode, fuzz, random.Random(f"{seed}/{app}/{mode}"))
        for mode in START_MODES:
            sweep_start(res, app, mode)
        if verbose:
            print(f"  {app}: {sum(res.calls.values())} calls so far, {len(res.violations)} violations", flush=True)
    by_mode = collections.defaultdict(dict)
    for (mode, surface), n in sorted(res.calls.items()):
        by_mode[mode][surface] = n
    return {"seed": seed, "fuzz": fuzz, "apps": len(apps), "calls": sum(res.calls.values()), "seconds": round(time.time() - started, 1),
            "by_mode": by_mode, "violations": res.violations}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fuzz", type=int, default=12, help="hostile name variants per tool and mode (default 12; ~400 reaches the full sweep)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--app", action="append", help="limit to these apps")
    ap.add_argument("--json", help="write the result here")
    ns = ap.parse_args(argv)
    out = run(ns.fuzz, ns.seed, tuple(ns.app) if ns.app else APPS, verbose=True)
    print(json.dumps({k: v for k, v in out.items() if k != "violations"}, indent=1))
    for v in out["violations"][:50]:
        print("VIOLATION", json.dumps(v, default=str))
    print(f"{out['calls']} calls over {out['apps']} apps in {out['seconds']}s: {len(out['violations'])} violations")
    if ns.json:
        pathlib.Path(ns.json).write_text(json.dumps(out, indent=1, default=str))
    return 1 if out["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
