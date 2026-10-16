#!/usr/bin/env python3
"""Generate the shared Python<->Go parity fixtures under go/mission/testdata/.

The Python implementation is the source of truth: every fixture is computed by calling the real Python code. Go
tests (go/mission/*_parity_test.go) replay the same inputs and must produce the same outputs, and
tests/test_go_parity_fixtures.py regenerates everything here and compares it to the checked-in files, so a change
to Python behaviour fails CI until the fixtures (and the Go port) are updated.

    python scripts/gen_parity_fixtures.py --write     # (re)write go/mission/testdata/*.json
    python scripts/gen_parity_fixtures.py --check     # exit 1 if a checked-in fixture is stale
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUT = ROOT / "go" / "mission" / "testdata"
ENV = "AGENTIC_OS_DEFAULT_FAILURE_POLICY"



@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


TEMPLATES = [None, "onboarding", "deploy_app", "sky_deploy", "invoice_recovery", "cost_audit", "no_such_template"]
ENVS = [None, "", "COMPENSATE_DECLARED", "halt_and_report", " preserve_only ", "CONTINUE_SIBLINGS", "PRESERVE_ONLY",
        "HALT_AND_REPORT", "bogus"]


def policy_resolution() -> dict:
    from agentic_os.mission.compensation_policy import (effective_policy, is_legacy_frozen,
                                                        resolve_default_detailed)
    from agentic_os.mission.runtime import MissionRuntime
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    from agentic_os.mission.registry import CapabilityRegistry

    resolve = []
    for t in TEMPLATES:
        for e in ENVS:
            with _env(**{ENV: e}):
                pol, src, warn = resolve_default_detailed(t)
            resolve.append({"template": t, "env": e, "policy": pol.value, "source": src, "warning": warn})

    effective = []
    for t in TEMPLATES:
        for own in ("", "COMPENSATE_DECLARED", "preserve_only", "weird"):
            for frozen in ("", "COMPENSATE_DECLARED", "PRESERVE_ONLY", "HALT_AND_REPORT"):
                for forked in (False, True):
                    for e in (None, "COMPENSATE_DECLARED"):
                        m = SimpleNamespace(template=t, failure_policy=own, default_failure_policy=frozen)
                        with _env(**{ENV: e}):
                            pol = effective_policy(m, forked=forked)
                        effective.append({"template": t, "failure_policy": own, "default_failure_policy": frozen,
                                          "forked": forked, "env": e, "policy": pol.value})

    legacy = []
    for t in TEMPLATES:
        for own in ("", "PRESERVE_ONLY"):
            for src in ("", "template"):
                for frozen in ("", "COMPENSATE_DECLARED", "PRESERVE_ONLY"):
                    m = SimpleNamespace(template=t, failure_policy=own, default_policy_source=src,
                                        default_failure_policy=frozen)
                    legacy.append({"template": t, "failure_policy": own, "default_policy_source": src,
                                   "default_failure_policy": frozen, "legacy": bool(is_legacy_frozen(m))})

    created = []
    for t in TEMPLATES:
        for own in ("", "COMPENSATE_DECLARED"):
            for e in (None, "COMPENSATE_DECLARED", "bogus"):
                with _env(**{ENV: e}):
                    rt = MissionRuntime(CapabilityRegistry(), Executor(InMemoryOperatorClient({})))
                    m = rt.create_mission("g", policy_refs=["*"], template=t, failure_policy=own)
                    p = next(ev.payload for ev in rt.store.for_mission(m.id) if ev.type == "MissionCreated")
                created.append({"template": t, "failure_policy": own, "env": e,
                                "recorded_failure_policy": p.get("failure_policy", ""),
                                "default_failure_policy": p["default_failure_policy"],
                                "default_policy_source": p["default_policy_source"],
                                "default_policy_warning": p.get("default_policy_warning", ""),
                                "effective": effective_policy(m).value})
    return {"resolve": resolve, "effective": effective, "legacy": legacy, "created": created}


# ── compensation scenarios: a declarative mission + handler script, run through the REAL Python runtime ─────────
# Each scenario is replayed by go/mission/compensation_parity_test.go through the Go runtime; after the initial run
# and after every step the (normalised) state, outcome, compensation events, handler calls and attention items must be
# identical. Node ids / mission id / idempotency keys are replaced by capability-derived names.

RO = {"verified": True, "observed": {"gone": True}}


def _s(name, provides=None, **kw):
    return {"name": name, "provides": provides, **kw}


def _cleanup(**over):
    c = {"supported": True, "capability": "tmp.release", "disposition": "CLEANUP", "preconditions": "tmp.safe",
         "authorization_policy": "EVIDENCE_BOUND", "verify": "tmp.verify"}
    c.update(over)
    return c


def _temp_fleet(comp=None, **tweaks):
    return [
        _s("tmp.acquire", "staged", side_effecting=True, compensation=comp or _cleanup()),
        _s("tmp.release", side_effecting=True, irreversible=True, irreversible_note="release", mission_owned_temporary=True),
        _s("tmp.verify"), _s("tmp.safe"),
        _s("app.deploy", "deployed", side_effecting=True, undo="app.rollback"),
        _s("app.rollback", side_effecting=True, irreversible=True, irreversible_note="rollback"),
        _s("report.submit", "reported", side_effecting=True, irreversible=True, irreversible_note="report"),
    ]


_TEMP_H = {"tmp.acquire": {"result": {"lock": "L1", "executed": True}}, "tmp.release": {"result": {"released": True, "executed": True}},
           "tmp.verify": {"result": RO}, "tmp.safe": {"result": {"holds": True}},
           "app.deploy": {"result": {"deployed": "v2", "executed": True}},
           "app.rollback": {"result": {"rolled_back": True, "executed": True}},
           "report.submit": {"error": "downstream failed"}}


def _h(**over):
    return {**_TEMP_H, **over}


def _full(undo, verify=None, **kw):
    comp = {"supported": True, "capability": undo, **({"verify": verify} if verify else {}), **kw}
    return comp


def _scenarios() -> list:
    out = []

    def add(name, specs, handlers, outcomes, mission=None, env=None, steps=None):
        out.append({"name": name, "specs": specs, "handlers": handlers, "outcomes": outcomes,
                    "mission": mission or {}, "env": env or {}, "steps": steps or []})

    temp_out = ["staged", "deployed", "reported"]
    add("default_halts_and_reports", _temp_fleet(), _h(), temp_out)
    add("preserve_only_cleans_temporary_keeps_deploy", _temp_fleet(), _h(), temp_out, {"failure_policy": "PRESERVE_ONLY"})
    add("template_default_is_preserve_only", _temp_fleet(), _h(), temp_out, {"template": "deploy_app"})
    add("precondition_not_met_retains", _temp_fleet(), _h(**{"tmp.safe": {"result": {"holds": False, "reason": "lock held by another job"}}}),
        temp_out, {"failure_policy": "PRESERVE_ONLY"})
    add("precondition_unevaluable_fail_closed", _temp_fleet(), _h(**{"tmp.safe": {"error": "boom"}}), temp_out,
        {"failure_policy": "PRESERVE_ONLY"})
    add("cleanup_not_assumed_when_undeclared", _temp_fleet({k: v for k, v in _cleanup().items() if k != "disposition"}),
        _h(), temp_out, {"failure_policy": "PRESERVE_ONLY"})
    add("halt_cleans_nothing", _temp_fleet(), _h(), temp_out, {"failure_policy": "HALT_AND_REPORT"})
    add("compensate_declared_reverses_everything_verified", _temp_fleet(), _h(**{"app.deploy": {"result": {"deployed": "v2", "executed": True}}}),
        temp_out, {"failure_policy": "COMPENSATE_DECLARED"})
    add("env_opt_in_beats_template", _temp_fleet(), _h(), temp_out, {"template": "onboarding"},
        {"AGENTIC_OS_DEFAULT_FAILURE_POLICY": "COMPENSATE_DECLARED"})
    add("env_invalid_fails_safe", _temp_fleet(), _h(), temp_out, {"template": "onboarding"},
        {"AGENTIC_OS_DEFAULT_FAILURE_POLICY": "bogus"})

    simple = lambda comp, **h: ([_s("pay.charge", "paid", side_effecting=True, compensation=comp),
                                _s("pay.refund", side_effecting=True, irreversible=True, irreversible_note="refund"),
                                _s("pay.check"), _s("pay.safe"),
                                _s("crm.fail", "failed", side_effecting=True, irreversible=True, irreversible_note="n")],
                                {"pay.charge": {"result": {"paid": True, "executed": True}},
                                 "pay.refund": {"result": {"refunded": True, "executed": True}},
                                 "pay.check": {"result": RO}, "pay.safe": {"result": {"holds": True}},
                                 "crm.fail": {"error": "downstream failed"}, **h})
    po = ["paid", "failed"]
    opt = {"failure_policy": "COMPENSATE_DECLARED"}
    sp, hd = simple(_full("pay.refund", "pay.check"))
    add("verified_readback_means_rolled_back", sp, hd, po, opt)
    sp, hd = simple(_full("pay.refund"))
    add("no_verify_declared_is_unverified_not_rolled_back", sp, hd, po, opt,
        steps=[{"intervene": {"action": "verify_manually", "actor": "alice", "node": "pay.charge"}}])
    sp, hd = simple(_full("pay.refund", "pay.check"), **{"pay.check": {"result": {"verified": False, "observed": 1}}})
    add("readback_says_not_verified_fails", sp, hd, po, opt)
    sp, hd = simple(_full("pay.refund", "pay.check"), **{"pay.check": {"result": {"verified": True}}})
    add("readback_without_observed_state_fails", sp, hd, po, opt)
    sp, hd = simple(_full("pay.refund", "pay.check"), **{"pay.refund": {"seq": [{"error": "refund gateway down"}, {"result": {"refunded": True, "executed": True}}]}})
    add("failed_undo_then_operator_retry", sp, hd, po, opt,
        steps=[{"intervene": {"action": "retry", "actor": "alice", "node": "pay.charge"}}])
    sp, hd = simple(_full("pay.refund", "pay.check"), **{"pay.refund": {"error": "always down"}})
    add("attempts_exhausted_then_acknowledge", sp, hd, po, opt,
        steps=[{"intervene": {"action": "retry", "actor": "a", "node": "pay.charge"}},
               {"intervene": {"action": "retry", "actor": "a", "node": "pay.charge"}},
               {"intervene": {"action": "retry", "actor": "a", "node": "pay.charge"}},
               {"intervene": {"action": "acknowledge", "actor": "alice", "node": "pay.charge", "reason": "handled by hand"}}])
    sp, hd = simple(_full("pay.refund", "pay.check", requires_approval=True))
    add("own_approval_parks_then_approve_runs", sp, hd, po, opt,
        steps=[{"intervene": {"action": "approve", "actor": "alice", "node": "pay.charge"}}])
    add("own_approval_parks_then_acknowledge", sp, hd, po, opt,
        steps=[{"intervene": {"action": "acknowledge", "actor": "alice", "node": "pay.charge"}}])
    sp, hd = simple(_full("pay.refund", "pay.check", preconditions="pay.safe", authorization_policy="HUMAN_APPROVAL"),
                    **{"pay.safe": {"result": {"holds": False, "reason": "refund window closed"}}})
    add("precondition_blocks_before_approval_is_asked", sp, hd, po, opt,
        steps=[{"set": {"pay.safe": {"result": {"holds": True}}}},
               {"intervene": {"action": "retry", "actor": "alice", "node": "pay.charge"}},
               {"intervene": {"action": "approve", "actor": "alice", "node": "pay.charge"}}])
    sp, hd = simple({"supported": False, "disposition": "PRESERVE"})
    add("preserve_disposition_never_reversed", sp, hd, po, opt)
    sp, hd = simple(None)
    sp[0]["undo"] = "pay.refund"
    add("legacy_undo_unverified", sp, hd, po, opt)
    sp = [_s("a.make", "made", side_effecting=True), _s("b.make", "built", side_effecting=True, irreversible=True, irreversible_note="n"),
          _s("c.fail", "failed", side_effecting=True, irreversible=True, irreversible_note="n")]
    add("unknown_and_irreversible_listed_not_reversible", sp, {"a.make": {"result": {"executed": True, "a": 1}},
        "b.make": {"result": {"executed": True, "b": 1}}, "c.fail": {"error": "x"}}, ["made", "built", "failed"], opt)
    cost = lambda res: ([_s("sky.launch", "launched", side_effecting=True, compensation={"supported": False, "incurs_ongoing_cost": True}),
                         _s("app.fail", "failed", side_effecting=True, irreversible=True, irreversible_note="n")],
                        {"sky.launch": {"result": {"executed": True, **res}}, "app.fail": {"error": "x"}})
    sp, hd = cost({"cluster": "c1", "est_monthly_usd": 73.0})
    add("retained_cost_known_from_forward_result", sp, hd, ["launched", "failed"], {"template": "sky_deploy"})
    sp, hd = cost({"cluster": "c2"})
    add("retained_cost_unknown_is_never_invented", sp, hd, ["launched", "failed"], {"template": "sky_deploy"})
    sp, hd = cost({"cluster": "c3", "hourly_usd": 0.5, "failed_over": True})
    add("retained_cost_failed_over_is_unknown", sp, hd, ["launched", "failed"], {"template": "sky_deploy"})
    sp = [_s("x.make", "made", side_effecting=True, compensation=_full("x.undo", "x.check")), _s("x.undo", side_effecting=True, irreversible=True, irreversible_note="n"),
          _s("x.check"), _s("y.fail", "failed", side_effecting=True, irreversible=True, irreversible_note="n")]
    add("forward_result_not_proving_execution_is_not_compensated", sp,
        {"x.make": {"result": {"staged": True, "made": 1}}, "x.undo": {"result": {"executed": True}}, "x.check": {"result": RO},
         "y.fail": {"error": "x"}}, ["made", "failed"], opt)
    return out


def _regs() -> list:
    """Registration / coerce cases: (compensation dicts and capability sets) -> exact error text or ok."""
    base = {"supported": True, "capability": "x.undo"}
    cases = []

    def case(name, specs):
        cases.append({"name": name, "specs": specs})

    case("self_compensation", [_s("x.do", "d", side_effecting=True, compensation={"supported": True, "capability": "x.do"})])
    case("legacy_self_undo", [_s("x.do", "d", side_effecting=True, undo="x.do")])
    case("verify_equals_undo", [_s("x.do", "d", side_effecting=True, compensation={**base, "verify": "x.undo"})])
    case("supported_without_capability", [_s("x.do", "d", side_effecting=True, compensation={"supported": True})])
    case("unsupported_but_reversible", [_s("x.do", "d", side_effecting=True, compensation={"supported": False, "reversibility": "REVERSIBLE_EXPLICIT"})])
    case("unsupported_names_capability", [_s("x.do", "d", side_effecting=True, compensation={"supported": False, "capability": "x.undo"})])
    case("supported_irreversible", [_s("x.do", "d", side_effecting=True, compensation={**base, "reversibility": "IRREVERSIBLE"})])
    case("unknown_field", [_s("x.do", "d", side_effecting=True, compensation={**base, "mission_owned_temporary": True})])
    case("preserve_with_supported", [_s("x.do", "d", side_effecting=True, compensation={**base, "disposition": "PRESERVE"})])
    case("preserve_with_legacy_undo", [_s("x.do", "d", side_effecting=True, undo="x.undo", compensation={"supported": False, "disposition": "PRESERVE"})])
    case("cleanup_needs_preconditions", [_s("x.do", "d", side_effecting=True, compensation={k: v for k, v in _cleanup(capability="x.undo").items() if k != "preconditions"})])
    case("cleanup_needs_authorization", [_s("x.do", "d", side_effecting=True, compensation={k: v for k, v in _cleanup(capability="x.undo").items() if k != "authorization_policy"})])
    case("cleanup_with_cost", [_s("x.do", "d", side_effecting=True, compensation=_cleanup(capability="x.undo", incurs_ongoing_cost=True))])
    case("unknown_disposition", [_s("x.do", "d", side_effecting=True, compensation={**base, "disposition": "NUKE"})])
    case("unknown_authorization_policy", [_s("x.do", "d", side_effecting=True, compensation={**base, "authorization_policy": "TRUST_ME"})])
    case("evidence_bound_with_requires_approval", [_s("x.do", "d", side_effecting=True, compensation={**base, "authorization_policy": "EVIDENCE_BOUND", "requires_approval": True})])
    case("preconditions_without_supported", [_s("x.do", "d", side_effecting=True, compensation={"supported": False, "preconditions": "x.safe"})])
    case("cleanup_undo_not_marked_temporary", [_s("x.do", "d", side_effecting=True, compensation=_cleanup(capability="x.undo")),
                                                _s("x.undo", side_effecting=True, irreversible=True, irreversible_note="n")])
    case("cleanup_undo_marked_temporary_ok", [_s("x.do", "d", side_effecting=True, compensation=_cleanup(capability="x.undo")),
                                              _s("x.undo", side_effecting=True, irreversible=True, irreversible_note="n", mission_owned_temporary=True)])
    case("temporary_marker_on_approval_gated_undo", [_s("x.undo", side_effecting=True, approval_required=True, mission_owned_temporary=True)])
    case("evidence_bound_over_approval_gated_undo", [_s("x.do", "d", side_effecting=True, compensation={**base, "authorization_policy": "EVIDENCE_BOUND"}),
                                                     _s("x.undo", side_effecting=True, approval_required=True, irreversible=True, irreversible_note="n")])
    case("valid_full_declaration", [_s("x.do", "d", side_effecting=True, compensation={**base, "verify": "x.check", "authorization_policy": "HUMAN_APPROVAL"}),
                                    _s("x.undo", side_effecting=True, irreversible=True, irreversible_note="n"), _s("x.check")])
    return cases


def _norm(obj, table, mission_id):
    """Replace volatile identifiers by capability-derived names; drop wall-clock fields."""
    import re
    if isinstance(obj, dict):
        return {_norm(k, table, mission_id): _norm(v, table, mission_id) for k, v in obj.items()
                if k not in ("at", "exp", "ts", "nonce")}
    if isinstance(obj, list):
        return [_norm(v, table, mission_id) for v in obj]
    if isinstance(obj, tuple):
        return [_norm(v, table, mission_id) for v in obj]
    if isinstance(obj, str):
        for old in sorted(table, key=len, reverse=True):
            obj = obj.replace(old, table[old])
        return obj.replace(mission_id, "MID")
    return obj


_EVENTS = ("CompensationRequested", "CompensationAuthorized", "CompensationExecuted", "CompensationVerified",
           "CompensationFailed", "CompensationApprovalRequired", "CompensationApproved", "CompensationAcknowledged",
           "CompensationUndeclaredTarget", "CompensationSkipped", "CompensationRetryRequested", "EffectsRetained",
           "NodeCompensated", "NodeCompensationFailed", "LegacyFailurePolicyNotice", "DefaultPolicyReresolved",
           "MissionBlocked", "NodeFailed")


def _handler(table: dict, name: str, calls: list):
    state = {"i": 0}

    def h(inputs):
        calls.append(name)
        spec = table[name]
        if "seq" in spec:
            spec = spec["seq"][min(state["i"], len(spec["seq"]) - 1)]
            state["i"] += 1
        if "error" in spec:
            raise RuntimeError(spec["error"])
        return json.loads(json.dumps(spec["result"]))
    return h


def _run_scenario(sc: dict) -> dict:
    from agentic_os.mission.compensation_runner import attention_items
    from agentic_os.mission.executor import Executor, InMemoryOperatorClient
    from agentic_os.mission.planner import ExecutionIntent, IntentStep
    from agentic_os.mission.registry import CapabilityRegistry
    from agentic_os.mission.runtime import MissionRuntime
    from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, MissionState

    class Planner:
        def plan(self, mission_id, goal, context):
            steps, prev = [], []
            for o in sc["outcomes"]:
                steps.append(IntentStep(outcome=o, need=f"do {o}", inputs_from=list(prev)))
                prev = [o]
            return ExecutionIntent(mission_id=mission_id, steps=steps)

    with _env(**{ENV: None, **sc["env"]}):
        specs = []
        for d in sc["specs"]:
            kw = {k: v for k, v in d.items() if k not in ("name", "provides")}
            specs.append(CapabilitySpec(d["name"], d["name"].split(".")[0], provides=[d["provides"]] if d["provides"] else [],
                                        permissions=["*"], **kw))
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("m", specs))
        calls: list = []
        htable = dict(sc["handlers"])
        client = InMemoryOperatorClient({n: _handler(htable, n, calls) for n in htable})
        rt = MissionRuntime(reg, Executor(client), planner=Planner())
        m = rt.create_mission("go", policy_refs=["*"], template=sc["mission"].get("template"),
                              **({"failure_policy": sc["mission"]["failure_policy"]} if sc["mission"].get("failure_policy") else {}))
        plan = rt._plans[m.id]
        table = {}
        for n in plan.graph.nodes:
            table[n.id] = "node:" + n.capability
            table[n.idempotency_key] = "KEY:" + n.capability
        rt.run(m.id)
        while m.state == MissionState.WAITING_HUMAN:
            rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"])
        by_cap = {n.capability: n.id for n in plan.graph.nodes}

        def snap(label):
            evs = [{"type": e.type, "payload": e.payload} for e in rt.store.for_mission(m.id) if e.type in _EVENTS]
            return _norm({"label": label, "state": m.state.value, "outcome": m.outcome, "events": evs, "calls": list(calls),
                          "attention": attention_items(rt, m.id)}, table, m.id)
        snaps = [snap("run")]
        for i, st in enumerate(sc["steps"]):
            if "set" in st:
                htable.update(st["set"])
                client._handlers.update({n: _handler(htable, n, calls) for n in st["set"]})
            if "intervene" in st:
                iv = st["intervene"]
                try:
                    rt.compensation_intervention(m.id, iv["action"], actor=iv["actor"], reason=iv.get("reason", ""),
                                                 node_id=by_cap.get(iv.get("node")))
                    err = ""
                except Exception as e:  # noqa: BLE001 - InterventionError is part of the contract
                    err = str(e)
                s = snap(f"step{i}:{iv['action']}")
                s["error"] = err
                snaps.append(s)
            else:
                snaps.append(snap(f"step{i}:set"))
    return {"name": sc["name"], "snapshots": snaps}


def compensation_scenarios() -> dict:
    return {"scenarios": [dict(sc, expected=_run_scenario(sc)["snapshots"]) for sc in _scenarios()]}


def compensation_registration() -> dict:
    from agentic_os.mission.registry import CapabilityRegistry
    from agentic_os.mission.types import CapabilityManifest, CapabilitySpec
    out = []
    for c in _regs():
        specs = [CapabilitySpec(d["name"], d["name"].split(".")[0], provides=[d["provides"]] if d["provides"] else [],
                                permissions=["*"], **{k: v for k, v in d.items() if k not in ("name", "provides")})
                 for d in c["specs"]]
        try:
            CapabilityRegistry().register(CapabilityManifest("m", specs))
            err = ""
        except (ValueError, TypeError) as e:
            err = str(e)
        out.append({**c, "error": err})
    return {"cases": out}


def result_classification() -> dict:
    """Shared vectors for the failure-shape classifier (verify.result_failed / is_staged / proves_execution)."""
    from agentic_os.mission.verify import is_staged, proves_execution, result_failed
    results = [
        {}, {"ok": True}, {"executed": True, "id": 1}, {"executed": False}, {"staged": True}, {"staged": False, "id": 1},
        {"status": "pending_approval"}, {"status": " Awaiting_Approval "}, {"status": "queued"}, {"status": "held"}, {"status": "ok"},
        {"status": "failed"}, {"status": "error"}, {"status": "partial"}, {"ok": False}, {"success": False}, {"verified": False},
        {"error": "x"}, {"error": ""}, {"errors": ["a"]}, {"errors": []}, {"failed_count": 2}, {"failed_count": "3"}, {"failed_count": 0},
        {"failures": [1]}, {"detail": "Forbidden."}, {"detail": "fine"}, {"status_code": 500}, {"http_status": "404"}, {"status_code": 200},
        {"upstream_status": 502}, {"count": 500}, {"results": [{"ok": True}, {"error": "x"}]}, {"items": [{"id": 1}, {"staged": True}]},
        {"result": {"executed": False}}, {"result": {"id": 1}}, {"results": [{"results": [{"results": [{"results": [{"error": "deep"}]}]}]}]},
        {"id": 1, "paid": True}, {"status": "success"}, {"status": "done", "id": 7},
    ]
    out = []
    for r in results:
        failed, why = result_failed(r)
        out.append({"result": r, "failed": failed, "reason": why, "staged": is_staged(r), "proves_execution": proves_execution(r),
                    "failed_side_effect_no_outputs": result_failed(r, side_effecting=True, declares_outputs=False)[0]})
    return {"cases": out}


def compensation_fold() -> dict:
    """Event logs (hand-built, including duplicated / out-of-order records) -> fold_compensation + summarize."""
    from agentic_os.mission import compensation as C

    def ev(t, nid, i=None, **p):
        d = {"type": t, "payload": {"node_id": nid, **p}}
        if i is not None:
            d["id"] = i
        return d

    req = lambda n, att, i=None: ev("CompensationRequested", n, i, undo="u", attempt=att)
    logs = {
        "happy_path_readback": [req("n1", 1), ev("CompensationAuthorized", "n1"), ev("CompensationExecuted", "n1", receipt={"r": 1}),
                                ev("CompensationVerified", "n1", method="readback")],
        "manual_verification_is_not_readback": [req("n1", 1), ev("CompensationExecuted", "n1"), ev("CompensationVerified", "n1", method="manual", actor="a")],
        "duplicated_requested_same_attempt": [req("n1", 1), req("n1", 1), req("n1", 1), ev("CompensationFailed", "n1", reason="x")],
        "duplicated_requested_does_not_exhaust_attempts": [req("n1", 1), req("n1", 1), ev("CompensationFailed", "n1", reason="x"),
                                                           req("n1", 2), req("n1", 2), req("n1", 2), ev("CompensationFailed", "n1", reason="y")],
        "legacy_requested_without_attempt_dedupes_by_event_id": [req("n1", 0, "e1"), req("n1", 0, "e1"), req("n1", 0, "e2")],
        "legacy_requested_without_attempt_or_id_counts_each": [req("n1", 0), req("n1", 0)],
        "retry_restarts_the_node": [req("n1", 1), ev("CompensationFailed", "n1", reason="x"), req("n1", 2), ev("CompensationExecuted", "n1")],
        "approval_for_current_attempt_only": [req("n1", 1), ev("CompensationApprovalRequired", "n1", inputs_sha256="h"),
                                              ev("CompensationApproved", "n1", attempt=7, approver="z"),
                                              ev("CompensationApproved", "n1", attempt=1, approver="alice")],
        "approval_cleared_by_new_attempt": [req("n1", 1), ev("CompensationApproved", "n1", attempt=1, approver="alice"), req("n1", 2)],
        "acknowledged_after_failure": [req("n1", 1), ev("CompensationFailed", "n1", reason="x"),
                                       ev("CompensationAcknowledged", "n1", actor="alice", reason="by hand")],
        "skipped_precondition": [req("n1", 1), ev("CompensationSkipped", "n1", reason="precondition_not_met: held")],
        "node_without_id_and_unknown_types_ignored": [{"type": "CompensationRequested", "payload": {"undo": "u"}},
                                                      ev("SomethingElse", "n1"), req("n2", 1)],
        "several_nodes_mixed": [req("a", 1), ev("CompensationVerified", "a", method="readback"), req("b", 1), ev("CompensationFailed", "b", reason="r"),
                                req("c", 1), ev("CompensationApprovalRequired", "c", inputs_sha256="h"), req("d", 1), ev("CompensationExecuted", "d")],
    }
    cases = []
    for name, events in logs.items():
        folded = C.fold_compensation(events)
        cases.append({"name": name, "events": events, "folded": folded, "summary": C.summarize(folded)})
    return {"cases": cases}


def approval_tokens() -> dict:
    """Canonical JSON / inputs hash / strip / signed approval tokens (mint + verify, incl. replay), computed by the
    Python implementation. The nonce is made deterministic so the fixture is reproducible."""
    import itertools
    from agentic_os.mission import approval as A

    counter = itertools.count(1)
    real = A._secrets.token_hex
    A._secrets.token_hex = lambda n=16: f"{next(counter):0{2 * n}x}"
    try:
        secret = "parity-test-secret"
        inputs_cases = [
            {}, {"a": 1}, {"b": 2, "a": [1, 2.5, "x", None, True], "c": {"z": 1, "y": {"k": 1e20}}},
            {"u": "café ☃", "q": 'quote" back\\ slash', "n": 10.0, "i": 10, "neg": -0.0, "small": 1e-07},
            {"_approval": {"approved": True}, "__Approval_x": 1, "approvalFoo": 2, "keep": 1, "_mission": "m", "_goal": "g", "_node": "n"},
            {"nested": {"_approval": 1, "keep": {"APPROVAL": 1, "k": 2}}, "list": [{"_approval": 1, "v": 1}]},
        ]
        hashes = [{"inputs": i, "inputs_sha256": A.inputs_hash(i), "canonical": A.canonical(i).decode(),
                   "stripped": A.strip_approval_keys(i)} for i in inputs_cases]

        base = {"operator": "billing", "capability": "billing.refund", "mission_id": "m-1", "node_id": "n-1",
                "inputs": {"invoice": "INV-1", "amount": 10}, "marker": {"approved": True, "compensation": True}}
        mints = []
        for name, over, now, ttl in [("full", {}, 1_800_000_000, 300), ("float_now", {}, 1_800_000_000.5, 300),
                                     ("no_node", {"node_id": None}, 1_800_000_000, 60),
                                     ("blank_mission", {"mission_id": "  "}, 1_800_000_000, 60),
                                     ("unicode_inputs", {"inputs": {"who": "café"}}, 1_800_000_000, 300)]:
            a = {**base, **over}
            tok = A.mint_approval_token(secret, now=now, ttl_s=ttl, **a)
            mints.append({"name": name, "args": a, "now": now, "ttl_s": ttl, "wire": tok})
        tok = A.mint_approval_token(secret, now=1_800_000_000, ttl_s=300, **base)
        unbound = A.mint_approval_token(secret, now=1_800_000_000, ttl_s=300, **{**base, "mission_id": None, "node_id": None})

        def ver(**o):
            return {"operator": base["operator"], "capability": base["capability"], "inputs": base["inputs"],
                    "mission_id": base["mission_id"], "node_id": base["node_id"], **o}
        vcases = [("ok", tok, ver(), 1_800_000_100),
                  ("ok_extra_meta_keys_ignored", tok, ver(inputs={**base["inputs"], "_mission": "x", "_approval": 1}), 1_800_000_100),
                  ("expired", tok, ver(), 1_800_000_301), ("exactly_at_expiry", tok, ver(), 1_800_000_300),
                  ("wrong_operator", tok, ver(operator="crm"), 1_800_000_100),
                  ("wrong_capability", tok, ver(capability="billing.charge"), 1_800_000_100),
                  ("wrong_mission", tok, ver(mission_id="m-2"), 1_800_000_100),
                  ("missing_mission", tok, ver(mission_id=None), 1_800_000_100),
                  ("blank_mission_presented", tok, ver(mission_id=""), 1_800_000_100),
                  ("wrong_node", tok, ver(node_id="n-2"), 1_800_000_100),
                  ("missing_node", tok, ver(node_id=None), 1_800_000_100),
                  ("tampered_amount", tok, ver(inputs={"invoice": "INV-1", "amount": 999}), 1_800_000_100),
                  ("other_invoice", tok, ver(inputs={"invoice": "INV-2", "amount": 10}), 1_800_000_100),
                  ("unbound_token_accepts_any_mission", unbound, ver(mission_id="whatever", node_id="whatever"), 1_800_000_100),
                  ("bad_signature", tok[:-2] + ("AA" if tok[-2:] != "AA" else "BB"), ver(), 1_800_000_100),
                  ("garbage", "not-a-token", ver(), 1_800_000_100), ("empty", "", ver(), 1_800_000_100),
                  ("two_dots", tok + ".x", ver(), 1_800_000_100), ("payload_only", tok.split(".")[0], ver(), 1_800_000_100)]
        verifies = []
        for name, t, kw, now in vcases:
            m = A.verify_approval_token(secret, t, now=now, **kw)
            verifies.append({"name": name, "wire": t, "args": kw, "now": now, "marker": m})
        other = A.verify_approval_token("different-secret", tok, now=1_800_000_100, **ver())
        g = A.ReplayGuard()
        rt1 = A.verify_approval_token(secret, tok, now=1_800_000_100, replay_guard=g, **ver())
        rt2 = A.verify_approval_token(secret, tok, now=1_800_000_100, replay_guard=g, **ver())
        tok2 = A.mint_approval_token(secret, now=1_800_000_000, ttl_s=300, **base)
        rt3 = A.verify_approval_token(secret, tok2, now=1_800_000_100, replay_guard=g, **ver())
        replay = {"wires": [tok, tok, tok2], "args": ver(), "now": 1_800_000_100, "markers": [rt1, rt2, rt3]}
        return {"hmac_seed": secret, "hashes": hashes, "mints": mints, "verifies": verifies,
                "wrong_secret_marker": other, "replay": replay}
    finally:
        A._secrets.token_hex = real


FIXTURES = {"approval_tokens.json": approval_tokens, "compensation_fold.json": compensation_fold, "result_classification.json": result_classification, "policy_resolution.json": policy_resolution, "compensation_scenarios.json": compensation_scenarios,
            "compensation_registration.json": compensation_registration}


def render(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def build_all() -> dict[str, str]:
    # resolve_default_detailed warns ONCE per process (module-level set): do not leave it mutated for other tests
    from agentic_os.mission import compensation_policy as cp
    saved = set(cp._WARNED)
    log = logging.getLogger("agentic_os.mission.compensation_policy")
    level = log.level
    log.setLevel(logging.CRITICAL)
    try:
        return {name: render(fn()) for name, fn in FIXTURES.items()}
    finally:
        log.setLevel(level)
        cp._WARNED.clear()
        cp._WARNED.update(saved)


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--check"
    stale = []
    for name, text in build_all().items():
        path = OUT / name
        if mode == "--write":
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        elif not path.exists() or path.read_text() != text:
            stale.append(name)
    if stale:
        print("stale parity fixtures (run scripts/gen_parity_fixtures.py --write):", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
