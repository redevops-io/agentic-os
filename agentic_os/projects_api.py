"""Projects API — the thin read/action HTTP surface the Projects UI (redevops-projects)
renders.

It serves **UI-facing projections** (overview · missions · workflows · attention ·
discovery · apps · activity) at the exact paths the front-end's ``HttpDataClient`` calls,
plus a ``POST /api/sidekick`` for the conversational surface. Each projection carries
provenance (``source_runtime`` + ``source_refs``) so advanced views can drill down without
the UI knowing Runtime internals — Runtime storage can evolve without a frontend rewrite.

Projections come from a pluggable :class:`ProjectionProvider`. A :class:`SampleProjectionProvider`
ships so the API (and the UI against it) runs end-to-end today; bind real Runtime stores by
implementing the same protocol. The **apps** projection prefers the live connector setup
guides (``redevops_connectors.SETUP_GUIDES`` + ``verify_setup``) when the ``[connectors]``
plugin is installed, and falls back to the sample otherwise — so "what apps can this Project
use, and are they connected?" reflects the real Integration Plane when it's present.
"""
from __future__ import annotations

import html as _html

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field
except ImportError as e:  # pragma: no cover
    raise ImportError("the Projects API needs fastapi (a base dependency)") from e


def _prov(runtime: str, *refs: str) -> Dict[str, Any]:
    return {"source_runtime": runtime, "source_refs": list(refs)}


# ── outcome input limits / tenant normalisation (shared by the demo and authoritative lanes) ────
MAX_REWARD_ABS = 1e6
MAX_DELAY_SECONDS = 10 * 365 * 24 * 3600.0       # 10 years
MAX_REWARD_DIMS = 16
MAX_DIM_KEY_LEN = 64
MAX_BODY_BYTES = 16 * 1024


def norm_tenant(t: Optional[str]) -> Optional[str]:
    """None, '' and whitespace are all 'untenanted' (None) so they can never collide with a real tenant."""
    t = (t or "").strip()
    return t or None


def validate_outcome_numbers(*, observed_reward, reward_dimensions, delay, attribution_confidence) -> None:
    """Raise OutcomeRejected(422) for non-finite / out-of-bound numbers or an oversized reward_dimensions."""
    import math

    def _ok(x, lo, hi):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and lo <= x <= hi
    if observed_reward is not None and not _ok(observed_reward, -MAX_REWARD_ABS, MAX_REWARD_ABS):
        raise OutcomeRejected(422, f"observed_reward must be finite and within +/-{MAX_REWARD_ABS:g}")
    dims = reward_dimensions or {}
    if len(dims) > MAX_REWARD_DIMS:
        raise OutcomeRejected(422, f"reward_dimensions is limited to {MAX_REWARD_DIMS} keys")
    for k, v in dims.items():
        if not isinstance(k, str) or not k or len(k) > MAX_DIM_KEY_LEN:
            raise OutcomeRejected(422, f"reward_dimensions keys must be 1..{MAX_DIM_KEY_LEN} chars")
        if not _ok(v, -MAX_REWARD_ABS, MAX_REWARD_ABS):
            raise OutcomeRejected(422, f"reward_dimensions[{k!r}] must be finite and within +/-{MAX_REWARD_ABS:g}")
    if not _ok(delay, 0.0, MAX_DELAY_SECONDS):
        raise OutcomeRejected(422, "delay must be finite, >= 0 and <= 10 years (seconds)")
    if attribution_confidence is not None and not _ok(attribution_confidence, 0.0, 1.0):
        raise OutcomeRejected(422, "attribution_confidence must be finite and within [0, 1]")



class BodyLimitMiddleware:
    """Pure-ASGI request-body cap for the outcome / promotion routes. Counts bytes as they stream in and
    aborts with 413 at ``limit`` (Content-Length is only a fast path), so a chunked body without a length
    is never buffered beyond ~limit."""

    def __init__(self, app, limit: int = 16 * 1024) -> None:
        self.app, self.limit = app, limit

    @staticmethod
    def _guarded(path: str) -> bool:
        return path == "/api/outcomes" or path.endswith("/policy/promotion-cycle")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or not self._guarded(scope.get("path", "")):
            return await self.app(scope, receive, send)
        started = False
        rejected = False

        async def _send(msg):
            nonlocal started
            if rejected:
                return                      # the 413 is already out; drop whatever the app tries next
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        async def _reject():
            nonlocal rejected
            if started or rejected:
                return
            rejected = True
            body = b'{"detail":"request body too large"}'
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        cl = dict(scope.get("headers") or []).get(b"content-length", b"")
        if cl.isdigit() and int(cl) > self.limit:
            return await _reject()
        total = 0

        async def _receive():
            nonlocal total
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if total > self.limit:
                    await _reject()         # answer 413 now, then tell the app the client is gone
                    return {"type": "http.disconnect"}
            return msg
        try:
            await self.app(scope, _receive, _send)
        except _BodyTooLarge:
            await _reject()


def demo_client_key(request) -> str:
    """The identity demo caps are keyed on. Default: the socket peer. ``OUTCOMES_TRUSTED_PROXY_HEADER``
    (``cf-connecting-ip`` | ``x-forwarded-for``, default OFF) is honoured ONLY when the peer is listed in
    ``OUTCOMES_TRUSTED_PROXY_IPS`` (comma-separated), so a direct caller cannot spoof it. For
    ``x-forwarded-for`` the LAST entry (appended by the trusted proxy) is used."""
    import os as _o
    peer = request.client.host if request.client else "anon"
    hdr = _o.environ.get("OUTCOMES_TRUSTED_PROXY_HEADER", "").strip().lower()
    trusted = {x.strip() for x in _o.environ.get("OUTCOMES_TRUSTED_PROXY_IPS", "").split(",") if x.strip()}
    if hdr in ("cf-connecting-ip", "x-forwarded-for") and peer in trusted:
        v = request.headers.get(hdr, "")
        v = v.split(",")[-1].strip() if hdr == "x-forwarded-for" else v.strip()
        if v:
            return v[:64]
    return peer


# ── enabled-app selection (a deployment offers only a subset of apps) ────────────
def enabled_selection() -> Optional[Set[str]]:
    """The set of enabled provider ids from ``$PROJECTS_APPS`` (comma-separated, e.g.
    ``google,slack,stripe``). Empty or unset → ``None``, meaning *no filter* — every app is
    offered, so the default behaviour is unchanged. Whitespace around ids is trimmed."""
    import os
    raw = os.environ.get("PROJECTS_APPS", "")
    ids = {p.strip() for p in raw.split(",") if p.strip()}
    return ids or None


def enabled_apps(apps: List[dict], selection: Optional[Set[str]]) -> List[dict]:
    """Filter an apps projection down to the enabled ``selection`` (matched on each app's
    ``provider`` id). A falsy selection (``None``/empty) means no filter and returns the apps
    unchanged. Ids in ``selection`` that match no app are ignored — the filter never invents
    apps, it only removes the ones a deployment hasn't enabled."""
    if not selection:
        return list(apps)
    return [a for a in apps if a.get("provider") in selection]


# ── hosted OAuth (click-only Connect for the served deployment) ──────────────────
_HOSTED: Any = None
_HOSTED_TRIED = False
_HOSTED_BROKEN = False      # OAuth client credentials are present but the hosted flow could not be built

# ── Sidekick's grounded LLM fallback (out-of-KB questions; None when no model configured) ──
_SIDEKICK: Any = None
_SIDEKICK_TRIED = False


def _get_sidekick_assistant() -> Any:
    """The deployment's grounded Sidekick fallback, built once from env. Its model is None (a
    no-op fallback) unless SIDEKICK_MODEL_BASE_URL points at an OpenAI-compatible endpoint."""
    global _SIDEKICK, _SIDEKICK_TRIED
    if _SIDEKICK_TRIED:
        return _SIDEKICK
    _SIDEKICK_TRIED = True
    try:
        from .sidekick_assistant import make_grounded_sidekick
        _SIDEKICK = make_grounded_sidekick()
    except Exception:
        _SIDEKICK = None
    return _SIDEKICK


def _get_hosted() -> Any:
    """The deployment's :class:`HostedConnect`, built once from env. Returns None when the
    connector plugin isn't installed or no OAuth app's client creds are set (Connect then falls
    back to the simulated path)."""
    global _HOSTED, _HOSTED_TRIED, _HOSTED_BROKEN
    if _HOSTED_TRIED:
        return _HOSTED
    _HOSTED_TRIED = True
    import os
    base = os.environ.get("PROJECTS_BASE_URL", "http://127.0.0.1:8787")
    try:
        from .integrations.hosted_oauth import HostedConnect, oauth_apps_from_env
        if oauth_apps_from_env(base):           # any provider's client creds present?
            _HOSTED = HostedConnect.from_env(base)
    except Exception:
        _HOSTED = None
        try:
            _HOSTED_BROKEN = bool(oauth_apps_from_env(base))
        except Exception:
            _HOSTED_BROKEN = False
    return _HOSTED


def _callback_page(body: str) -> str:
    return ("<!doctype html><meta charset=utf-8><title>ReDevOps Connect</title>"
            "<body style='font:16px system-ui;margin:4rem;text-align:center'>" + body + "</body>")


# ── the projection contract ─────────────────────────────────────────────────────
class ProjectionProvider(Protocol):
    def projects(self) -> List[dict]: ...
    def overview(self, project_id: str) -> dict: ...
    def missions(self, project_id: str) -> List[dict]: ...
    def mission_detail(self, project_id: str, mission_id: str) -> dict: ...
    def workflows(self, project_id: str) -> List[dict]: ...
    def attention(self, project_id: str) -> List[dict]: ...
    def priorities(self, project_id: str) -> dict: ...
    def discovery(self, project_id: str) -> List[dict]: ...
    def apps(self, project_id: str) -> List[dict]: ...
    def connect_app(self, provider: str) -> dict: ...
    def sources(self, project_id: str) -> List[dict]: ...
    def runtime(self, project_id: str) -> dict: ...
    def templates(self, project_id: str) -> List[dict]: ...
    def activity(self, project_id: str) -> List[dict]: ...


# ── live producer sources + provider selection ─────────────────────────────────────
#: (app name, producer name) -> a zero-arg callable returning this producer's LIVE decision output: a list
#: of InterventionCandidates, or a DecisionOpportunity (the loop then selects among its actions). An app
#: registers its manifest producers' live-state sources here at boot, beside
#: ``agentic_os.app_kit.boot.register_app_manifest``.
LIVE_PRODUCER_SOURCES: Dict[Tuple[str, str], Callable[[], Any]] = {}


def register_live_producer_source(app: str, producer: str, source: Callable[[], Any]) -> None:
    LIVE_PRODUCER_SOURCES[(app, producer)] = source


def unregister_live_producer_source(app: str, producer: str) -> None:
    LIVE_PRODUCER_SOURCES.pop((app, producer), None)


def registered_live_sources(registry: Any = None) -> List[Tuple[str, str, Callable[[], Any]]]:
    """Live-state sources for producers DECLARED in apps registered with the app_kit boot registry. A
    producer in a manifest with no registered source contributes nothing (it has no live state)."""
    if registry is None:
        from agentic_os.app_kit.boot import default_registry
        registry = default_registry()
    out = []
    for reg_app in registry:
        for prod in reg_app.manifest.producers:
            fn = LIVE_PRODUCER_SOURCES.get((reg_app.manifest.name, prod.name))
            if fn is not None:
                out.append((reg_app.manifest.name, prod.name, fn))
    return out


