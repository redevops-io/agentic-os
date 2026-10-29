"""Run the synthetic inbound-call demo: ``python -m agentic_os_voice.demo.run``.

What runs: a real Pipecat pipeline (real frames, real aggregators, a real ``LLMService`` function-call path, real Pipecat
Flows) with a scripted model and fake speech in and out; the real Agent Gateway with a durable authority guard; the voice
action bridge with a hash-chained journal; and a mock CRM. What does not: any real speech model, telephony, network,
provider or customer data. The process refuses network connections while it runs.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agentic_os.agent_gateway import AgentGateway, InboxApprovalStore
from agentic_os.mission.authority import AuthorityGuard, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore, SqliteLedger
from agentic_os.mission.store import EventStore, MemoryEventStore

from ..bridge import ApprovalTap, VoiceActionBridge
from ..capabilities import build_registry, default_envelope, default_tools
from ..contracts import SAY, ActionStatus, IdentityLevel, SessionBinding
from .mock_crm import MockCrm

TENANT = "acme-demo"
SUBJECT = "voice-agent"
CLAIM_WORDS = ("done", "sent", "updated", "recorded", "completed", "saved", "submitted", "confirmation")


class DemoAuthorizer:
    """Stands in for the identity plane: which permissions the voice agent's principal holds. Revocable mid-call."""

    def __init__(self, grants=("crm.read", "crm.write", "handoff.write")):
        self.grants = set(grants)

    def __call__(self, principal: Any, permission: str) -> bool:
        return permission in self.grants


@dataclass
class DemoEnv:
    workdir: str
    crm: MockCrm
    journal: Any
    inbox: InboxApprovalStore
    tap: ApprovalTap
    authorizer: DemoAuthorizer
    envelopes: Any
    guard: AuthorityGuard
    gateway: AgentGateway
    bridge: VoiceActionBridge
    clock: Callable[[], float] = time.time
    tenant: str = TENANT


def build_env(workdir: str, *, crm: Optional[MockCrm] = None, journal: Optional[EventStore] = None, inbox: Optional[Any] = None,
              authorizer: Optional[DemoAuthorizer] = None, clock: Callable[[], float] = time.time, tenant: str = TENANT,
              install_envelope: bool = True, envelope_ttl_s: float = 3600.0, durable: bool = True, grants: Optional[tuple] = None) -> DemoEnv:
    """Fresh gateway + bridge over files in ``workdir``. Calling it again with the same ``workdir`` and ``crm`` models a restart."""
    crm = crm or MockCrm(os.path.join(workdir, "crm.json"))
    journal = journal if journal is not None else (
        EventStore(os.path.join(workdir, "voice-journal.jsonl")) if durable else MemoryEventStore())
    inner = inbox if inbox is not None else InboxApprovalStore(clock=clock)
    tap = ApprovalTap(inner)
    authorizer = authorizer or DemoAuthorizer()
    envelopes = SqliteEnvelopeStore(os.path.join(workdir, "envelopes.sqlite")) if durable else MemoryEnvelopeStore()
    ledger = SqliteLedger(os.path.join(workdir, "usage.sqlite")) if durable else MemoryLedger()
    if install_envelope and envelopes.get(f"{tenant}:{SUBJECT}") is None:
        envelopes.put(default_envelope(tenant, subject=SUBJECT, now=clock(), ttl_s=envelope_ttl_s, grants=grants))
    guard = AuthorityGuard(envelopes, ledger, clock=clock)
    gateway = AgentGateway(registry=build_registry(crm, crm), authorize=authorizer, approvals=tap, authority=guard)
    bridge = VoiceActionBridge(gateway, default_tools(), journal, approvals=tap, clock=clock)
    return DemoEnv(workdir, crm, journal, inner, tap, authorizer, envelopes, guard, gateway, bridge, clock, tenant)


