"""Operator CLI for the AOS-DD mission. One process per command, all state in ``--state`` (events.jsonl + its anchor, authority.db, ops.db).

Every command that acts on a mission authenticates the operator: ``--operator-auth FILE`` holds the expected operator credentials (what the
control plane would hold, 0600), and the operator presents ONE credential through ``--operator-token-file FILE`` (0600) or the environment
variable ``AOSDD_OPERATOR_TOKEN``. The base ``resolve_caller`` decides who that is; the resulting ``AuthenticatedPrincipal`` is what the
runtime records. There is no ``--actor``: a free-text name is not an identity (see identity.py for the role -> action table).

    python -m agentic_os.integrations.aosdd.cli --state DIR --config FILE --operator-auth FILE --operator-token-file FILE submit --case K [--attempt N]
    ... approve --mission M --plan-sha256 H
    ... resume --mission M [--reason TEXT]          (explicit: a restart never resumes by itself)
    ... reconcile --mission M --step S3 [--reason TEXT]
    ... revoke --mission M --reason TEXT
    ... compensate --mission M --reason TEXT
    ... status --mission M
    python -m agentic_os.integrations.aosdd.cli --state DIR reconstruct --mission M      (reads only events.jsonl and its anchor; no config, no network)
    python -m agentic_os.integrations.aosdd.cli --state DIR verify                       (integrity of the event log + anchor, the operation ledger and the authority store)

Test hooks (never used by the library; they let a harness kill the process at an exact point):
    --pause-after CAPABILITY      after that node's NodeSucceeded, write PAUSED and wait for GO (or be killed)
    --hold-after-response APP     after the first write response from APP is received (before it is recorded), write HELD and wait for GO
Everything printed goes through the configured redactor."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time

from agentic_os.mission.authority import AuthorityGuard, SqliteEnvelopeStore, SqliteLedger

from .config import AosddConfig
from .identity import AuthenticationFailed, OperatorAuth
from .http import Request, Resp, UrllibTransport
from .oplog import OpLog
from .reconstruct import reconstruct
from .runtime import Runner, RunnerError
from .services import RedactingEventStore, Services


class _HoldTransport:
    def __init__(self, inner, app_base: str, flag: str):
        self.inner, self.base, self.flag, self.done = inner, app_base, flag, False

    def send(self, req: Request) -> Resp:
        resp = self.inner.send(req)
        if not self.done and req.method != "GET" and req.url.startswith(self.base):
            self.done = True
            open(self.flag, "w").write(f"{req.method} {req.url.split('?')[0]} -> {resp.status}")
            while not os.path.exists(os.path.dirname(self.flag) + "/GO"):
                time.sleep(0.1)
        return resp


def _out(cfg, obj) -> None:
    text = json.dumps(obj, indent=1, default=str)
    print(cfg.redactor.text(text) if cfg else text)


def operator(args):
    """The authenticated principal behind the presented credential (never a name taken from the command line)."""
    if not args.operator_auth:
        raise AuthenticationFailed("--operator-auth is required")
    tok = os.environ.get("AOSDD_OPERATOR_TOKEN", "")
    if args.operator_token_file:
        st = os.stat(args.operator_token_file)
        if st.st_mode & 0o077:
            raise AuthenticationFailed("the operator token file must be private (chmod 600)")
        tok = open(args.operator_token_file, encoding="utf-8").read().strip()
    return OperatorAuth.from_file(args.operator_auth).authenticate(tok)


def build(args) -> "tuple[Runner, AosddConfig]":
    cfg = AosddConfig.from_file(args.config)
    os.makedirs(args.state, exist_ok=True)
    guard = AuthorityGuard(SqliteEnvelopeStore(args.state + "/authority.db"), SqliteLedger(args.state + "/authority.db.usage"))
    store = RedactingEventStore(args.state + "/events.jsonl", cfg.redactor)
    transport = UrllibTransport()
    if args.hold_after_response:
        transport = _HoldTransport(transport, cfg.base_urls[args.hold_after_response], args.state + "/HELD")
    svc = Services(cfg, store, OpLog(args.state + "/ops.db", lease_ttl_s=cfg.lease_ttl_s), guard, transport=transport)
    if args.pause_after:
        def hook(ev):
            if ev.type == "NodeSucceeded" and ev.payload.get("capability") == args.pause_after:
                open(args.state + "/PAUSED", "w").write(ev.payload["capability"])
                while not os.path.exists(args.state + "/GO"):          # the harness either kills this process or releases it
                    time.sleep(0.1)
        store.subscribe(hook)
    return Runner(svc, grant_ttl_s=args.grant_ttl), cfg


def summary(r: Runner, mid: str) -> dict:
    m = r.rt._missions.get(mid)
    out = {k: v for k, v in (m.outcome or {}).items() if k != "world"} if m else None
    return {"mission_id": mid, "state": (m.state.value if m else None), "outcome": out,
            "ledger_head": r.svc.store.head(mid), "chain_ok": r.svc.store.verify().ok}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aosdd")
    ap.add_argument("--state", required=True)
    ap.add_argument("--config")
    ap.add_argument("--operator-auth")
    ap.add_argument("--operator-token-file")
    ap.add_argument("--grant-ttl", type=float, default=900.0)
    ap.add_argument("--pause-after")
    ap.add_argument("--hold-after-response")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit"); s.add_argument("--case", required=True); s.add_argument("--attempt", type=int, default=1); s.add_argument("--ref", default="AOSDD-HFC-001")
    s = sub.add_parser("approve"); s.add_argument("--mission", required=True); s.add_argument("--plan-sha256", required=True)
    s.add_argument("--grant-ttl-s", type=float)
    s = sub.add_parser("resume"); s.add_argument("--mission", required=True); s.add_argument("--reason", default="")
    s = sub.add_parser("reconcile"); s.add_argument("--mission", required=True); s.add_argument("--step", required=True); s.add_argument("--reason", default="")
    s = sub.add_parser("revoke"); s.add_argument("--mission", required=True); s.add_argument("--reason", required=True)
    s = sub.add_parser("compensate"); s.add_argument("--mission", required=True); s.add_argument("--reason", required=True)
    s = sub.add_parser("status"); s.add_argument("--mission", required=True)
    s = sub.add_parser("reconstruct"); s.add_argument("--mission", required=True)
    sub.add_parser("verify")
    a = ap.parse_args(argv)
    if a.cmd == "reconstruct":                # the ledger alone: no config, no runtime, no network
        _out(None, reconstruct(a.state + "/events.jsonl", a.mission))
        return 0
    if a.cmd == "verify":
        _out(None, verify_state(a.state))
        return 0
    if not a.config:
        ap.error("--config is required")
    r, cfg = build(a)
    try:
        if a.cmd == "status":
            r.attach(a.mission)
            _out(cfg, summary(r, a.mission))
            return 0
        who = operator(a)                      # authenticated before anything else happens
        if a.cmd == "submit":
            mid = r.submit(a.case, a.ref, principal=who, attempt=a.attempt)
            if mid not in r.rt._missions:          # a duplicate submit returned an existing mission: show it, run nothing
                r.attach(mid)
            p = r.plan(mid) if r.svc.plan_for(mid) else None
            _out(cfg, {**summary(r, mid), "plan_sha256": p and p["plan_sha256"], "steps": p and [{"step": x["step"], "op_id": x["op_id"], "target": x["target"]} for x in p["steps"]]})
        elif a.cmd == "approve":
            r.approve_plan(a.mission, a.plan_sha256, principal=who, grant_ttl_s=a.grant_ttl_s)
            _out(cfg, summary(r, a.mission))
        elif a.cmd == "resume":
            r.resume(a.mission, principal=who, reason=a.reason)
            _out(cfg, summary(r, a.mission))
        elif a.cmd == "reconcile":
            r.reconcile(a.mission, a.step, principal=who, reason=a.reason)
            _out(cfg, summary(r, a.mission))
        elif a.cmd == "revoke":
            r.revoke(a.mission, a.reason, principal=who)
            _out(cfg, {"mission_id": a.mission, "revoked": True, "by": who.record()})
        elif a.cmd == "compensate":
            r.compensate(a.mission, a.reason, principal=who)
            _out(cfg, summary(r, a.mission))
    except (RunnerError, AuthenticationFailed) as e:
        _out(cfg, {"error": str(e)})
        return 2
    return 0


def verify_state(state: str) -> dict:
    """Integrity of everything the mission depends on, reported separately; nothing here talks to an application."""
    from agentic_os.mission.authority import SqliteEnvelopeStore
    from agentic_os.mission.store import EventStore
    out: dict = {}
    try:
        st = EventStore(state + "/events.jsonl", strict=False)
        rep = st.check_integrity()
        out["event_log"] = {**dataclasses.asdict(rep), "chain_ok": st.verify().ok, "anchor_key": "none configured" if not os.environ.get("AGENTIC_OS_ANCHOR_KEY") else "configured"}
        if not rep.ok:
            out["event_log"]["error"] = rep.reason
    except Exception as e:  # noqa: BLE001
        out["event_log"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["operation_ledger"] = OpLog(state + "/ops.db").verify()
    except Exception as e:  # noqa: BLE001
        out["operation_ledger"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["authority_store"] = SqliteEnvelopeStore(state + "/authority.db").verify()
    except Exception as e:  # noqa: BLE001
        out["authority_store"] = {"error": f"{type(e).__name__}: {e}"}
    out["ok"] = all("error" not in v for v in out.values())
    return out


if __name__ == "__main__":
    sys.exit(main())