@dataclass
class SampleProjectionProvider:
    """Deterministic projections matching the UI's shapes — the API runs end-to-end with no
    Runtime wiring. Replace method-by-method with real projections; each already carries the
    provenance the UI drill-down expects."""

    project_id: str = "customer-ops"
    project_name: str = "Customer Operations"
    #: Today's sandbox cases + simulated sends exist ONLY on the pure sample provider. A provider backed by real
    #: producers (RegisteredProducersProvider) sets this False: its Today is the read-only feed of real signals.
    today_sample_cases: bool = True
    #: providers currently connected — mutated by connect_app so apps() and template
    #: readiness reflect real connection state (seeded from the sample's connected apps).
    connected: Set[str] = field(
        default_factory=lambda: {a["provider"] for a in _sample_apps() if a["state"] != "NOT_CONNECTED"} | {"polar", "apollo"})
    #: recorded OutcomeEvents feeding the learning loop. A deployment persists these; the sample keeps
    #: them in memory so the demo can show cross-app action selection ADAPT as real outcomes arrive.
    outcome_events: List[Any] = field(default_factory=list)
    #: optional durable backing for the outcome log ($OUTCOME_STORE_PATH). When set, recorded outcomes
    #: are appended to a JSONL file and reloaded on start — so learned selection survives a restart.
    persist_path: Optional[str] = None
    #: the PolicyRegistry the request path consults for the PROMOTED policy. Selection never refits a
    #: model from the raw outcome log: the only way the active policy changes is
    #: :meth:`run_promotion_cycle` (``apply=True`` AND a passed gate). Default: persistent when
    #: ``policy_store_path`` / ``$POLICY_STORE_PATH`` / ``$AOS_DATA_DIR`` configure a store, else in-memory.
    policy_registry: Any = None
    policy_store_path: Optional[str] = None
    #: InterventionStore holding the executed-intervention receipts an AUTHORITATIVE outcome must link to.
    #: None => no receipts resolvable => every authoritative write is rejected (fail closed).
    intervention_store: Any = None
    #: serialises dedupe-check + append of the in-memory working copy and the durable file
    _outcome_lock: Any = field(default_factory=__import__("threading").Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.policy_registry is None:
            from agentic_os.policy_promotion import PolicyRegistry, open_policy_store
            self.policy_registry = PolicyRegistry(store=open_policy_store(self.policy_store_path))
        # rehydrate the in-memory working copy from durable storage, so learning resumes where it left off
        if self.persist_path:
            from agentic_os.outcome_store import FileOutcomeStore
            self.outcome_events = list(FileOutcomeStore(self.persist_path).load())

    def projects(self) -> List[dict]:
        return [{"id": self.project_id, "name": self.project_name, "health": "ok"}]

    def missions(self, _pid: str) -> List[dict]:
        m = [
            ("4821", "Refund Sarah Chen", "Customer Refunds", "needs", "4/7", "mission:4821",
             ["HubSpot customer record", "Support Postgres", "WhatsApp conversation", "Refund policy PDF"]),
            ("triage", "Daily support triage", "Support Triage", "running", "84%", "mission:triage", []),
            ("vti", "Review VTI exposure", "Portfolio Review", "completed", "Verified", "mission:vti", []),
            ("rel24", "Deploy release 2.4", "Release Workflow", "failed", "Verify step", "mission:rel24", []),
            ("prospect", "Weekly prospecting", "Prospecting", "scheduled", "—", "mission:prospect", []),
            ("outreach", "Outreach — Tasha at Nutrients.tech", "Cold Outreach", "needs", "5/8", "mission:outreach",
             ["Generated copy", "Generated hero asset"]),
        ]
        return [{"id": i, "title": t, "workflow": w, "state": s, "progress": p,
                 "context_used": ctx, **_prov("mission", r)}
                for i, t, w, s, p, r, ctx in m]

    def mission_detail(self, project_id: str, mission_id: str) -> dict:
        summary = next((m for m in self.missions(project_id) if m["id"] == mission_id), None)
        if summary is None:
            return {}
        return {"summary": summary, **_mission_evidence(mission_id)}

    def workflows(self, _pid: str) -> List[dict]:
        return [
            {"id": "refund", "name": "Customer Refund Handling",
             "steps": ["Find customer", "Find charge", "Request approval", "Refund", "Verify", "Reply"],
             "cadence": "Event-driven", "state": "active",
             "runsNote": "12 runs · 11 completed · 1 needs attention", **_prov("mission", "workflow:refund")},
            {"id": "triage", "name": "Daily Support Triage",
             "steps": ["Ingest", "Classify", "CRM", "Route", "Escalate"], "cadence": "Weekdays 08:00",
             "state": "scheduled", "runsNote": "runs daily", **_prov("mission", "workflow:triage")},
            {"id": "prospect", "name": "Weekly Prospecting",
             "steps": ["Discover", "Enrich", "Sequence", "Approve", "Activate"], "cadence": "Weekly",
             "state": "paused", "runsNote": "approval before activation", **_prov("mission", "workflow:prospect")},
        ]

    def attention(self, _pid: str) -> List[dict]:
        return [
            {"id": "a1", "kind": "approval", "title": "Approve refund · $129",
             "reason": "Sarah Chen · duplicate charge", "consequence": "Executes a Stripe refund (tier 4)",
             "available_actions": ["Review", "Approve", "Reject"], "priority": 90, **_prov("governance", "mission:4821")},
            {"id": "a2", "kind": "reconnect", "title": "Reconnect WhatsApp", "reason": "Token expires soon",
             "consequence": "Customer channel stops receiving", "available_actions": ["Connect"], "priority": 70,
             **_prov("connector", "app:whatsapp_business")},
            {"id": "a3", "kind": "discovery_review", "title": "Refund policy changed",
             "reason": "Stripe documentation", "consequence": "May affect the Customer Refund workflow",
             "available_actions": ["Review"], "priority": 60, **_prov("discovery", "finding:918")},
        ]

    def deployment_inspection(self, _pid: str) -> dict:
        """Phase 9 Deployment Inspection card. Offline sample snapshot by default; ``$PROJECTS_INSPECTION_TARGET``
        (an operator opt-in, never request-supplied) switches to a live read-only inspection of that deployment."""
        import os
        from agentic_os.agent_gateway.external.sample_projections import deployment_inspection_view
        return deployment_inspection_view(os.environ.get("PROJECTS_INSPECTION_TARGET", "").strip())

    # ── Today: the outcome-first feed (agentic_os.today). Per-session sandboxed sample ledger; a real
    # provider supplies its own implementation of these methods against the governed runtime.
    def _today(self, pid: str, session: str):
        from agentic_os.today import TodaySessions
        if getattr(self, "_today_sessions", None) is None:
            self._today_sessions = TodaySessions()
        return self._today_sessions.get(session or "anon", tenant="demo", project=pid,
                                        sample_cases=self.today_sample_cases)

    def today(self, pid: str, session: str = "") -> dict:
        return self._today(pid, session).feed(self.priorities(pid), self.attention(pid))

    def today_item(self, pid: str, item_id: str, session: str = "", targets=None, identity_id=None) -> dict:
        from agentic_os.today import TodayError
        t = self._today(pid, session)
        try:
            return t.item(item_id, targets=targets, identity_id=identity_id)
        except TodayError as e:
            if e.status != 404:
                raise
            err = e
        hit = next((i for i in self.today(pid, session)["items"] if i["id"] == item_id), None)
        if hit is None:
            raise err
        return {**hit, "why": hit["reason"], "evidence": [], "action": None, "history": [], "receipt": None}

    def today_approve(self, pid: str, item_id: str, session: str = "", **kw) -> dict:
        return self._today(pid, session).approve(item_id, **kw)

    def today_reconcile(self, pid: str, item_id: str, session: str = "", manual: Optional[str] = None,
                        by: str = "user:operator") -> dict:
        return self._today(pid, session).reconcile(item_id, manual=manual, by=by)

    def today_snooze(self, pid: str, item_id: str, session: str = "", seconds: float = 0) -> dict:
        return self._today(pid, session).snooze(item_id, seconds=seconds)

    def today_dismiss(self, pid: str, item_id: str, session: str = "") -> dict:
        return self._today(pid, session).dismiss(item_id)

    def today_batch(self, pid: str, session: str = "", **kw) -> dict:
        return self._today(pid, session).batch(**kw)

    def today_undo(self, pid: str, session: str = "", token: str = "") -> dict:
        return self._today(pid, session).undo(token)

    def today_notifications(self, pid: str, session: str = "") -> dict:
        return self._today(pid, session).notifications(self.priorities(pid), self.attention(pid))

    def today_ack(self, pid: str, session: str = "", keys=None) -> dict:
        return self._today(pid, session).ack_notifications(keys or [])

    def today_prefs(self, pid: str, session: str = "", **kw) -> dict:
        return self._today(pid, session).set_notification_prefs(**kw)

    def today_simulate_outcome(self, pid: str, item_id: str, session: str = "") -> dict:
        return self._today(pid, session).observe_outcome(item_id)

    def _intake(self, pid: str, session: str):
        from agentic_os.capture import CaptureError
        if not self.today_sample_cases:                       # fail closed: durable intake needs tenant+actor binding
            raise CaptureError(501, "unsupported", "capture is not enabled for this deployment")
        return self._today(pid, session).intake

    def capture(self, pid: str, session: str = "", **kw) -> dict:
        return self._intake(pid, session).capture(**kw)

    def capture_list(self, pid: str, session: str = "", status=None) -> list:
        return self._intake(pid, session).list(status=status)

    def capture_resolve(self, pid: str, capture_id: str, session: str = "", case_id: str = "") -> dict:
        return self._intake(pid, session).resolve(capture_id, case_id)

    def capture_archive(self, pid: str, capture_id: str, session: str = "") -> dict:
        return self._intake(pid, session).archive(capture_id)

    def social_intelligence(self, _pid: str) -> dict:
        """Phase 9 Social Intelligence card over the offline replay corpus (no live social provider)."""
        from agentic_os.agent_gateway.external.sample_projections import social_intelligence_view
        return social_intelligence_view()

    def priorities(self, _pid: str) -> dict:
        """The Priority Engine's cross-app 'what needs me?' surface (plan §2), driven by the SHIPPED
        detectors over this sample's example scenario. Growth trend scoring, Support follow-up, the
        Projects Execution Risk Radar and the Research Information-Gain Planner all produce real
        candidates through their own kernels; the refund approval and KB cleanup are direct candidates.
        A live deployment swaps in a provider whose sources read real data — hence ``basis`` is honest
        about what fed this ('sample' here, not a real deployment's signals)."""
        import time
        from agentic_os.agent_gateway.contracts import RiskTier
        from agentic_os.priority_engine import (
            InterventionCandidate, OutcomeLog, PriorityPolicy, collect_priorities, from_research_plan,
            from_risk_report, from_support_thread, from_trend_report, select_action)
        from agentic_os.crm_nba import DealSignals, next_best_action
        from agentic_os.outreach_nba import ProspectSignals, outreach_opportunity
        from agentic_os.analytics_anomaly import MetricAnomaly, anomaly_opportunity
        from agentic_os.wealth_drift import PlanAssumption, drift_opportunity
        from agentic_os.execution_risk import IsotonicCalibrator as _RiskCal
        from agentic_os.execution_risk import Mode as RiskMode
        from agentic_os.execution_risk import ProjectSignals, raw_risk
        from agentic_os.execution_risk import assess as risk_assess
        from agentic_os.research_planner import Belief, Investigation, ResearchPolicy
        from agentic_os.research_planner import plan as research_plan
        from agentic_os.support_autonomy import FollowUpPolicy, ThreadState, qualify_lead
        from agentic_os.trend_intelligence import Mode, TrendCandidate, assess
        now = time.time()

        # the shared learning loop, GATED: selection reads the registry's PROMOTED policy (static prior until
        # a promotion cycle has passed the gate). It does NOT refit from the raw log per request.
        learn_fn = self._learn_fn()

        def crm_nba_source():
            # CRM Next-Best-Action as a producer: the loop selects WHICH action for this deal, learning
            # from recorded outcomes; the chosen (outbound) action then competes for attention.
            opp = next_best_action(DealSignals("Acme", stage="engaged", buying_signal=True, engagement=0.6))
            sel = select_action(opp, utility_fn=learn_fn)
            return [] if sel.action.risk_tier == RiskTier.READ else [sel.action]

        def outreach_source():
            opp = outreach_opportunity(ProspectSignals("Tasha@Nutrients.tech", fresh_trigger=True))
            sel = select_action(opp, utility_fn=learn_fn)
            return [] if sel.action.risk_tier == RiskTier.READ else [sel.action]

        def analytics_source():
            # a material adverse drop in a good metric (activation rate)
            opp = anomaly_opportunity(MetricAnomaly("activation_rate", observed=0.38, baseline_mean=0.52,
                                                    baseline_std=0.04, higher_is_better=True))
            sel = select_action(opp, utility_fn=learn_fn)
            return [] if sel.action.risk_tier == RiskTier.READ else [sel.action]

        def wealth_source():
            opp = drift_opportunity(PlanAssumption("Retirement 2045", "long-run inflation ~2%",
                                                   assumed_value=2.0, current_value=3.6, tolerance=0.8))
            sel = select_action(opp, utility_fn=learn_fn)
            return [] if sel.action.risk_tier == RiskTier.READ else [sel.action]

        def growth_source():
            rising = [1, 1.5, 2.3, 3.6, 5.6, 8.7, 13.5, 21.0]
            report = assess(TrendCandidate(
                entity="agentic RAG for support",
                series={s: list(rising) for s in ("reddit", "youtube", "search")},
                creator_outliers=(8.0, 6.0), question_growth=1.0, geo_count=4), mode=Mode.EXPLORATORY)
            c = from_trend_report(report)
            return [c] if c else []

        def support_source():
            lead = qualify_lead(message="what's your pricing for the team plan?", has_email=True,
                                has_company=True, message_count=3)
            thread = ThreadState(last_inbound_at=now - 4000, status="awaiting_customer")
            c = from_support_thread(thread, FollowUpPolicy(clock=lambda: now).assess(thread), lead,
                                    thread_ref="sample:acme-pricing")      # a stable identity, not an object address
            return [c] if c else []

        def risk_source():
            signals = ProjectSignals(
                "Production pilot", days_remaining=12, work_remaining=0.7,
                progress_series=(0.10, 0.14, 0.17, 0.19, 0.20, 0.205), dependency_lags=(0.9,),
                blocked_dependencies=2, open_critical_decisions=1, oldest_decision_age_days=12)
            # raw risk is compressed; the radar runs with a calibrator (as in the backtest) — seed a
            # small one so this strong risk surfaces the way it would against real history.
            cal = _RiskCal().fit([0.05, 0.1, 0.2, raw_risk(signals), 0.5], [0, 0, 0, 1, 1])
            c = from_risk_report(risk_assess(signals, calibrator=cal, mode=RiskMode.BALANCED))
            return [c] if c else []

        def research_source():
            hyps = ("temporal graph helps", "no effect", "hurts recall")
            step = research_plan(
                Belief.uniform(hyps),
                [Investigation("temporal-benchmark", cost=1.0,
                               pos_likelihoods={"temporal graph helps": 0.9, "no effect": 0.3, "hurts recall": 0.1},
                               question="does temporal graph retrieval improve historical chat recall?"),
                 Investigation("false-negative-analysis", cost=1.5,
                               pos_likelihoods={"temporal graph helps": 0.7, "no effect": 0.5, "hurts recall": 0.4})],
                budget_remaining=5.0, policy=ResearchPolicy())
            c = from_research_plan(step, question="does temporal graph retrieval improve chat recall?")
            return [c] if c else []

        def direct_source():
            return [
                InterventionCandidate(
                    source_app="CRM", subject="Sarah Chen — duplicate charge",
                    proposed_action="Approve the $129 Stripe refund", expected_value=0.7,
                    confidence=0.95, urgency=0.8, risk_tier=RiskTier.CRITICAL, reversibility=0.2,
                    required_capabilities=("billing.refund.execute",), candidate_id="crm:refund:4821"),
                InterventionCandidate(
                    source_app="Knowledge", subject="Refund-policy article is stale",
                    proposed_action="Draft a corrected KB article for review", expected_value=0.4,
                    confidence=0.72, urgency=0.3, risk_tier=RiskTier.READ, candidate_id="kb:refund-policy"),
                InterventionCandidate(
                    source_app="CRM", subject="MaybeCorp — faint signal", proposed_action="Reach out",
                    expected_value=0.5, confidence=0.3, risk_tier=RiskTier.CONSEQUENTIAL,
                    candidate_id="crm:maybe"),
            ]

        summary = collect_priorities(
            [growth_source, support_source, risk_source, research_source, crm_nba_source,
             outreach_source, analytics_source, wealth_source, direct_source],
            PriorityPolicy(attention_budget=5), now=now)
        out = summary.as_dict()
        out["basis"] = "sample"    # honest: example detectors/data, not a live deployment's signals
        # the closed loop, honestly labelled: selection adapts to recorded outcomes (in simulation here)
        out["learning"] = self._learning_status(learn_fn)
        return out

    def _learn_fn(self):
        """The utility_fn of the registry's PROMOTED policy (None ⇒ static producer priors)."""
        reg = self.policy_registry
        reg.refresh()                       # see a promotion made by another process / a restart
        return reg.active.utility_fn()

    def _learning_status(self, learn_fn) -> dict:
        reg = self.policy_registry
        return {"outcomes_recorded": len(self.outcome_events), "selection_adjusted": learn_fn is not None,
                "policy_version": reg.active.version, "policy_kind": reg.active.kind}

    def run_promotion_cycle(self, *, apply: bool = False, gate=None, tenant_id: Optional[str] = None, **kwargs):
        """The ONLY path that changes the learned policy: collect → evaluate (held-out) → promote through
        the gate. ``apply=False`` is a dry run (returns the report, changes nothing). Wraps
        :func:`agentic_os.policy_promotion.run_promotion_cycle` over this provider's outcomes + registry."""
        from agentic_os.outcome_store import InMemoryOutcomeStore
        from agentic_os.policy_promotion import run_promotion_cycle
        store = InMemoryOutcomeStore()
        for ev in self.outcome_events:
            if getattr(ev, "simulated", False):       # defence in depth: synthetic data is never evidence
                continue
            if norm_tenant(ev.tenant_id) != norm_tenant(tenant_id):
                continue          # tenant isolation: ONLY this tenant's evidence (None => untenanted only)
            store.append(ev)
        return run_promotion_cycle(store, gate=gate, registry=self.policy_registry, apply=apply, **kwargs)

    def record_outcome_event(self, *, source_app: str, action_kind: str, candidate_id: str = "",
                             observed_reward: Optional[float] = None,
                             reward_dimensions: Optional[Dict[str, float]] = None,
                             attribution_confidence: float = 1.0, delay: float = 0.0,
                             tenant_id: Optional[str] = None, event_id: str = "",
                             selected_intervention_id: str = "", note: str = "") -> dict:
        """TRUSTED in-process write (app/connector code). NOT reachable anonymously: the HTTP surface goes
        through :meth:`record_authoritative_outcome` (token + receipt) or the demo sandbox.
        Record an observed outcome into the shared log — this is how a deployment closes the loop.
        ``delay`` is in SECONDS. The outcome feeds the NEXT promotion cycle (:meth:`run_promotion_cycle`); it
        does not change selection by itself (governance is untouched)."""
        from agentic_os.priority_engine import OutcomeEvent
        ev = OutcomeEvent(
            candidate_id=candidate_id, source_app=source_app, action_kind=action_kind,
            observed_reward=observed_reward, reward_dimensions=dict(reward_dimensions or {}),
            attribution_confidence=attribution_confidence, delay=delay, tenant_id=tenant_id,
            event_id=event_id, selected_intervention_id=selected_intervention_id, note=note)
        from agentic_os.outcome_store import dedupe_key
        k = dedupe_key(ev)
        with self._outcome_lock:                    # check-then-append is atomic
            prior = next((e for e in self.outcome_events if k and dedupe_key(e) == k), None)
            if prior is not None:
                if event_id and note and prior.note != note:
                    raise OutcomeRejected(409, "idempotency_key was already used with a different payload")
                return {"recorded": len(self.outcome_events), "durable": bool(self.persist_path),
                        "duplicate": True}
            self.outcome_events.append(ev)
            if self.persist_path:                   # durably append so it survives a restart
                from agentic_os.outcome_store import FileOutcomeStore
                FileOutcomeStore(self.persist_path).append(ev)
        return {"recorded": len(self.outcome_events), "durable": bool(self.persist_path), "duplicate": False}

    def record_authoritative_outcome(self, *, intervention_id: str, idempotency_key: str,
                                     attribution_confidence: Optional[float],
                                     observed_reward: Optional[float] = None,
                                     reward_dimensions: Optional[Dict[str, float]] = None,
                                     delay: float = 0.0, tenant_id: Optional[str] = None,
                                     execution_ref: str = "", mission_id: str = "",
                                     source_app: str = "", action_kind: str = "") -> dict:
        """Authenticated evidence write. The outcome must link to an EXECUTED InterventionRecord in
        :attr:`intervention_store`; producer identity (source_app/action_kind/candidate) is taken from that
        record, never from the caller. Raises :class:`OutcomeRejected` (4xx) and appends nothing otherwise."""
        if not intervention_id:
            raise OutcomeRejected(422, "intervention_id (decision_ref) is required")
        if not idempotency_key:
            raise OutcomeRejected(422, "idempotency_key is required")
        if attribution_confidence is None:
            raise OutcomeRejected(422, "attribution_confidence is required and must be within [0, 1]")
        validate_outcome_numbers(observed_reward=observed_reward, reward_dimensions=reward_dimensions,
                                 delay=delay, attribution_confidence=attribution_confidence)
        if observed_reward is None and not reward_dimensions:
            raise OutcomeRejected(422, "observed_reward or reward_dimensions is required")
        if len(idempotency_key) > 128 or len(intervention_id) > 256:
            raise OutcomeRejected(422, "idempotency_key/intervention_id too long")
        tenant_id = norm_tenant(tenant_id)
        rec = None
        if self.intervention_store is not None:
            rec = next((r for r in self.intervention_store.all() if r.intervention_id == intervention_id), None)
        if rec is None:
            raise OutcomeRejected(404, f"unknown intervention {intervention_id!r}")
        if rec.shadow or rec.executed_at is None or not rec.execution_ref:
            raise OutcomeRejected(409, f"intervention {intervention_id!r} was not executed (no receipt)")
        if execution_ref and execution_ref != rec.execution_ref:
            raise OutcomeRejected(409, "execution_ref does not match the intervention receipt")
        if mission_id and mission_id != rec.mission_id:
            raise OutcomeRejected(409, "mission_id does not match the intervention receipt")
        if (source_app and source_app != rec.source_app) or (
                action_kind and action_kind != (rec.action_kind or rec.selected_action)):
            raise OutcomeRejected(409, "source_app/action_kind do not match the intervention receipt")
        if norm_tenant(getattr(rec, "tenant_id", None)) != tenant_id:
            raise OutcomeRejected(409, "tenant does not match the intervention receipt")
        import hashlib
        import json as _json
        phash = hashlib.sha256(_json.dumps({
            "i": intervention_id, "r": observed_reward, "d": dict(reward_dimensions or {}),
            "c": attribution_confidence, "t": delay, "n": tenant_id, "e": rec.execution_ref, "m": rec.mission_id},
            sort_keys=True).encode()).hexdigest()
        return self.record_outcome_event(
            source_app=rec.source_app, action_kind=rec.action_kind or rec.selected_action,
            candidate_id=rec.candidate_id, observed_reward=observed_reward,
            reward_dimensions=reward_dimensions, attribution_confidence=attribution_confidence, delay=delay,
            tenant_id=tenant_id, event_id=f"idem:{idempotency_key}", selected_intervention_id=intervention_id,
            note=f"payload_sha256:{phash}")

    def discovery(self, _pid: str) -> List[dict]:
        return [
            {"id": "918", "title": "Stripe refund API behavior changed", "detail": "Source: Stripe documentation",
             "confidence": "High", "needsReview": True,
             "affects": {"workflows": ["Customer Refund Handling"], "missions": ["4821"], "scheduled": 4},
             **_prov("discovery", "finding:918")},
            {"id": "919", "title": "HubSpot property schema changed", "detail": "Affects CRM reconciliation",
             "confidence": "Medium", "needsReview": False,
             "affects": {"workflows": ["CRM reconciliation"], "missions": [], "scheduled": 0}, **_prov("discovery")},
            {"id": "920", "title": "Contradictory evidence detected", "detail": "Mission: Portfolio Review #221",
             "confidence": "", "needsReview": False,
             "affects": {"workflows": [], "missions": ["221"], "scheduled": 0}, **_prov("discovery")},
        ]

    def activity(self, _pid: str) -> List[dict]:
        rows = [
            ("e1", "18:42", "Mission #4821 requested approval in Slack", "governance"),
            ("e2", "18:40", "Stripe charge ch_3Q… matched · $129", "connector"),
            ("e3", "18:39", "Discovery linked customer evidence · Sarah Chen", "discovery"),
            ("e4", "18:38", "WhatsApp request received · \"I was charged twice\"", "mission"),
            ("e5", "18:30", "Slack connection verified", "connector"),
            ("e6", "18:24", "Workflow policy updated · Customer Refund Handling", "project"),
        ]
        return [{"id": i, "time": t, "text": x, **_prov(rt)} for i, t, x, rt in rows]

    def apps(self, _pid: str) -> List[dict]:
        apps = _sample_apps()
        for a in apps:  # reflect the live connection set
            if a["provider"] in self.connected:
                if a["state"] == "NOT_CONNECTED":
                    a.update(state="VERIFIED_READ", health="ok", verified="Verified just now")
            else:
                a.update(state="NOT_CONNECTED", health="mut")
        # A deployment can offer only a subset of apps via $PROJECTS_APPS; unset = all.
        return enabled_apps(apps, enabled_selection())

    def connect_app(self, provider: str) -> dict:
        """Mark a provider connected (a served-app deployment plugs a real HostedConnectSession
        here; the sample simulates the outcome so Connect → readiness updates end to end)."""
        self.connected.add(provider)
        return {"provider": provider, "state": "VERIFIED_READ", "connected": True,
                "detail": "connected (sample: simulated hosted OAuth)"}

    def sources(self, _pid: str) -> List[dict]:
        return _sample_sources()

    def runtime(self, _pid: str) -> dict:
        return _sample_runtime()

    def templates(self, _pid: str) -> List[dict]:
        source_ids = {s["source_id"] for s in _sample_sources()}
        out: List[dict] = []
        for t in _sample_templates():
            deps = t.pop("_deps")
            t["readiness"] = [
                {"label": lbl, "ready": (key in self.connected) if kind == "app" else (key in source_ids)}
                for (lbl, kind, key) in deps
            ]
            out.append(t)
        return out

    def overview(self, project_id: str) -> dict:
        return {
            "project": self.projects()[0],
            "attention": self.attention(project_id),
            "missions": self.missions(project_id),
            "workflows": self.workflows(project_id),
            "discovery": self.discovery(project_id),
            "apps": self.apps(project_id),
            "sources": self.sources(project_id),
            "runtime": self.runtime(project_id),
        }


def _app(provider: str, display_name: str, used_for: str, state: str, health: str,
         caps: List[str], **extra: Any) -> Dict[str, Any]:
    return {"provider": provider, "display_name": display_name, "used_for": used_for,
            "state": state, "health": health, "verified": extra.get("verified", ""),
            "capabilities": caps, "setup_url": extra.get("setup_url", ""),
            "manual_steps": extra.get("manual_steps", []), "required_scopes": extra.get("required_scopes", []),
            "credential_fields": extra.get("credential_fields", []), **_prov("connector", f"app:{provider}")}


def _sample_apps() -> List[dict]:
    return [
        _app("whatsapp_business", "WhatsApp Business", "Receive customer messages and reply.",
             "MISSION_READY", "warn", ["chat.message.send"], verified="Verified 2 days ago"),
        _app("hubspot", "HubSpot", "Find the customer and record the conversation.",
             "MISSION_READY", "ok", ["crm.contact.upsert", "crm.note.create"], verified="Verified 11 min ago"),
        _app("slack", "Slack", "Notifications, approvals, replies in threads.",
             "MISSION_READY", "ok", ["chat.message.send", "approval.request", "chat.message.read"], verified="Verified 3 min ago"),
        _app("stripe", "Stripe", "Find charges and execute approved refunds.",
             "MISSION_READY", "ok", ["billing.charge.find", "billing.refund.execute"], verified="Verified 5 min ago"),
        _app("gmail", "Gmail", "Send email confirmations (optional).",
             "NOT_CONNECTED", "mut", ["email.message.send"]),
        _app("ayrshare", "Ayrshare", "Publish content to many venues in one call.",
             "NOT_CONNECTED", "mut", ["content.publish"]),
    ]


def _source(source_id: str, name: str, kind: str, location: str, health_state: str, detail: str,
            stats: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"source_id": source_id, "name": name, "kind": kind, "provider": extra.get("provider", ""),
            "location": location, "access_mode": extra.get("access_mode", "read_only"),
            "indexing_policy": extra.get("indexing_policy", "automatic"),
            "refresh_policy": extra.get("refresh_policy", "on_change"),
            "exposure_class": extra.get("exposure_class", "internal"),
            "health": {"state": health_state, "detail": detail, "last_observed_at": extra.get("last", "")},
            "stats": stats, "allowed_paths": extra.get("allowed_paths", []),
            "allowed_schemas": extra.get("allowed_schemas", []), "allowed_tables": extra.get("allowed_tables", []),
            "allowed_content_types": extra.get("allowed_content_types", []), "denied": extra.get("denied", []),
            "last_verified": extra.get("last", ""), "source_fingerprint": extra.get("fp", ""),
            **_prov("context", f"source:{source_id}")}


def _sample_sources() -> List[dict]:
    return [
        _source("pdfs", "Customer policy docs", "files", "~/company-docs", "healthy",
                "391 indexed", {"discovered": 428, "indexed": 391, "skipped": 37},
                allowed_content_types=["pdf", "docx", "markdown"], last="2 min ago", fp="a1b2c3"),
        _source("crm", "CRM database", "database", "postgres · localhost/customer_ops", "healthy",
                "Live connection", {"schemas": 2, "tables": 42}, provider="postgres",
                allowed_schemas=["public", "support"], denied=["billing.card_data", "hr.*"], last="live"),
        _source("gdrive", "Google Drive", "cloud_files", "Drive · Policies", "healthy",
                "Synced 4 min ago", {"files": 1842, "indexed": 1842}, provider="google_drive", last="4 min ago"),
        _source("s3", "Support archive", "cloud_files", "s3://support-archive", "stale",
                "Credentials expired · last sync 2 days ago", {"files": 12045}, provider="s3", last="2 days ago"),
    ]


def _sample_runtime() -> dict:
    h = lambda name, state, detail: {"name": name, "state": state, "detail": detail}
    return {
        "runtimes": [h("Mission Runtime", "ok", "Healthy"), h("Context Runtime", "ok", "Healthy"),
                     h("Discovery Runtime", "ok", "Healthy")],
        "models": [{"name": "Local model", "role": "primary", "state": "ok"},
                   {"name": "Cloud fallback", "role": "fallback", "state": "mut"}],
        "security": {"credential_broker": "ok", "policy": "ok"},
        "apps": [{"name": "WhatsApp", "state": "warn"}, {"name": "HubSpot", "state": "ok"},
                 {"name": "Slack", "state": "ok"}, {"name": "Polar", "state": "ok"}],
        "sources": [{"name": "Customer PDFs", "state": "ok"}, {"name": "CRM Postgres", "state": "ok"},
                    {"name": "Google Drive", "state": "ok"}, {"name": "S3 archive", "state": "warn"}],
        **_prov("project", "runtime:health"),
    }


def _sample_templates() -> List[dict]:
    # deps are (label, kind, key): kind "app" keys a provider id, "source" keys a source id.
    # readiness is computed against the live connection state in SampleProjectionProvider.templates.
    A, S = "app", "source"

    def tpl(tid, goal, caps, srcs, auth, wf, deps):
        return {"id": tid, "goal": goal, "required_capabilities": caps, "required_sources": srcs,
                "authority_requirements": auth, "suggested_workflow": wf, "_deps": deps}
    return [
        tpl("refunds", "Handle customer refunds",
            ["chat.message.send", "crm.contact.upsert", "approval.request", "billing.refund.execute"],
            ["Customer policy docs", "CRM database"], ["Approval required before refund"],
            "Customer Refund Handling",
            [("WhatsApp", A, "whatsapp_business"), ("HubSpot", A, "hubspot"), ("Slack", A, "slack"),
             ("Polar", A, "polar"), ("Customer policy docs", S, "pdfs"), ("CRM database", S, "crm")]),
        tpl("prospect", "Run weekly prospecting",
            ["crm.contact.upsert", "email.message.send"], ["CRM database"], [],
            "Weekly Prospecting",
            [("Apollo", A, "apollo"), ("Gmail", A, "gmail"), ("HubSpot", A, "hubspot"), ("CRM database", S, "crm")]),
        tpl("triage", "Review support queue", ["chat.message.read", "crm.contact.upsert"],
            ["CRM database"], [], "Daily Support Triage",
            [("Slack", A, "slack"), ("HubSpot", A, "hubspot"), ("CRM database", S, "crm")]),
        tpl("reconcile", "Reconcile CRM", ["crm.contact.upsert", "crm.note.create"], ["CRM database"], [],
            "CRM Reconciliation", [("HubSpot", A, "hubspot"), ("CRM database", S, "crm")]),
        tpl("outreach", "Run cold outreach for the agentic-apps stack",
            ["generate.copy", "generate.asset", "outreach.sequence.configure", "outreach.enroll"],
            [], ["Sequence activation is provider-UI-only (a human toggles it on)"],
            "Cold Outreach",
            [("Apollo", A, "apollo")]),
    ]


def _mission_evidence(mission_id: str) -> Dict[str, Any]:
    """The Mission's ACTIONS-used and EVIDENCE-used, each with a 'why' — the Used+Why half of
    the Available/Used/Why symmetry (Available comes from /apps and /sources). Evidence
    distinguishes QUERY evidence (records retrieved live, with count + observed time) from
    CATALOG/FILE evidence (identity: fingerprint/version), matching sources_postgres."""
    if mission_id == "outreach":
        return _outreach_evidence()
    if mission_id != "4821":
        return {"steps": [], "context_used": [], "context_plan_note": ""}
    steps = [  # ACTIONS used · provider chosen · why (EXPLAIN)
        {"n": 1, "capability": "chat.message.read", "provider": "whatsapp_business", "tier": 2,
         "status": "done", "why": "inbound channel the request arrived on"},
        {"n": 2, "capability": "crm.contact.upsert", "provider": "hubspot", "tier": 2,
         "status": "done", "why": "named CRM; only connected contact store"},
        {"n": 3, "capability": "billing.order.find", "provider": "polar", "tier": 1,
         "status": "done", "why": "billing provider of record for this account"},
        {"n": 4, "capability": "approval.request", "provider": "slack", "tier": 3,
         "status": "waiting", "why": "policy requires human approval before a refund"},
        {"n": 5, "capability": "billing.refund.execute", "provider": "polar", "tier": 4,
         "status": "todo", "why": "moves money — runs only after approval, then verified"},
    ]
    context_used = [  # EVIDENCE used · how retrieved · why (Context Plan)
        {"source_id": "crm", "source_name": "CRM database", "provider": "postgres", "kind": "database",
         "evidence_kind": "query",
         "retrieved": {"count": 3, "observed_at": "2026-09-09T11:31:00Z"}, "identity": None,
         "refs": [{"ref": "postgres:customers#0", "summary": "id=8821 · email=sarah@…"},
                  {"ref": "postgres:support.tickets#0", "summary": "subject=Billed twice · status=open"}],
         "why": "scoped SQL against the live source — Context Runtime queried in place rather than ingesting the DB"},
        {"source_id": "pdfs", "source_name": "Refund Policies", "provider": "google_drive", "kind": "cloud_files",
         "evidence_kind": "file",
         "retrieved": None, "identity": {"fingerprint": "a1b2c3", "version": "v19"},
         "refs": [{"ref": "gdrive:f1", "summary": "Refund Policy.pdf"}],
         "why": "vector retrieval over indexed policy PDFs — the right representation for prose"},
    ]
    return {"steps": steps, "context_used": context_used,
            "context_plan_note": "Sources define what evidence is available; Context Runtime chose SQL-in-place "
                                 "for the structured customer data and vector retrieval for the policy prose."}


def _outreach_evidence() -> Dict[str, Any]:
    """The outreach Mission (synthesis → configure → boundary → verify). Logical steps are
    portable; the ACTIVATE_SEQUENCE step is a PHYSICAL capability result — Apollo activation is
    provider-UI-only, so it waits on a human. Generated copy + hero image are the created
    artifacts (creative asset is optional)."""
    steps = [
        {"n": 1, "capability": "prepare.outreach", "provider": "runtime", "tier": 0,
         "status": "done", "why": "target Tasha · Nutrients.tech · cold outreach"},
        {"n": 2, "capability": "generate.copy", "provider": "claude", "tier": 0,
         "status": "done", "why": "grounded in a nutrition-tech ops example; subject prefixed [test]"},
        {"n": 3, "capability": "generate.asset", "provider": "fal.ai", "tier": 0,
         "status": "done", "why": "optional creative asset — multimodal composition"},
        {"n": 4, "capability": "outreach.sequence.configure", "provider": "apollo", "tier": 3,
         "status": "done", "why": "built the sequence step + email template (wait_mode day; template endpoint)"},
        {"n": 5, "capability": "outreach.enroll", "provider": "apollo", "tier": 3,
         "status": "done", "why": "enrolled tasha@nutrients.tech from the warmed mailbox"},
        {"n": 6, "capability": "outreach.sequence.activate", "provider": "apollo", "tier": 4,
         "status": "waiting", "why": "PROVIDER_UI_REQUIRED — Apollo activation is UI-only (capability advertises "
                                     "automatable=false); a human flips the sequence on"},
        {"n": 7, "capability": "outreach.observe", "provider": "apollo", "tier": 1,
         "status": "todo", "why": "after activation, observe the send through the mailbox"},
        {"n": 8, "capability": "verify.delivery", "provider": "apollo", "tier": 1,
         "status": "todo", "why": "confirm delivery and produce an ExecutionReceipt"},
    ]
    context_used = [
        {"source_id": "copy", "source_name": "Generated copy", "provider": "claude", "kind": "artifact",
         "evidence_kind": "file", "retrieved": None, "identity": {"version": "v1"},
         "refs": [{"ref": "artifact:copy", "summary": "[test] One governed system for Nutrients.tech's apps + data"}],
         "why": "synthesized from the goal + target context"},
        {"source_id": "asset", "source_name": "Generated hero asset", "provider": "fal.ai", "kind": "artifact",
         "evidence_kind": "file", "retrieved": None, "identity": {"version": "v1"},
         "refs": [{"ref": "artifact:hero", "summary": "conceptual hero — apps + DB + doc → one governed system"}],
         "preview": "/hero.jpg", "why": "optional creative asset (copy is required; asset is not)"},
    ]
    return {"steps": steps, "context_used": context_used,
            "context_plan_note": "Logical outreach workflow is provider-independent; activation is a physical "
                                 "capability result — Apollo is provider-UI-only, so the Mission pauses for a human."}


def propose_sources(project_id: str, text: str):
    """Deterministic 'use X as context' interpreter → an editable SourceConnectionProposal.
    Mirrors the integration wizard: infers reversible choices (read-only, content types),
    asks only what can't be safely guessed (which tables). The real Sidekick uses an LLM read."""
    from .sources import AccessMode, IndexingPolicy, ProposedSource, SourceConnectionProposal, SourceKind
    import re

    t = (text or "")
    tl = t.lower()
    proposed: List[Any] = []
    assumptions: List[str] = []
    questions: List[str] = []

    for m in re.finditer(r"(~?/[\w./\-]+)", t):  # any unix-ish path
        proposed.append(ProposedSource(kind=SourceKind.FILES, location=m.group(1),
                                       access_mode=AccessMode.READ_ONLY, indexing_policy=IndexingPolicy.AUTOMATIC))
        assumptions.append(f"{m.group(1)} — read-only, automatic indexing, common document types")
    if "postgres" in tl or "database" in tl or "db " in tl:
        loc = "localhost/customer_ops"
        mm = re.search(r"database on ([\w.:/\-]+)", tl)
        if mm:
            loc = mm.group(1)
        proposed.append(ProposedSource(kind=SourceKind.DATABASE, location=loc, provider="postgres",
                                       access_mode=AccessMode.READ_ONLY))
        assumptions.append(f"{loc} — read-only connection")
        questions.append("Which schemas/tables may be used as context?")
    if "google drive" in tl or "gdrive" in tl:
        proposed.append(ProposedSource(kind=SourceKind.CLOUD_FILES, location="Google Drive",
                                       provider="google_drive", access_mode=AccessMode.READ_ONLY))
        assumptions.append("Google Drive — connect via provider OAuth, then pick folders")
    if "onedrive" in tl or "sharepoint" in tl:
        proposed.append(ProposedSource(kind=SourceKind.CLOUD_FILES, location="OneDrive",
                                       provider="microsoft_onedrive", access_mode=AccessMode.READ_ONLY))
        assumptions.append("Microsoft OneDrive/SharePoint — connect via Microsoft OAuth, then pick folders")

    return SourceConnectionProposal(project_id=project_id, sources=proposed,
                                    assumptions=assumptions, questions=questions)


def build_source_registry(resolver: Optional[Any] = None, *, use_rag: Optional[bool] = None) -> Any:
    """A registry routing each source kind to its real connector. Files are always real
    (stdlib LocalFilesConnector). Database (PostgreSQL) and cloud files (Google Drive) are
    registered — and connect for real — only when a credential ``resolver`` is provided (a
    deployment binds its CredentialBroker here). Without one they stay 'pending' rather than
    attempting a live connection during a demo.

    ``use_rag`` (or ``$PROJECTS_RAG``) binds the live Context-Runtime/RAG indexer to the files
    connector so file/drive content is actually indexed + retrievable — needs ``[rag]``."""
    import os
    from .sources import LocalFilesConnector, SourceConnectorRegistry
    if use_rag is None:
        use_rag = os.environ.get("PROJECTS_RAG", "").lower() in ("1", "true", "yes")

    reg = SourceConnectorRegistry()
    if use_rag:
        from .sources_rag import RagIndexer
        reg.register(LocalFilesConnector(indexer=RagIndexer()))  # real indexing + retrieval
    else:
        reg.register(LocalFilesConnector())  # stdlib scan (CountingIndexer)
    if resolver is not None:
        from .sources_drive import GoogleDriveSourceConnector
        from .sources_onedrive import MicrosoftGraphSourceConnector
        from .sources_postgres import PostgresSourceConnector
        indexer_kw = {}
        if use_rag:
            from .sources_rag import RagIndexer
            indexer_kw = {"indexer": RagIndexer()}
        reg.register(PostgresSourceConnector(resolver=resolver))
        # Two cloud_files providers coexist via the registry's provider index (routing on the
        # spec's ``provider``): Google Drive and Microsoft OneDrive/SharePoint.
        reg.register(GoogleDriveSourceConnector(resolver=resolver, **indexer_kw))
        reg.register(MicrosoftGraphSourceConnector(resolver=resolver, **indexer_kw))
    return reg


ENV_SOURCE_ROOTS = "PROJECTS_SOURCE_ROOTS"   # os.pathsep-separated folders anonymous callers may confirm


def _source_roots() -> List[str]:
    """The configured allow-list. Only absolute, non-filesystem-root folders count: a relative entry depends on the
    process's working directory and ``/`` would allow everything, so both are ignored (and logged)."""
    import logging
    import os
    out: List[str] = []
    for r in os.environ.get(ENV_SOURCE_ROOTS, "").split(os.pathsep):
        if not r.strip():
            continue
        expanded = os.path.expanduser(r.strip())
        if "\x00" in expanded or not os.path.isabs(expanded):
            logging.getLogger(__name__).warning("%s: ignoring non-absolute entry", ENV_SOURCE_ROOTS)
            continue
        real = os.path.realpath(expanded)
        if real == os.path.dirname(real):
            logging.getLogger(__name__).warning("%s: ignoring filesystem root entry", ENV_SOURCE_ROOTS)
            continue
        out.append(real)
    return out


def authorize_source_locations(source_specs: List[dict], authorization: Optional[str]) -> List[dict]:
    """HTTP guard for ``confirm_and_connect_sources``: a ``files`` source is scanned from the SERVER's
    filesystem, so every one must resolve (symlinks and ``..`` included) under ``$PROJECTS_SOURCE_ROOTS``;
    otherwise the caller must present the control token (``require_control_token``: 503 unset, 401 wrong).

    Returns the specs with every file location replaced by the path that was checked (the realpath), so the
    connector scans exactly what was authorised. A location containing a NUL byte is a 422."""
    import os
    roots = _source_roots()
    resolved: List[dict] = []
    token_checked = False
    for s in source_specs or []:
        s = dict(s or {})
        kind = str(s.get("kind", "files") or "files").lower()
        if kind not in ("files", "file", "folder"):
            resolved.append(s)
            continue
        raw = str(s.get("location", "") or "")
        if "\x00" in raw:
            raise HTTPException(422, "invalid location")
        loc = os.path.realpath(os.path.expanduser(raw))
        s["location"] = loc
        resolved.append(s)
        if any(loc == r or loc.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            continue
        if not token_checked:
            from agentic_os.mission.control_auth import require_control_token
            require_control_token(authorization)
            token_checked = True
    return resolved


def confirm_and_connect_sources(project_id: str, source_specs: List[dict], confirmed_by: str,
                                registry: Optional[Any] = None) -> List[dict]:
    """Confirm an (already-answered) proposal and actually connect the sources through the
    right connector. Files are scanned for real by the stdlib LocalFilesConnector; database/
    cloud route to PostgresSourceConnector/GoogleDriveSourceConnector when the ``registry``
    carries them (see :func:`build_source_registry`), else report 'pending'. Returns
    ContextSource projections for the UI."""
    from .sources import (AccessMode, ConfirmedSourceIntent, IndexingPolicy, ProposedSource,
                          SourceConnectorRegistry, SourceKind)

    specs = []
    for s in source_specs:
        specs.append(ProposedSource(
            kind=SourceKind(s.get("kind", "files")), location=s.get("location", ""),
            name=s.get("name", ""), provider=s.get("provider", ""),
            access_mode=AccessMode(s.get("access_mode", "read_only")),
            allowed_content_types=list(s.get("allowed_content_types", [])),
            allowed_schemas=list(s.get("allowed_schemas", [])),
            indexing_policy=IndexingPolicy(s.get("indexing_policy", "automatic")),
        ))
    intent = ConfirmedSourceIntent(project_id=project_id, sources=tuple(specs),
                                   confirmed_by=confirmed_by, confirmed_at="")
    reg = registry if registry is not None else SourceConnectorRegistry.default()
    return [cs.to_projection() for cs in reg.connect(intent)]


def apps_from_setup_guides(connected: Optional[Set[str]] = None) -> Optional[List[dict]]:
    """Build the apps projection from the live connector setup guides when the ``[connectors]``
    plugin is installed — so the UI shows the real Integration Plane's providers, capabilities
    and setup steps. Connection *state* is not the guide's to know: it comes from ``connected``
    (the provider's live set), so Connect flips a provider here too. Returns ``None`` when the
    plugin is absent (caller uses the sample)."""
    try:
        from redevops_connectors import SETUP_GUIDES  # type: ignore
    except ImportError:
        return None
    connected = connected or set()
    out: List[dict] = []
    for g in SETUP_GUIDES.values():
        is_on = g.provider in connected
        out.append({
            "provider": g.provider, "display_name": g.display_name, "used_for": g.used_for,
            "state": "VERIFIED_READ" if is_on else "NOT_CONNECTED",
            "health": "ok" if is_on else "mut", "verified": "Verified just now" if is_on else "",
            "capabilities": [], "setup_url": g.setup_url,
            "manual_steps": list(g.manual_steps), "required_scopes": list(g.required_scopes),
            "credential_fields": [{"name": c.name, "label": c.label, "secret": c.secret}
                                  for c in g.credential_fields],
            **_prov("connector", f"app:{g.provider}", f"guide:{g.provider}"),
        })
    return out


def _is_priorities_query(t: str) -> bool:
    """An ACTIONABLE 'what needs me?' — wants the live prioritised surface, not an explanation of the
    feature. Explanatory phrasings ('how does…', 'what is…') fall through to the KB roadmap explainer."""
    if any(x in t for x in ("how does", "how do", "what is", "what's the", "explain", "roadmap")):
        return False
    return any(k in t for k in (
        "what needs me", "what needs my attention", "what should i work on", "what should i do next",
        "on my plate", "anything need me", "anything need my attention", "what needs doing",
        "what do i need to look at", "my priorities", "what needs approval", "what needs my sign"))


def sidekick_reply(ctx: Dict[str, Any], text: str,
                   provider: Optional["ProjectionProvider"] = None) -> Dict[str, Any]:
    """A governed conversational stand-in honouring the context contract — 'this' resolves to
    ``ctx.objectRef``. The real Sidekick compiles requests through the wizard + Mission Runtime;
    this keeps the surface live meanwhile."""
    t = (text or "").lower()
    obj = ctx.get("objectRef") or "this"
    # Cross-app attention surface (plan §2): an actionable 'what needs me?' returns the live Priority
    # Engine surface. Checked before the KB so the real list wins over the roadmap explainer.
    if provider is not None and _is_priorities_query(t):
        fn = getattr(provider, "priorities", None)
        if fn is not None:
            surf = fn(ctx.get("project") or "customer-ops")
            return {"text": surf.get("summary", ""), "topic": "Priorities",
                    "items": surf.get("surfaced", []),
                    "actions": [{"label": "Open Projects", "kind": "navigate", "ref": "attention"}]}
    if "two approver" in t or "$500" in t:
        return {"text": f"Proposed on {obj}: refunds above $500 require two approvers. Governed policy change — confirm to commit.",
                "actions": [{"label": "Confirm", "kind": "commit"}]}
    if "why" in t and "approv" in t and not any(w in t for w in ("stay", "staged", "pending", "executing")):
        return {"text": "This is billing.refund.execute — a tier-4 action that moves money, so policy requires your approval. It runs under a GovernedEnvelope and is verified by re-reading the refund."}
    if "affected" in t or "which active mission" in t:
        return {"text": "The Stripe refund-API finding affects 1 workflow, 1 active Mission (#4821), and 4 scheduled Missions.",
                "actions": [{"label": "Pause scheduled", "kind": "pause"}]}
    if ("refund" in t and "whatsapp" in t) or "connect whatever" in t or "handle refund" in t:
        return {"text": "I'd wire WhatsApp → HubSpot → Stripe → Slack approval → refund → verify → reply. Slack/HubSpot/Stripe are connected; still needed: WhatsApp. Refund needs approval; Stripe test mode first.",
                "actions": [{"label": "Set this up", "kind": "setup"}, {"label": "Change something", "kind": "edit"}]}
    # Sidekick → a REAL Mission: an actionable goal (not a question) becomes durable Mission state instead
    # of a scripted reply, WHEN the deployment binds a Mission Runtime (provider.mission_runtime). Creating
    # the mission is planning — its consequential steps stay approval-gated; prose never authorizes
    # execution (mission.from_sidekick / from_intent). No runtime bound ⇒ this is skipped and the demo is
    # unchanged.
    runtime = getattr(provider, "mission_runtime", None) if provider is not None else None
    if runtime is not None:
        from agentic_os.mission.from_sidekick import SidekickMissionBridge, is_actionable_goal
        if is_actionable_goal(text):
            try:
                m = SidekickMissionBridge(runtime).compile(
                    text, actor=str(ctx.get("actor", "")), project_id=str(ctx.get("project", "")))
                tail = " It's waiting on your approval before any consequential step." if m.needs_approval \
                    else f" It's {m.state.lower()}."
                return {"text": f"I turned that into a governed Mission ({m.kind.replace('_', ' ')}).{tail}",
                        "topic": "Mission", "mission_id": m.mission_id,
                        "actions": [{"label": "Open Mission", "kind": "navigate", "ref": "missions"}]}
            except Exception:
                pass   # any runtime error → fall through to the curated KB / fallback, never a hard failure

    # Sidekick is also the stack's in-product expert: authoritative, curated answers about how
    # credentials are handled, where data is processed (local vs cloud), and how execution is
    # governed — so nobody has to read a manual. Consulted before the generic fallback.
    from agentic_os.stack_knowledge import answer_stack_question
    kb = answer_stack_question(text)
    if kb is not None:
        reply: Dict[str, Any] = {"text": kb.answer, "topic": kb.topic,
                "actions": [{"label": "Where this is enforced", "kind": "explain", "ref": kb.source}]}
        detail = getattr(kb, "detail", "")   # tiered topics (e.g. component explainers) carry a deeper tier
        if detail:
            reply["detail"] = detail
        return reply
    # No confident curated match. If a model is wired, answer STRICTLY GROUNDED on the KB corpus
    # (it may cover paraphrases and the long tail, but only from verified facts, and it declines to
    # the team when the facts don't cover the question). No model configured → generic fallback.
    assistant = _get_sidekick_assistant()
    if assistant is not None:
        ga = assistant.answer(text, ctx)
        if ga is not None:
            reply: Dict[str, Any] = {"text": ga.text, "topic": "Sidekick", "grounded": True}
            if ga.sources:
                reply["actions"] = [{"label": "Where this is enforced", "kind": "explain",
                                     "ref": ga.sources[0]}]
            return reply
    return {"text": "I can turn that into a governed Mission across your connected apps. Want me to propose the steps? "
                    "You can also ask me how your credentials are handled or whether your data stays local."}


class _TodayApproveReq(BaseModel):
    payload_digest: str
    targets: Optional[List[str]] = None
    identity_id: Optional[str] = None


class _CaptureReq(BaseModel):
    content: str
    source: str = "manual"
    observed_at: Optional[float] = None
    attachments: List[Any] = []


class _CaptureResolveReq(BaseModel):
    case_id: str


class _TodayBatchReq(BaseModel):
    action: str
    item_ids: List[str]
    seconds: Optional[float] = None
    assignee: Optional[str] = None
    category: Optional[str] = None


class _TodayUndoReq(BaseModel):
    token: str


class _TodayAckReq(BaseModel):
    keys: List[str]


class _TodayPrefsReq(BaseModel):
    enabled: Optional[bool] = None
    quiet_start: Optional[int] = None
    quiet_end: Optional[int] = None
    tz_offset_minutes: Optional[int] = None
    clear_quiet: bool = False


class _TodaySnoozeReq(BaseModel):
    seconds: float


class _SidekickReq(BaseModel):
    ctx: Dict[str, Any] = {}
    text: str = ""


class OutcomeRejected(Exception):
    """An authoritative outcome write that fails validation/receipt resolution (maps to an HTTP 4xx)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


class _OutcomeReq(BaseModel):
    source_app: str = Field(default="", max_length=256)
    action_kind: str = Field(default="", max_length=256)
    candidate_id: str = Field(default="", max_length=256)
    observed_reward: Optional[float] = None
    reward_dimensions: Dict[str, float] = {}
    attribution_confidence: Optional[float] = None   # authoritative path: REQUIRED, within [0, 1]
    delay: float = 0.0               # SECONDS between the action and the outcome
    # authoritative (operator) path only — requires a bearer token AND an executed-intervention receipt:
    authoritative: bool = False
    intervention_id: str = Field(default="", max_length=256)        # a.k.a. decision_ref
    decision_ref: str = Field(default="", max_length=256)
    execution_ref: str = Field(default="", max_length=256)
    mission_id: str = Field(default="", max_length=256)
    idempotency_key: str = Field(default="", max_length=128)
    tenant_id: Optional[str] = Field(default=None, max_length=128)


class DemoOutcomeSandbox:
    """In-memory, bounded, TTL'd store for SYNTHETIC outcomes from anonymous callers (namespace ``demo``).
    It holds plain dict records (never OutcomeEvents), is never persisted, and has no reader in the
    promotion / correlation / export paths. Bounds: per session, per ``group`` (client IP), sessions, and a
    GLOBAL event budget, so a caller cannot widen its allowance by rotating the session header."""
    NAMESPACE = "demo"

    def __init__(self, *, cap_per_session: int = 50, max_sessions: int = 500, ttl_seconds: float = 900.0,
                 cap_per_group: int = 100, global_cap: int = 5000, clock=None) -> None:
        import time as _t
        self.cap, self.max_sessions, self.ttl = cap_per_session, max_sessions, ttl_seconds
        self.cap_per_group, self.global_cap = cap_per_group, global_cap
        self._clock = clock or _t.time
        self._sessions: Dict[str, List[dict]] = {}
        self._group: Dict[str, str] = {}
        import threading
        self._lock = threading.Lock()

    def _expire(self) -> None:
        now = self._clock()
        for sid in [k for k, v in self._sessions.items() if not v or now - v[-1]["ts"] > self.ttl]:
            del self._sessions[sid]
            self._group.pop(sid, None)

    def add(self, session: str, record: dict, group: str = "") -> dict:
        with self._lock:
            self._expire()
            # graceful degradation: when sessions / the global budget are exhausted evict the OLDEST other
            # sessions instead of refusing everyone, so one client cannot disable the sandbox for all.
            while ((session not in self._sessions and len(self._sessions) >= self.max_sessions)
                   or sum(len(v) for v in self._sessions.values()) >= self.global_cap):
                others = [k for k in self._sessions if k != session]
                if not others:
                    return {"stored": False, "reason": "demo capacity reached"}
                oldest = min(others, key=lambda k: self._sessions[k][-1]["ts"] if self._sessions[k] else 0)
                del self._sessions[oldest]
                self._group.pop(oldest, None)
            if group and sum(len(v) for k, v in self._sessions.items()
                             if self._group.get(k) == group) >= self.cap_per_group:
                return {"stored": False, "reason": f"demo cap of {self.cap_per_group} events per client reached"}
            evs = self._sessions.setdefault(session, [])
            self._group[session] = group
            if len(evs) >= self.cap:
                return {"stored": False, "reason": f"demo cap of {self.cap} events per session reached"}
            evs.append({**record, "ts": self._clock(), "demo": True, "simulated": True,
                        "namespace": self.NAMESPACE})
            return {"stored": True, "count": len(evs)}

    def events(self, session: str) -> List[dict]:
        with self._lock:
            self._expire()
            return [dict(e) for e in self._sessions.get(session, [])]


class _ProposeSourcesReq(BaseModel):
    project_id: str = "customer-ops"
    text: str = ""


class _ConfirmSourcesReq(BaseModel):
    project_id: str = "customer-ops"
    sources: List[Dict[str, Any]] = []
    confirmed_by: str = ""


def mount_agent_gateway(app: "FastAPI", prov: "ProjectionProvider") -> bool:
    """Expose a read-only, governed slice of this deployment to external agents over MCP + REST
    (the Governed Agent Gateway). Opt-in: does nothing unless ``AGENT_GATEWAY_TOKEN`` is set — the
    static bearer an external client presents (fine for a read-only surface over sample/scoped data;
    production wires the OAuth 2.1 + PKCE AuthorizationServer instead). Returns True if mounted.

    Only READ capabilities are wired (projects/missions/sources), each running the one governed path
    with PolicyEgressEngine on the way out, so PII/secrets are redacted/denied and every call audits.
    Writes, mission delegation and the sandbox are intentionally NOT exposed on this surface.
    """
    import os
    token = os.environ.get("AGENT_GATEWAY_TOKEN", "").strip()
    if not token:
        return False
    from fastapi import Body, Header, HTTPException
    from agentic_os.overlays import Principal
    from agentic_os.agent_gateway import (
        AgentGateway, GatewayPrincipal, InMemoryAuditSink, MCPEndpoint, McpGatewayBridge,
        PolicyEgressEngine, RestGatewayAdapter, build_read_registry)
    from agentic_os.agent_gateway.auth import GatewayAuthError

    default_pid = (prov.projects() or [{"id": "customer-ops"}])[0].get("id", "customer-ops")
    _GRANTS = {"projects.read", "missions.read", "sources.read"}

    class _Projections:
        def projects(self): return prov.projects()
        def overview(self, pid): return prov.overview(pid or default_pid)

    class _Missions:
        def list(self): return prov.missions(default_pid)
        def get(self, mid): return prov.mission_detail(default_pid, mid)
        def explain(self, mid): return prov.mission_detail(default_pid, mid)
        def pending_approvals(self): return prov.attention(default_pid)

    class _Sources:
        def search(self, query, limit=10): return prov.sources(default_pid)[: int(limit)]

    class _StaticVerifier:
        """Maps the configured bearer to a read-only principal; anything else → None (deny)."""
        def verify(self, t):
            if t and t == token:
                return GatewayPrincipal(Principal("external-agent", "service", (), "demo"),
                                        client_id="external-agent", workspace="demo",
                                        scopes=tuple(_GRANTS))
            return None

    registry = build_read_registry(projections=_Projections(), missions=_Missions(), sources=_Sources())
    gateway = AgentGateway(registry=registry, authorize=lambda p, perm: perm in _GRANTS,
                           egress=PolicyEgressEngine(), audit=InMemoryAuditSink())
    verifier = _StaticVerifier()
    rest = RestGatewayAdapter(gateway, verifier)
    endpoint = MCPEndpoint(McpGatewayBridge(gateway, verifier))

    @app.get("/api/agent-gateway/capabilities")
    def _agw_caps(authorization: str = Header(default="")):
        try:
            return rest.list_capabilities(authorization)
        except GatewayAuthError:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.post("/api/agent-gateway/capabilities/{name}/invoke")
    def _agw_invoke(name: str, body: Dict[str, Any] = Body(default={}),
                    authorization: str = Header(default="")):
        try:
            return rest.invoke(authorization, name, body or {})
        except GatewayAuthError:
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.post("/api/agent-gateway/mcp")
    def _agw_mcp(body: Dict[str, Any] = Body(default={}), authorization: str = Header(default="")):
        try:
            return endpoint.handle(authorization, str((body or {}).get("method", "")),
                                   (body or {}).get("params") or {})
        except GatewayAuthError:
            raise HTTPException(status_code=401, detail="unauthorized")

    return True


@dataclass
class RegisteredProducersProvider(SampleProjectionProvider):
    """A provider whose 'what needs me?' surface aggregates the producers registered through the app_kit
    boot registry (those with a live-state source — see :func:`register_live_producer_source`), selecting
    with the PROMOTED policy. Every other projection is inherited. When no registered producer has a live
    source it falls back to the sample surface WITH the ``basis: "sample"`` label — never silently."""

    registry: Any = None
    today_sample_cases: bool = False

    def priorities(self, project_id: str) -> dict:
        import time
        from agentic_os.priority_engine import (
            DecisionOpportunity, PriorityPolicy, collect_priorities, select_action)
        from agentic_os.agent_gateway.contracts import RiskTier
        live = registered_live_sources(self.registry)
        if not live:
            return super().priorities(project_id)           # basis "sample", honestly labelled
        learn_fn = self._learn_fn()

        def _wrap(fn):
            def _source():
                got = fn()
                if isinstance(got, DecisionOpportunity):
                    sel = select_action(got, utility_fn=learn_fn)
                    return [] if sel.action.risk_tier == RiskTier.READ else [sel.action]
                return list(got or [])
            return _source

        summary = collect_priorities([_wrap(fn) for _, _, fn in live], PriorityPolicy(attention_budget=5),
                                     now=time.time())
        out = summary.as_dict()
        out["basis"] = "registered"
        out["producers"] = [f"{a}/{p}" for a, p, _ in live]
        out["learning"] = self._learning_status(learn_fn)
        return out


def open_intervention_store_from_env():
    """The receipt store authoritative outcomes resolve against: a durable FileInterventionStore at
    ``$INTERVENTION_STORE_PATH`` (mirrors ``$OUTCOME_STORE_PATH``). Unset => None => authoritative writes
    stay fail-closed (404 unknown intervention)."""
    import os as _os
    p = _os.environ.get("INTERVENTION_STORE_PATH", "").strip()
    if not p:
        return None
    from agentic_os.intervention_record import FileInterventionStore
    return FileInterventionStore(p)


def select_provider(*, persist_path: Optional[str] = None) -> ProjectionProvider:
    """Choose the default provider explicitly. ``$PROJECTS_PROVIDER``: ``sample`` -> the sample provider;
    ``registered`` -> the registered-producers provider; unset/``auto`` -> registered when some registered
    producer has a live-state source, else the sample provider (whose surface is labelled ``basis: "sample"``)."""
    import os as _os
    choice = _os.environ.get("PROJECTS_PROVIDER", "").strip().lower()
    istore = open_intervention_store_from_env()
    if choice == "sample":
        return SampleProjectionProvider(persist_path=persist_path, intervention_store=istore)
    if choice not in ("", "auto", "registered"):
        raise ValueError(f"PROJECTS_PROVIDER must be 'sample', 'registered' or unset (got {choice!r})")
    if choice == "registered" or registered_live_sources():
        return RegisteredProducersProvider(persist_path=persist_path, intervention_store=istore)
    return SampleProjectionProvider(persist_path=persist_path, intervention_store=istore)


def create_app(provider: Optional[ProjectionProvider] = None, *, allow_origins: Optional[List[str]] = None) -> FastAPI:
    # a deployment sets $OUTCOME_STORE_PATH to persist the learning loop's outcomes across restarts
    import os as _os_env
    prov: ProjectionProvider = provider or select_provider(
        persist_path=(_os_env.environ.get("OUTCOME_STORE_PATH", "").strip() or None))
    from fastapi import Body
    from fastapi.responses import HTMLResponse
    app = FastAPI(title="ReDevOps Projects API", version="0.1.0")
    # Same-origin only by default (the bundled UI is served from this app, so it needs no CORS). Cross-origin
    # access is opt-in: `allow_origins=` or $PROJECTS_CORS_ORIGINS (comma-separated origins).
    origins = allow_origins or [o.strip() for o in _os_env.environ.get("PROJECTS_CORS_ORIGINS", "").split(",")
                                if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins,
            allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/projects")
    def _projects() -> List[dict]:
        return prov.projects()

    @app.get("/api/projects/{project_id}/overview")
    def _overview(project_id: str) -> dict:
        return prov.overview(project_id)

    @app.get("/api/projects/{project_id}/missions")
    def _missions(project_id: str) -> List[dict]:
        return prov.missions(project_id)

    @app.get("/api/projects/{project_id}/missions/{mission_id}")
    def _mission_detail(project_id: str, mission_id: str) -> dict:
        return prov.mission_detail(project_id, mission_id)

    @app.get("/api/projects/{project_id}/workflows")
    def _workflows(project_id: str) -> List[dict]:
        return prov.workflows(project_id)

    @app.get("/api/projects/{project_id}/attention")
    def _attention(project_id: str) -> List[dict]:
        return prov.attention(project_id)

    @app.get("/api/projects/{project_id}/priorities")
    def _priorities(project_id: str) -> dict:
        # The Priority Engine's cross-app 'what needs me?' surface. Optional on the provider — a
        # provider without it yields the honest empty state rather than an error.
        fn = getattr(prov, "priorities", None)
        if fn is None:
            return {"summary": "Nothing needs you right now.", "surfaced": [], "deferred": [],
                    "handled_automatically": 0, "abstained": 0, "basis": "unavailable"}
        return fn(project_id)

    # ── Today (outcome-first AttentionItem feed). Anonymous callers get a per-session SIMULATED sandbox; the
    # approve path is the governed external-action path (exact-payload binding, single use, verified send).
    def _today_session(request: Request, x_demo_session: Optional[str]) -> str:
        return f"{demo_client_key(request)}|{(x_demo_session or '').strip()[:32]}"

    def _today_call(name: str, *args, **kw):
        from agentic_os.today import TodayError
        fn = getattr(prov, name, None)
        if fn is None:
            raise HTTPException(501, {"error": "unsupported", "detail": "this deployment does not provide Today"})
        try:
            return fn(*args, **kw)
        except TodayError as e:
            raise HTTPException(e.status, e.as_dict())

    def _capture_call(name: str, *args, **kw):
        from agentic_os.capture import CaptureError
        fn = getattr(prov, name, None)
        if fn is None:
            raise HTTPException(501, {"error": "unsupported", "detail": "this deployment does not provide capture"})
        try:
            return fn(*args, **kw)
        except CaptureError as e:
            raise HTTPException(e.status, e.as_dict())
        except Exception as e:                      # a Today refusal while linking (e.g. case vanished)
            from agentic_os.today import TodayError
            if isinstance(e, TodayError):
                raise HTTPException(e.status, e.as_dict())
            raise

    @app.post("/api/projects/{project_id}/capture")
    def _capture(project_id: str, req: _CaptureReq, request: Request,
                 x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _capture_call("capture", project_id, session=_today_session(request, x_demo_session),
                             content=req.content, source=req.source, observed_at=req.observed_at,
                             attachments=req.attachments)

    @app.get("/api/projects/{project_id}/capture")
    def _capture_list(project_id: str, request: Request, status: Optional[str] = None,
                      x_demo_session: Optional[str] = Header(default=None)) -> list:
        return _capture_call("capture_list", project_id, session=_today_session(request, x_demo_session), status=status)

    @app.post("/api/projects/{project_id}/capture/{capture_id}/resolve")
    def _capture_resolve(project_id: str, capture_id: str, req: _CaptureResolveReq, request: Request,
                         x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _capture_call("capture_resolve", project_id, capture_id, session=_today_session(request, x_demo_session),
                             case_id=req.case_id)

    @app.post("/api/projects/{project_id}/capture/{capture_id}/archive")
    def _capture_archive(project_id: str, capture_id: str, request: Request,
                         x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _capture_call("capture_archive", project_id, capture_id, session=_today_session(request, x_demo_session))

    @app.get("/api/projects/{project_id}/today")
    def _today(project_id: str, request: Request, x_demo_session: Optional[str] = Header(default=None)) -> dict:
        if getattr(prov, "today", None) is None:
            return {"project_id": project_id, "generated_at": 0, "basis": "unavailable", "simulated_actions": False,
                    "headline": "Today isn't available on this deployment.", "counts": {}, "items": [],
                    "handled_automatically": 0, "abstained": 0}
        return _today_call("today", project_id, session=_today_session(request, x_demo_session))

    @app.get("/api/projects/{project_id}/today/notifications")
    def _today_notifications(project_id: str, request: Request, x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_notifications", project_id, session=_today_session(request, x_demo_session))

    @app.post("/api/projects/{project_id}/today/notifications/ack")
    def _today_ack(project_id: str, req: _TodayAckReq, request: Request,
                   x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_ack", project_id, session=_today_session(request, x_demo_session), keys=req.keys)

    @app.put("/api/projects/{project_id}/today/notification-prefs")
    def _today_prefs(project_id: str, req: _TodayPrefsReq, request: Request,
                     x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_prefs", project_id, session=_today_session(request, x_demo_session),
                           **req.model_dump())

    @app.post("/api/projects/{project_id}/today/batch")
    def _today_batch(project_id: str, req: _TodayBatchReq, request: Request,
                     x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_batch", project_id, session=_today_session(request, x_demo_session),
                           action=req.action, item_ids=req.item_ids, seconds=req.seconds,
                           assignee=req.assignee, category=req.category)

    @app.post("/api/projects/{project_id}/today/batch/undo")
    def _today_undo(project_id: str, req: _TodayUndoReq, request: Request,
                    x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_undo", project_id, session=_today_session(request, x_demo_session), token=req.token)

    @app.get("/api/projects/{project_id}/today/{item_id}")
    def _today_item(project_id: str, item_id: str, request: Request, targets: Optional[str] = None,
                    identity_id: Optional[str] = None, x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_item", project_id, item_id, session=_today_session(request, x_demo_session),
                           targets=[t for t in (targets or "").split(",") if t] or None, identity_id=identity_id)

    @app.post("/api/projects/{project_id}/today/{item_id}/approve")
    def _today_approve(project_id: str, item_id: str, req: _TodayApproveReq, request: Request,
                       x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_approve", project_id, item_id, session=_today_session(request, x_demo_session),
                           expected_digest=req.payload_digest, targets=req.targets, identity_id=req.identity_id)

    @app.post("/api/projects/{project_id}/today/{item_id}/reconcile")
    def _today_reconcile(project_id: str, item_id: str, request: Request, manual: Optional[str] = None,
                         x_demo_session: Optional[str] = Header(default=None),
                         authorization: Optional[str] = Header(default=None)) -> dict:
        """Settle an unconfirmed send from a read (provider lookup + independent check). ``manual`` is a person's
        own statement ("delivered" | "not_delivered"). Never sends anything."""
        by = "user:operator"
        if manual is not None:
            # A person's own statement that it was / was not delivered closes the question, so it is accepted only
            # from an authenticated, authorised approver when real sends are possible. A provider wired to a real
            # adapter advertises that with ``today_live``; the sample/simulated Today has nothing real to settle.
            from agentic_os.projects.approver_auth import approving_actor  # noqa: PLC0415
            live = bool(getattr(prov, "today_live", lambda pid: False)(project_id))
            by = approving_actor(authorization, live=live, claimed="user:operator", publishing=False)
        return _today_call("today_reconcile", project_id, item_id,
                           session=_today_session(request, x_demo_session), manual=manual, by=by)

    @app.post("/api/projects/{project_id}/today/{item_id}/snooze")
    def _today_snooze(project_id: str, item_id: str, req: _TodaySnoozeReq, request: Request,
                      x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_snooze", project_id, item_id, session=_today_session(request, x_demo_session),
                           seconds=req.seconds)

    @app.post("/api/projects/{project_id}/today/{item_id}/dismiss")
    def _today_dismiss(project_id: str, item_id: str, request: Request,
                       x_demo_session: Optional[str] = Header(default=None)) -> dict:
        return _today_call("today_dismiss", project_id, item_id, session=_today_session(request, x_demo_session))

    @app.post("/api/projects/{project_id}/today/{item_id}/simulate-outcome")
    def _today_outcome(project_id: str, item_id: str, request: Request,
                       x_demo_session: Optional[str] = Header(default=None)) -> dict:
        # SAMPLE ONLY: lets the demo show a business outcome closing an item. A real provider observes
        # outcomes from the systems of record and does not implement this method.
        return _today_call("today_simulate_outcome", project_id, item_id,
                           session=_today_session(request, x_demo_session))

    @app.get("/api/projects/{project_id}/deployment-inspection")
    def _deployment_inspection(project_id: str) -> dict:
        # Phase 9 card. Optional on the provider — absent means the honest "not connected" state, not an error.
        fn = getattr(prov, "deployment_inspection", None)
        if fn is None:
            return {"mission": "Deployment Inspection", "target": "", "connected": False,
                    "boundary": {"inspection": "unavailable", "remediation": "unavailable"}, "kpis": [],
                    "findings": [], "findings_by_severity": {}, "finding_count": 0,
                    "proposed_actions": [], "governed_action": None}
        return fn(project_id)

    @app.get("/api/projects/{project_id}/social-intelligence")
    def _social_intelligence(project_id: str) -> dict:
        fn = getattr(prov, "social_intelligence", None)
        if fn is None:
            return {"mission": "Social Intelligence", "sources": [], "observations": 0,
                    "opportunities": {"total": 0, "problem_signals": 0, "solution_seeking": 0,
                                      "commercial_intent_evidence": 0, "unknown_commercial_intent": 0},
                    "market_signals": 0, "proposed_actions": [], "opportunity_cards": [],
                    "data_source": "unavailable"}
        return fn(project_id)

    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)
    demo_outcomes = DemoOutcomeSandbox()
    app.state.demo_outcomes = demo_outcomes

    def _outcome_tenant(authorization: Optional[str], requested: Optional[str]) -> Optional[str]:
        """THE single tenant resolver for outcome + promotion routes. Today the tenant is taken from the
        request body (the control token is global, so it is a CLAIM, enforced only against the receipt's
        tenant). Swap this one function for a tenant-bound-token lookup to make it caller-derived."""
        return norm_tenant(requested)

    def _check_body_size(request: Request) -> None:
        cl = request.headers.get("content-length", "")
        if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            raise HTTPException(413, f"request body exceeds {MAX_BODY_BYTES} bytes")

    @app.post("/api/outcomes")
    def _record_outcome(req: _OutcomeReq, request: Request,
                        authorization: Optional[str] = Header(default=None),
                        x_demo_session: Optional[str] = Header(default=None)) -> dict:
        # Two lanes. AUTHORITATIVE (``Authorization`` header or ``authoritative: true``): bearer token
        # (503 unset / 401 wrong) + executed-intervention receipt + idempotency key; the only HTTP path into
        # the learning pipeline. Everything else is an anonymous DEMO write: sandboxed, labelled
        # demo/simulated, in memory, bounded; it never reaches OutcomeStore or the promotion cycle.
        _check_body_size(request)
        fn = getattr(prov, "record_outcome_event", None)
        if fn is None:
            return {"recorded": 0, "supported": False}
        if authorization is not None or req.authoritative:
            from agentic_os.mission.control_auth import require_control_token
            require_control_token(authorization)
            rec_fn = getattr(prov, "record_authoritative_outcome", None)
            if rec_fn is None:
                return {"recorded": 0, "supported": False}
            try:
                out = rec_fn(
                    intervention_id=req.intervention_id or req.decision_ref,
                    idempotency_key=req.idempotency_key, attribution_confidence=req.attribution_confidence,
                    observed_reward=req.observed_reward, reward_dimensions=req.reward_dimensions,
                    delay=req.delay, tenant_id=_outcome_tenant(authorization, req.tenant_id),
                    execution_ref=req.execution_ref,
                    mission_id=req.mission_id, source_app=req.source_app, action_kind=req.action_kind)
            except OutcomeRejected as e:
                raise HTTPException(e.status, e.detail)
            return {**out, "supported": True, "demo": False, "simulated": False, "authoritative": True}
        try:
            validate_outcome_numbers(observed_reward=req.observed_reward, reward_dimensions=req.reward_dimensions,
                                     delay=req.delay, attribution_confidence=req.attribution_confidence)
        except OutcomeRejected as e:
            raise HTTPException(e.status, e.detail)
        conf = 0.5 if req.attribution_confidence is None else req.attribution_confidence
        ip = demo_client_key(request)      # primary key; X-Demo-Session is only a sub-key
        sub = (x_demo_session or "").strip()[:32]
        res = demo_outcomes.add(f"{ip}|{sub}", group=ip, record={
            "source_app": req.source_app, "action_kind": req.action_kind, "candidate_id": req.candidate_id,
            "observed_reward": req.observed_reward, "reward_dimensions": dict(req.reward_dimensions),
            "attribution_confidence": conf, "delay": req.delay})
        return {"supported": True, "demo": True, "simulated": True, "namespace": "demo", "authoritative": False,
                "durable": False, "recorded": res.get("count", 0), "stored": res["stored"],
                "reason": res.get("reason", ""),
                "note": "anonymous demo event: sandboxed per session, never used as learning evidence"}

    @app.post("/api/projects/{project_id}/policy/promotion-cycle")
    def _promotion_cycle(project_id: str, body: Dict[str, Any] = Body(default={}),
                         authorization: Optional[str] = Header(default=None)) -> dict:
        # Evaluate the candidate policy against the held-out outcomes. DRY RUN by default; the active
        # policy changes only when the caller asks apply=true AND the deployment allows it
        # ($PROJECTS_ALLOW_POLICY_APPLY) AND the gate passes.
        fn = getattr(prov, "run_promotion_cycle", None)
        if fn is None:
            return {"supported": False}
        import os as _os
        allowed = _os.environ.get("PROJECTS_ALLOW_POLICY_APPLY", "").lower() in ("1", "true", "yes")
        want = bool((body or {}).get("apply", False))
        tenant = _outcome_tenant(authorization, (body or {}).get("tenant_id"))
        if want or tenant is not None:
            # applying mutates policy state; a tenant-scoped read exposes that tenant's evidence
            from agentic_os.mission.control_auth import require_control_token
            require_control_token(authorization)
        rep = fn(apply=bool(want and allowed), tenant_id=tenant)
        return {"supported": True, "promote": rep.decision.promote, "reasons": list(rep.decision.reasons),
                "promoted": rep.promoted, "apply_requested": want, "apply_allowed": allowed,
                "active_version": rep.active.version, "candidate_version": rep.candidate.version,
                "n_test": rep.result.n_test, "relative_improvement": rep.result.relative_improvement,
                "baseline": rep.result.baseline}

    @app.get("/api/projects/{project_id}/discovery")
    def _discovery(project_id: str) -> List[dict]:
        return prov.discovery(project_id)

    @app.get("/api/projects/{project_id}/apps")
    def _apps(project_id: str) -> List[dict]:
        # Prefer the live Integration Plane guides when the connector plugin is installed,
        # overlaying the provider's live connection set; else the sample provider (also stateful).
        guided = apps_from_setup_guides(getattr(prov, "connected", None))
        apps = guided if guided is not None else prov.apps(project_id)
        # Offer only the deployment's enabled apps ($PROJECTS_APPS); unset = all. Applies to the
        # live-guide path too (prov.apps already filters, so the sample path stays idempotent).
        return enabled_apps(apps, enabled_selection())

    @app.post("/api/apps/{provider}/connect")
    def _connect_app(provider: str) -> dict:
        # Simulated connect (no hosted OAuth app configured) — flips readiness for the demo. Once hosted OAuth is
        # configured (or was meant to be), the connected flag may only be set by the real flow.
        if _get_hosted() is not None or _HOSTED_BROKEN:
            raise HTTPException(403, "use the hosted connect flow")
        return prov.connect_app(provider)

    @app.post("/api/apps/{provider}/connect/start")
    def _connect_start(provider: str, authorization: Optional[str] = Header(default=None)) -> dict:
        """Begin hosted OAuth: return the provider consent URL the browser should open. Falls
        back to the simulated connect when no ReDevOps OAuth app is registered for the provider."""
        hosted = _get_hosted()
        if hosted is None and _HOSTED_BROKEN:
            # credentials are configured: never degrade to the simulated flag
            raise HTTPException(503, "hosted OAuth is configured but unavailable")
        if hosted is not None and hosted.available(provider):
            from agentic_os.mission.control_auth import require_control_token
            require_control_token(authorization)
            try:
                import hashlib as _hl
                started = hosted.start(provider, principal=_hl.sha256((authorization or "").encode()).hexdigest())
            except RuntimeError as e:
                raise HTTPException(429, str(e))
            return {"hosted": True, "authorize_url": started["authorize_url"], "state": started["state"]}
        if hosted is not None:
            raise HTTPException(403, "use the hosted connect flow")
        return {"hosted": False, "authorize_url": None, "connected": prov.connect_app(provider)}

    @app.get("/api/apps/connect/callback", response_class=HTMLResponse)
    def _connect_callback(code: str = "", state: str = ""):
        """The hosted redirect target: exchange the code, store the token, verify, and reflect
        the connection in the projections. Returns a small page the user closes."""
        hosted = _get_hosted()
        if hosted is None:
            return HTMLResponse("<p>Hosted OAuth is not configured on this deployment.</p>", status_code=400)
        outcome = hosted.callback(code, state)
        if outcome.connected:
            prov.connect_app(outcome.provider)  # reflect in /apps + template readiness
            body = (f"<h2>&#10003; Connected {_html.escape(str(outcome.provider))}</h2>"
                    f"<p>{_html.escape(str(outcome.state))} · you can close this tab and return to Projects.</p>")
            return HTMLResponse(_callback_page(body))
        return HTMLResponse(_callback_page(f"<h2>Couldn't connect</h2><p>{_html.escape(str(outcome.detail))}</p>"), status_code=400)

    @app.get("/api/projects/{project_id}/sources")
    def _sources(project_id: str) -> List[dict]:
        return prov.sources(project_id)

    @app.get("/api/projects/{project_id}/runtime")
    def _runtime(project_id: str) -> dict:
        return prov.runtime(project_id)

    @app.get("/api/projects/{project_id}/templates")
    def _templates(project_id: str) -> List[dict]:
        return prov.templates(project_id)

    @app.get("/api/projects/{project_id}/activity")
    def _activity(project_id: str) -> List[dict]:
        return prov.activity(project_id)

    @app.post("/api/sidekick")
    def _sidekick(req: _SidekickReq) -> dict:
        return sidekick_reply(req.ctx, req.text, prov)

    @app.get("/api/sidekick/help")
    def _sidekick_help() -> List[dict]:
        # The canonical stack questions Sidekick can answer authoritatively — for a "what can I
        # ask" surface (how creds are handled, is my data local/cloud, what can a connected app see…).
        from agentic_os.stack_knowledge import help_questions
        return list(help_questions())

    @app.post("/api/sources/propose")
    def _propose(req: _ProposeSourcesReq) -> dict:
        return propose_sources(req.project_id, req.text).to_dict()

    @app.post("/api/sources/confirm")
    def _confirm(req: _ConfirmSourcesReq, authorization: Optional[str] = Header(default=None)) -> List[dict]:
        # A ``files`` source makes THIS server walk a client-chosen path, so an anonymous caller may only
        # confirm folders under an operator-allowed root ($PROJECTS_SOURCE_ROOTS); anything else needs the
        # control token (fail closed: 503 when unset, 401 when wrong).
        specs = authorize_source_locations(req.sources, authorization)
        try:
            return confirm_and_connect_sources(req.project_id, specs, req.confirmed_by)
        except ValueError as e:                       # unknown kind / access mode / embedded NUL: the caller's input
            raise HTTPException(422, str(e))

    # Governed Agent Gateway — let an EXTERNAL agent (Claude/ChatGPT/Cursor) reach a read-only,
    # governed slice of this deployment over MCP + REST. Opt-in: mounted only when
    # AGENT_GATEWAY_TOKEN is set. Every call runs the one governed path (permissions → egress →
    # audit); writes/mission-delegation are NOT exposed here (read-only surface).
    mount_agent_gateway(app, prov)

    # Serve the bundled Projects UI at the SAME origin as the API (no CORS) — mounted LAST so
    # the /api routes above always win. The UI is built with VITE_PROJECTS_API="/", so it calls
    # this service's /api directly. `pip install 'agentic-os[projects]'` ships the bundle.
    ui = _ui_dir()
    if ui is not None:
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=str(ui), html=True), name="projects-ui")

    return app


def _ui_dir() -> Optional["Path"]:
    """The built Projects UI directory: ``$PROJECTS_UI_DIR`` if set, else the bundle vendored at
    ``agentic_os/projects_ui/``. Returns None when no build is present (API-only)."""
    import os
    from pathlib import Path
    candidates = []
    env = os.environ.get("PROJECTS_UI_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parent / "projects_ui")
    for c in candidates:
        if (c / "index.html").is_file():
            return c
    return None


def mounted_app(base_path: str = "", provider: Optional[ProjectionProvider] = None, **kw) -> FastAPI:
    """Build the app, optionally under a base path (e.g. ``/projects``) so it can be fronted at
    a URL sub-path — cloudflared routes ``demo.redevops.io/projects`` → this service without
    stripping, so the app serves ``/projects/api`` + ``/projects/`` (the UI must be built with a
    matching ``base``). Without ``base_path`` the app serves at the origin root."""
    inner = create_app(provider, **kw)
    base = (base_path or "").rstrip("/")
    if not base:
        return inner
    outer = FastAPI(title="ReDevOps Projects", version="0.1.0")
    outer.mount(base, inner)   # Starlette strips the prefix before the inner app sees it
    return outer


def main() -> None:
    """Console entrypoint (``agentic-os-projects``): serve the Projects UI + API on one origin,
    optionally under ``$PROJECTS_BASE_PATH``."""
    import os
    import uvicorn
    from agentic_os.secrets.bootstrap import ensure_secrets
    ensure_secrets()  # non-fatal; generates missing platform secrets once (docs/SECRETS.md)
    host = os.environ.get("PROJECTS_HOST", "127.0.0.1")
    port = int(os.environ.get("PROJECTS_PORT", "8787"))
    base = os.environ.get("PROJECTS_BASE_PATH", "")
    served = "UI + API" if _ui_dir() is not None else "API only (no UI bundle found)"
    print(f"ReDevOps Projects — {served} on http://{host}:{port}{base or ''}")
    uvicorn.run(mounted_app(base), host=host, port=port)


# Module-level app for `uvicorn agentic_os.projects_api:app` (honours $PROJECTS_BASE_PATH).
import os as _os
app = mounted_app(_os.environ.get("PROJECTS_BASE_PATH", ""))