def open_call(env: DemoEnv, fixture: dict, *, identity: IdentityLevel = IdentityLevel.ANONYMOUS, ttl_s: float = 600.0):
    return env.bridge.open_session(SessionBinding(
        session_id=fixture["session_id"], tenant=env.tenant, expires_at=env.clock() + ttl_s, identity=identity,
        caller_id=fixture.get("caller_id", ""), subject=SUBJECT))


@contextlib.contextmanager
def network_blocked():
    """In-process guard: any attempt to open a network connection, or to bind or listen on a port, raises. (Not an OS-level sandbox; it proves the demo code and
    the libraries it exercised did not try, not that nothing could.)"""
    real = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo, socket.socket.bind,
            socket.socket.listen)

    def deny(*a: Any, **k: Any):
        raise RuntimeError("network access is blocked in the synthetic demo")
    socket.socket.connect = deny                       # type: ignore[method-assign]
    socket.socket.connect_ex = deny                    # type: ignore[method-assign]
    socket.create_connection = deny                    # type: ignore[assignment]
    socket.getaddrinfo = deny                          # type: ignore[assignment]
    socket.socket.bind = deny                          # type: ignore[method-assign]      # no listening port either
    socket.socket.listen = deny                        # type: ignore[method-assign]
    try:
        yield
    finally:
        (socket.socket.connect, socket.socket.connect_ex, socket.create_connection, socket.getaddrinfo, socket.socket.bind,   # type: ignore[method-assign,assignment]
         socket.socket.listen) = real


# ── the scripted 'model' for the happy-path call ───────────────────────────────────────────────────────────────
def happy_script(lead_id: str):
    from .fakes import Call, Say, SayResult, View

    def script(v: View) -> List[Any]:
        u, t = v.last_user, v.last_tool
        if not u and not t:
            return [Say("Hello, this is the automated phone assistant for Example Co. May I have your name?")]
        if not t and "form" in u:
            return [Say("Thanks Dana. Before I open your record I need to verify it is you. Please read me your verification code.")]
        if not t and "code" in u:
            return [Call("verification_done", {})]
        if t == "verification_done":
            return [Call("lookup_lead", {"lead_id": lead_id})] if v.last_result.get("verified") else [Say("I could not verify you.")]
        if t == "lookup_lead":
            if v.last_result.get("status") != "completed":
                return [SayResult("lookup_lead")]
            out = v.last_result.get("output") or {}
            return [Say(f"Thank you. I can see your request for {out.get('company', 'your company')}. What budget and timeline do you have in mind?")]
        if not t and "twenty" in u:
            return [Call("record_answers", {"budget_band": "10k_50k", "timeline": "this_quarter",
                                           "need_summary": "Help with onboarding."})]
        if t == "record_answers":
            return [Say("To confirm: a budget between ten and fifty thousand dollars, starting this quarter, for help with onboarding. "
                        "May I record that on your lead?")]
        if not t and "record it" in u:
            return [Call("update_lead", {"lead_id": lead_id, "status": "qualified", "budget_band": "10k_50k", "timeline": "this_quarter",
                                         "need_summary": "Help with onboarding.", "caller_confirmed": True})]
        if t == "update_lead":
            acts: List[Any] = [SayResult("update_lead")]
            if v.last_result.get("status") in ("completed", "pending_approval"):
                acts.append(Call("draft_followup", {"contact": lead_id, "goal": "Here is a link to book a call with our team."}))
            return acts
        if t == "draft_followup":
            return [Say("I have drafted a short follow-up message. Would you like me to ask a colleague to send it?")]
        if not t and "send that" in u:
            body = (v.results.get("draft_followup", {}).get("output") or {}).get("draft", "")
            return [Call("request_followup_send", {"contact": lead_id, "body": body, "caller_confirmed": True})]
        if t == "request_followup_send":
            return [SayResult("request_followup_send"), Say("Thank you for calling Example Co. Goodbye.")]
        return [Say("I am not able to help with that.")]
    return script


@dataclass
class CallReport:
    spoken: List[str] = field(default_factory=list)
    interrupted: List[str] = field(default_factory=list)
    nodes: List[str] = field(default_factory=list)
    actions: Dict[str, dict] = field(default_factory=dict)
    inferences: int = 0
    steps: List[str] = field(default_factory=list)
    handoff: Optional[str] = None
    cancelled_calls: List[str] = field(default_factory=list)   # tool calls Pipecat cancelled because the caller interrupted
    context: List[Any] = field(default_factory=list)       # the LLM context at the end of the call (what the 'model' was shown)


async def run_call(env: DemoEnv, fixture: dict, script: Callable, *, identity: IdentityLevel = IdentityLevel.ANONYMOUS,
                   with_flow: bool = True, extra: Optional[Callable[[Any, Any], Any]] = None,
                   host_ops: Optional[Dict[str, Callable]] = None, timeline: Optional[List[dict]] = None) -> CallReport:
    """Run one fixture call through a real Pipecat pipeline. Imports Pipecat lazily so the module imports without it."""
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
    from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
    from pipecat.workers.runner import WorkerRunner
    from pipecat.frames.frames import EndFrame

    from ..pipecat_adapter import DistressGuard, register_governed_tools, tool_schemas
    from .call_simulator import CallSimulator
    from .fakes import FakeSTT, FakeTTS, ScriptedLLM

    proxy = open_call(env, fixture, identity=identity)
    report = CallReport()
    holder: Dict[str, Any] = {}
    llm = ScriptedLLM(script, on_inference=lambda v: report.nodes.append(holder["fm"].current_node if "fm" in holder and holder["fm"] else "-"))
    tts = FakeTTS()
    if with_flow:
        context = LLMContext(messages=[])
    else:
        register_governed_tools(llm, proxy)
        context = LLMContext(messages=[{"role": "system", "content": "Fixture call."}], tools=tool_schemas(proxy.tool_specs))
    pair = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()))
    guard = DistressGuard(proxy, on_handoff=lambda cat, res: setattr(report, "handoff", cat))
    stt = FakeSTT({k: v for k, v in fixture["utterances"].items()})
    steps = [stt, guard, pair.user(), llm, tts, pair.assistant()]
    if extra:
        steps = extra(steps, holder) or steps
    worker = PipelineWorker(Pipeline(steps), params=PipelineParams(), cancel_on_idle_timeout=False)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @llm.event_handler("on_function_calls_cancelled")
    async def _cancelled(service: Any, calls: Any) -> None:
        report.cancelled_calls.extend(c.function_name for c in calls)

    fm = None
    if not with_flow:
        from pipecat.frames.frames import LLMRunFrame

        @worker.event_handler("on_pipeline_started")
        async def _started_plain(w: Any, frame: Any) -> None:
            await worker.queue_frame(LLMRunFrame())          # the greeting turn, as the flow's first node does
    if with_flow:
        from pipecat.flows import FlowManager
        from .flow import build_nodes
        nodes = build_nodes(proxy)
        fm = FlowManager(llm=llm, context_aggregator=pair, worker=worker)
        holder["fm"] = fm

        @worker.event_handler("on_pipeline_started")
        async def _started(w: Any, frame: Any) -> None:
            await fm.initialize(nodes["welcome"]())

    async def verify() -> None:
        env.bridge.mark_verified(fixture["session_id"], tuple(fixture["verified_accounts"]), method="simulated_otp")

    async def wait_handoff() -> None:
        while guard.handoff_result is None:
            await asyncio.sleep(0.005)

    sim = CallSimulator({**fixture, **({"timeline": timeline} if timeline else {})}, worker, llm, tts,
                        host={"verify": verify, "wait_handoff": wait_handoff, **(host_ops or {})})

    async def drive() -> None:
        try:
            await sim.run()
        finally:
            await worker.queue_frame(EndFrame())
    driver = asyncio.ensure_future(drive())
    try:
        await runner.run()
    finally:
        await driver if not driver.cancelled() else None
    await env.bridge.drain()
    report.spoken, report.interrupted = list(tts.spoken), list(tts.interrupted)
    report.context = json.loads(json.dumps(context.get_messages(), default=str))
    report.inferences, report.steps = llm.inferences, sim.log
    report.actions = {aid: env.bridge.receipt(aid) for aid in list(env.bridge._index) if not aid.startswith("refused:")}
    env.bridge.close_session(fixture["session_id"])
    return report


def operator_review(env: DemoEnv, approver: str = "alex.reviewer") -> List[dict]:
    """The human step after the call: a named person approves each parked action; the bridge then runs the identical request."""
    out = []
    for action_id, approval_id in env.bridge.pending_actions().items():
        env.inbox.approve(approval_id, by=approver)
        res = env.bridge.resume(action_id)
        out.append({"action_id": action_id, "approval_id": approval_id, "approved_by": approver, "status": res.status.value,
                    "receipt_ref": res.receipt_ref})
    return out


def unreceipted_claims(spoken: List[str], env: DemoEnv) -> List[str]:
    """Any sentence that claims an outcome must contain the bridge's fixed sentence for a status that really occurred (read from
    the journal), and the 'completed' sentence additionally needs a completed action that carries a downstream receipt."""
    seen = set()
    receipted = False
    for ev in env.journal.all():
        st = (ev.payload or {}).get("status")
        if ev.type.startswith("VoiceAction") and st:
            seen.add(st)
            receipted = receipted or (st == "completed" and bool((ev.payload or {}).get("receipt_ref")))
    allowed = {SAY[s] for s in ActionStatus if s.value in seen}
    bad = []
    for s in spoken:
        if any(w in s.lower() for w in CLAIM_WORDS) and not any(a in s for a in allowed):
            bad.append(s)
        elif SAY[ActionStatus.COMPLETED] in s and not receipted:
            bad.append(s)
    return bad


def render(report: CallReport, env: DemoEnv, review: List[dict], bad_claims: List[str]) -> str:
    lines = ["== spoken by the bot (fake TTS) =="] + [f"  bot: {s}" for s in report.spoken]
    if report.interrupted:
        lines += ["== cut off by the caller =="] + [f"  bot (interrupted): {s}" for s in report.interrupted]
    lines += ["== flow nodes at each model turn ==", "  " + " > ".join(report.nodes)]
    lines.append("== actions (bridge receipts; no argument values) ==")
    for aid, r in sorted(report.actions.items(), key=lambda kv: kv[1].get("seq", 0)):
        lines.append(f"  {r['tool']:<22} {r['status']:<17} reason={r['reason']:<18} receipt_ref={r.get('receipt_ref') or '-'}")
    lines.append("== operator review after the call ==")
    lines += [f"  {r['approved_by']} approved {r['approval_id']}; action now {r['status']} receipt_ref={r['receipt_ref']}" for r in review] or ["  (nothing pending)"]
    lines.append("== the mock CRM's own record of effects (ground truth) ==")
    lines += [f"  {k} -> {v}" for k, v in sorted(env.crm.effects.items())]
    lines.append(f"== journal chain verified: {env.journal.verify().ok} ({env.journal.verify().checked} events) ==")
    lines.append(f"== unreceipted claims by the bot: {len(bad_claims)} ==")
    return "\n".join(lines)


async def _amain(args: argparse.Namespace) -> int:
    from .call_simulator import load_fixture
    fixture = load_fixture("inbound_lead_call.json")
    workdir = args.workdir or tempfile.mkdtemp(prefix="voice-demo-")
    try:
        env = build_env(workdir)
        with network_blocked():
            report = await run_call(env, fixture, happy_script(fixture["lead_id"]))
            review = operator_review(env)
        bad = unreceipted_claims(report.spoken, env)
        print(json.dumps({"report": report.__dict__, "review": review, "crm_effects": env.crm.effects, "bad_claims": bad}, indent=2, default=str)
              if args.json else render(report, env, review, bad))
        return 1 if bad else 0
    finally:
        if not args.keep and not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv: Optional[List[str]] = None) -> int:
    try:                                              # keep the framework's debug logging out of the report
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Synthetic inbound-call demo (fixtures and fakes only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workdir", help="use (and keep) this directory instead of a throwaway one")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway directory")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
