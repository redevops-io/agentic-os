"""Typed Sidekick tools for the integrated products (domains ``security`` and ``quality``).

Mechanism: each tool is a ``SidekickCapability`` descriptor (the repo's existing contract) with a bound handler,
registered in a registry of its own (``build_registry``) so the audited default registry is untouched, plus a JSON
Schema in ``schemas.INPUT_SCHEMAS`` validated before any HTTP call (``additionalProperties: false``: no path, URL,
command, plan body or credential can ride a tool). Selection stays deterministic (``CapabilityRegistry.discover``).

Authority classes (also enforced by the product on its side):

* ``read``: the service token reads; no approval.
* ``stage`` (propose a ban/unban): creates a PROPOSAL that does nothing; the result is the approval card and record ids.
* ``present``: returns the approval card of an existing proposal.
* ``bounded_write`` (QA start/cancel, gated): never takes a path, URL, command or plan.
* DECIDE and EXECUTE are NEVER tools (``schemas.FORBIDDEN_TOOLS``; ``build_registry`` refuses to register them): the
  person decides in the product UI or by clicking in Projects (``approvals.py``).

A tool result names the product's own record ids so the standalone UI and Projects show the same records, never carries
``executed: true`` for a consequential action, and is an explicit ``unavailable`` result (never an empty success, never
a fabricated row) when the product is not configured, not reachable or gated.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from agentic_os.sidekick.capability import CapabilityDomain as D, CapabilityRegistry, Maturity as M, SidekickCapability
from agentic_os.sidekick.contracts import ArtifactLink
from agentic_os.sidekick.orchestrator import CapabilityResult

from . import qa as qamod
from . import sentinel as sn
from .client import ProductError
from .hub import ProductHub
from .principal import current_actor_label
from .schemas import FORBIDDEN_TOOLS, INPUT_SCHEMAS, TOOL_CLASS, SchemaError, validate

_TOOLS: Tuple[Tuple[str, str, str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]], ...] = (
    # id, domain, summary, intents, required, optional, candidate_actions
    ("security.sentinel_posture", "security", "Edge Sentinel security posture: active bans, alerts by severity, CrowdSec connection and mode.",
     ("security status", "security posture", "edge sentinel posture", "how is security", "threat status", "crowdsec status"),
     (), (), ()),
    ("security.sentinel_list_cases", "security", "List Edge Sentinel security cases, filtered by status or severity.",
     ("list security cases", "security cases", "open cases", "show cases", "what alerts became cases"),
     (), ("status", "severity", "q", "limit", "cursor"), ()),
    ("security.sentinel_get_case", "security", "Read one Edge Sentinel case with its evidence, findings, actions, decisions and receipts.",
     ("show case", "case details", "get case", "open case"), ("case_id",), (), ()),
    ("security.sentinel_list_actions", "security", "List Edge Sentinel proposed actions: the approval queue and execution history.",
     ("proposed actions", "approval queue", "pending approvals security", "list actions", "ban proposals"),
     (), ("state", "limit", "cursor"), ()),
    ("security.sentinel_propose_ban", "security", "Propose an IP ban in Edge Sentinel. Creates a proposal awaiting a person's approval; nothing is blocked.",
     ("propose ban", "ban ip", "block ip", "propose a ban", "block address"), ("ip", "reason"),
     ("duration_hours",), ("request_ban_approval",)),
    ("security.sentinel_propose_unban", "security", "Propose lifting an IP ban in Edge Sentinel. Creates a proposal awaiting a person's approval.",
     ("propose unban", "unban ip", "lift ban", "unblock ip", "remove ban"), ("ip", "reason"), (),
     ("request_unban_approval",)),
    ("security.sentinel_request_decision", "security", "Show the approval card of an Edge Sentinel proposal. Makes no decision.",
     ("approval card", "request decision", "show approval", "what needs approval security"), ("request_id",), (), ()),
    ("security.sentinel_read_back", "security", "Read the persisted post-action verification (read-back) of an Edge Sentinel action.",
     ("read back", "verify ban", "was the ban verified", "verification", "read-back"), ("request_id",), (), ()),
    ("quality.qa_info", "quality", "redevops-qa version, platform and which test profiles are available or explicitly unavailable.",
     ("qa info", "quality status", "test platform status", "which tests can run"), (), (), ()),
    ("quality.qa_list_targets", "quality", "List the operator-registered redevops-qa targets.",
     ("list targets", "qa targets", "test targets"), (), (), ()),
    ("quality.qa_list_profiles", "quality", "List redevops-qa test profiles with availability.",
     ("list profiles", "test profiles", "qa profiles"), (), ("target_id",), ()),
    ("quality.qa_estimate", "quality", "Estimate a redevops-qa run (no outbound connection, no evidence bundle).",
     ("estimate run", "qa estimate", "estimate test run"), ("target_id", "profile"), (), ()),
    ("quality.qa_start_run", "quality", "Start a redevops-qa run for a target and profile; a run that needs approval waits for a person.",
     ("start run", "run tests", "start qa run", "run test profile"), ("target_id", "profile"),
     ("estimate_digest", "defect"), ("request_run_approval",)),
    ("quality.qa_list_runs", "quality", "Recent redevops-qa runs with their state and outcome, newest first; filter by state to find what failed.",
     ("list runs", "recent runs", "failed runs", "which runs failed", "qa history"), (), ("state", "limit"), ()),
    ("quality.qa_get_run", "quality", "Progress, state and outcome of a redevops-qa run.",
     ("run progress", "how is the run going", "qa run status", "get run"), ("run_id",), (), ()),
    ("quality.qa_run_events", "quality", "Progress events of a redevops-qa run.",
     ("run events", "qa run events"), ("run_id",), ("after", "limit"), ()),
    ("quality.qa_cancel_run", "quality", "Cancel a running redevops-qa run.",
     ("cancel run", "stop qa run"), ("run_id",), (), ()),
    ("quality.qa_get_evidence", "quality", "Evidence manifest and level of a finished redevops-qa run.",
     ("qa evidence", "run evidence", "evidence bundle"), ("run_id",), (), ()),
    ("quality.qa_verify", "quality", "Independent re-verification of a redevops-qa evidence bundle.",
     ("verify run", "qa verify", "reverify evidence"), ("run_id",), (), ()),
    ("quality.ui_audit", "quality", "Audit a registered target's UI: an accessibility/usability audit run through the QA service, with full evidence. Starts a normal QA run; a run that needs approval waits for a person.",
     ("ui audit", "audit ui", "accessibility audit", "check the ui", "audit the app ui", "is the ui accessible"),
     ("target_id",), ("profile_id",), ("request_run_approval",)),
    ("quality.plan_generate", "quality", "Generate a frozen test plan for a registered target. The plan body is composed by the QA service from its own allow-listed builders, never by the caller.",
     ("generate test plan", "plan tests", "make a test plan", "regression plan", "plan a follow-up"),
     ("target_id", "scope"), ("source_run_id",), ()),
    ("security.deployment_scan", "security", "Read-only inspection of a deployment's own state: GETs only the endpoints the product already serves to readers and derives findings from the live answers.",
     ("deployment scan", "check deployment", "scan security", "inspect deployment", "security check", "is the deployment healthy"),
     ("product_id",), (), ()),
    ("findings.consolidate", "findings", "One report of UI, functional and security findings, folded from the products' own records: every row cites a product record id and an evidence link.",
     ("consolidate findings", "combined report", "all findings", "one report", "summary of findings", "what did we find"),
     (), ("product_ids", "run_ids", "case_ids", "severity_min"), ()),
)


# ── result helpers ────────────────────────────────────────────────────────────────────────────────
def unavailable_result(cap_id: str, product: str, u: Mapping[str, str]) -> CapabilityResult:
    reason = u.get("reason") or "unavailable"
    remedy = u.get("remedy") or ""
    ans = f"{product} is unavailable: {reason}." + (f" {remedy}." if remedy else "")
    return CapabilityResult(capability_id=cap_id, answer=ans, confidence=0.0,
                            data={"status": "unavailable", "product": product, "executed": False,
                                  "code": u.get("code", "unavailable"), "reason": reason, "remedy": remedy})


def error_result(cap_id: str, product: str, e: ProductError) -> CapabilityResult:
    if e.unavailable:
        return unavailable_result(cap_id, product, {"code": e.code, "reason": e.detail or e.reason or e.code,
                                                    "remedy": e.remedy})
    msg = e.reason or e.detail or e.code
    return CapabilityResult(capability_id=cap_id, answer=f"{product} refused: {msg}.", confidence=0.0,
                            data={"status": "refused", "product": product, "executed": False, **e.as_dict()})


def _invalid_result(cap_id: str, product: str, err: SchemaError) -> CapabilityResult:
    return CapabilityResult(capability_id=cap_id, confidence=0.0,
                            answer="That request is not valid for this tool: " + "; ".join(err.problems[:4]) + ".",
                            data={"status": "invalid", "product": product, "executed": False, "problems": err.problems})


def _link(spec, rtype: str, rid: str, path: str) -> ArtifactLink:
    return ArtifactLink(provider=spec.id, resource_type=rtype, resource_id=rid, native_url=spec.ui_link(path))


def _fmt_counts(d: Any) -> str:
    if isinstance(d, Mapping) and d:
        return ", ".join(f"{k} {v}" for k, v in sorted(d.items()))
    return ""


# ── severity vocabulary (SKILLS_DESIGN.md §2/§5) ─────────────────────────────────────────────────
# The consolidated report's vocabulary is `note < minor < major < blocker`. Products keep their own vocabularies;
# every mapping INTO this one is a table pinned by a test, never an inference. An unmapped severity never
# becomes a row — inventing a severity for a record is exactly the fabricated row the design forbids.
SEVERITY_RANK = {"note": 0, "minor": 1, "major": 2, "blocker": 3}
CASE_SEVERITY = {"critical": "blocker", "high": "major", "medium": "minor", "low": "note", "info": "note"}
DEPLOY_SEVERITY = {"critical": "blocker", "high": "major", "medium": "minor", "low": "note"}
AUDIT_RULE_SEVERITY = {"a11y-blocker": "blocker", "a11y": "major", "usability-blocking": "blocker",
                       "usability": "minor", "note": "note"}
# The profile the audit runs on; overridable only to another id the SERVICE advertises (schemas close the shape).
AUDIT_PROFILE_DEFAULT = "ui-audit"


# ── the tools ─────────────────────────────────────────────────────────────────────────────────────
class ProductTools:
    """Handlers for every tool, bound to one hub. ``call`` is the single entry (validate, gate, run, wrap)."""

    def __init__(self, hub: ProductHub) -> None:
        self.hub = hub

    def product_for(self, cap_id: str) -> Optional[str]:
        kind = cap_id.split(".", 1)[0]
        if kind == "findings":
            # Consolidation reads the products; it owns none. Anchor on a ready quality product, else a ready
            # security product, else the first registered one (call() then says explicitly why it is unavailable).
            owned = [s for s in self.hub.specs() if s.kind in ("quality", "security")]
            for pref in ("quality", "security"):
                for s in owned:
                    if s.kind == pref and self.hub.ready(s.id):
                        return s.id
            return owned[0].id if owned else None
        for s in self.hub.specs():
            if s.kind == kind and (not s.capabilities or cap_id in s.capabilities):
                return s.id
        return None

    def call(self, cap_id: str, inputs: Mapping[str, Any], actor: str = "") -> CapabilityResult:
        if cap_id in FORBIDDEN_TOOLS or cap_id not in INPUT_SCHEMAS:
            return CapabilityResult(capability_id=cap_id, confidence=0.0, answer="That is not a tool.",
                                    data={"status": "refused", "executed": False})
        pid = self.product_for(cap_id)
        if pid is None:
            return unavailable_result(cap_id, cap_id.split(".")[0], {
                "code": "not_registered", "reason": "no product of this kind is registered",
                "remedy": "add the product to the AGENTIC_OS_PRODUCTS registry"})
        spec = self.hub.spec(pid)
        if not self.hub.ready(pid):          # say why first (gate, no token, down); the status is cached, not a per-call probe
            return unavailable_result(cap_id, spec.display_name, self.hub.unavailable_reason(pid))
        try:
            validate(dict(inputs), INPUT_SCHEMAS[cap_id])
        except SchemaError as e:
            return _invalid_result(cap_id, spec.display_name, e)
        who = actor or current_actor_label(self.hub.registry.single_owner)
        try:
            return getattr(self, "_" + cap_id.split(".", 1)[1])(spec, dict(inputs), who)
        except ProductError as e:
            self.hub.invalidate(pid) if e.unavailable else None
            return error_result(cap_id, spec.display_name, e)

    # security ---------------------------------------------------------------------------------
    def _sentinel_posture(self, spec, inp, who):
        p = self.hub.sentinel(spec.id).posture(actor=who, source="sidekick")
        bans = p.get("decisions_active", p.get("active_bans"))
        alerts = p.get("alerts_total", p.get("alerts_24h", p.get("alerts")))
        parts = []
        if bans is not None:
            parts.append(f"{bans} active ban decision(s)")
        if alerts is not None:
            parts.append(f"{alerts} alert(s) held by CrowdSec")
        sev = _fmt_counts(p.get("by_severity"))
        if sev:
            parts.append(f"by severity: {sev}")
        mode = p.get("mode") if isinstance(p.get("mode"), Mapping) else \
            ((self.hub.status(spec.id).get("info") or {}).get("mode") or {})
        tail = []
        if p.get("crowdsec_connected") is False or p.get("connected") is False:
            tail.append("CrowdSec is not connected")
        if mode.get("demo"):
            tail.append("demo mode: synthetic data, side effects refused")
        elif mode.get("dry_run"):
            tail.append("dry run: blocking is not enabled")
        ans = f"{spec.display_name} posture: " + ("; ".join(parts) or "no data reported") + \
              (". " + "; ".join(tail) if tail else "") + "."
        return CapabilityResult(capability_id="security.sentinel_posture", answer=ans, data={
            "product": spec.id, "posture": p, "executed": False}, artifacts=(_link(spec, "overview", "posture", "/ui/"),))

    def _sentinel_list_cases(self, spec, inp, who):
        r = self.hub.sentinel(spec.id).cases(status=inp.get("status", ""), severity=inp.get("severity", ""),
                                             q=inp.get("q", ""), limit=inp.get("limit", 10), cursor=inp.get("cursor", ""),
                                             actor=who, source="sidekick")
        items = r.get("items", []) if isinstance(r, Mapping) else []
        lines = [sn.case_line(c) for c in items[:10]]
        ans = (f"{len(items)} case(s): " + "; ".join(lines) + ".") if items else "No cases match."
        return CapabilityResult(capability_id="security.sentinel_list_cases", answer=ans, data={
            "product": spec.id, "items": items, "next_cursor": r.get("next_cursor") if isinstance(r, Mapping) else None,
            "executed": False}, evidence=tuple(str(c.get("case_id")) for c in items if c.get("case_id")),
            artifacts=tuple(_link(spec, "case", str(c["case_id"]), f"/ui/cases/{c['case_id']}") for c in items[:10] if c.get("case_id")))

    def _sentinel_get_case(self, spec, inp, who):
        c = self.hub.sentinel(spec.id).case(inp["case_id"], actor=who, source="sidekick")
        acts = c.get("actions_detail") if isinstance(c.get("actions_detail"), list) else \
            (c.get("actions") if isinstance(c.get("actions"), list) else [])
        summ = c.get("summary") if isinstance(c.get("summary"), Mapping) else c
        ans = f"Case {inp['case_id']}: {summ.get('title') or summ.get('case_type') or ''} [{summ.get('severity', '?')}/{summ.get('status', '?')}], " \
              f"{len(acts)} action(s)" + (": " + "; ".join(f"{a.get('request_id')} {a.get('state')}" for a in acts[:5]) if acts else "") + "."
        return CapabilityResult(capability_id="security.sentinel_get_case", answer=ans, data={
            "product": spec.id, "case": c, "executed": False}, evidence=(inp["case_id"],),
            artifacts=(_link(spec, "case", inp["case_id"], f"/ui/cases/{inp['case_id']}"),))

    def _sentinel_list_actions(self, spec, inp, who):
        r = self.hub.sentinel(spec.id).actions(state=inp.get("state", ""), limit=inp.get("limit", 20),
                                               cursor=inp.get("cursor", ""), actor=who, source="sidekick")
        items = r.get("items", []) if isinstance(r, Mapping) else []
        lines = [f"{a.get('request_id')} {a.get('state')} {sn.action_title(a).replace('Approve ', '')}" for a in items[:10]]
        ans = (f"{len(items)} action(s): " + "; ".join(lines) + ".") if items else "No actions match."
        return CapabilityResult(capability_id="security.sentinel_list_actions", answer=ans, data={
            "product": spec.id, "items": items, "next_cursor": r.get("next_cursor") if isinstance(r, Mapping) else None,
            "executed": False}, evidence=tuple(str(a.get("request_id")) for a in items if a.get("request_id")),
            artifacts=tuple(_link(spec, "action", str(a["request_id"]), f"/ui/actions/{a['request_id']}") for a in items[:10] if a.get("request_id")))

    def _propose(self, spec, inp, who, capability: str, cap_id: str):
        r = self.hub.sentinel(spec.id).propose(capability=capability, ip=inp["ip"], reason=inp["reason"],
                                               duration_hours=inp.get("duration_hours"), actor=who, source="sidekick")
        rid = str(r.get("request_id") or "")
        a = self.hub.with_mode(spec.id, self.hub.sentinel(spec.id).action(rid, actor=who, source="sidekick")) if rid else r
        card = sn.approval_card(spec, a) if rid else None
        self.hub.invalidate(spec.id)
        ans = (f"Proposed {'lifting the ban of' if capability == sn.CAP_UNBAN else 'a ban of'} {inp['ip']} "
               f"(proposal {rid}, case {r.get('case_id', a.get('case_id', ''))}). It is awaiting a person's approval; nothing has been "
               f"{'lifted' if capability == sn.CAP_UNBAN else 'blocked'}. Approve it in Projects or open it in {spec.display_name}.")
        return CapabilityResult(capability_id=cap_id, answer=ans, data={
            "product": spec.id, "case_id": r.get("case_id") or a.get("case_id"), "request_id": rid,
            "state": str(r.get("state") or a.get("state") or "AWAITING_APPROVAL"), "approval": card, "executed": False},
            evidence=tuple(x for x in (rid, str(r.get("case_id") or "")) if x),
            candidate_actions=({"action": "request_approval", "request_id": rid},) if rid else (),
            artifacts=(_link(spec, "action", rid, f"/ui/actions/{rid}"),) if rid else ())

    def _sentinel_propose_ban(self, spec, inp, who):
        return self._propose(spec, inp, who, sn.CAP_BAN, "security.sentinel_propose_ban")

    def _sentinel_propose_unban(self, spec, inp, who):
        return self._propose(spec, inp, who, sn.CAP_UNBAN, "security.sentinel_propose_unban")

    def _sentinel_request_decision(self, spec, inp, who):
        a = self.hub.sentinel(spec.id).action(inp["request_id"], actor=who, source="sidekick")
        card = sn.approval_card(spec, a)
        st = str(a.get("state"))
        ans = (f"{card['title']} ({inp['request_id']}) is {st.replace('_', ' ').lower()}. {card['consequence']} "
               "A person decides: approve it in Projects or in " + spec.display_name + ".")
        return CapabilityResult(capability_id="security.sentinel_request_decision", answer=ans, data={
            "product": spec.id, "request_id": inp["request_id"], "case_id": a.get("case_id"), "state": st,
            "approval": card, "executed": False}, evidence=(inp["request_id"],),
            artifacts=(_link(spec, "action", inp["request_id"], f"/ui/actions/{inp['request_id']}"),))

    def _sentinel_read_back(self, spec, inp, who):
        v = self.hub.sentinel(spec.id).verification(inp["request_id"], actor=who, source="sidekick")
        state = str(v.get("state") or "not_run")
        text = {"verified": "Verified: the product read the edge back and the decision is present.",
                "unverified": "Unverified: the request was accepted but the read-back did not observe it.",
                "dry_run": "Dry run: nothing was blocked, so there is nothing to verify at the edge.",
                "not_run": "No read-back has run yet (the action has not executed)."}.get(state, state)
        return CapabilityResult(capability_id="security.sentinel_read_back", answer=f"{inp['request_id']}: {text}"
                                + (f" {v.get('detail')}" if v.get("detail") else ""), data={
            "product": spec.id, "request_id": inp["request_id"], "state": state, "detail": v.get("detail", ""),
            "at": v.get("at", ""), "receipt_id": v.get("receipt_id", ""), "executed": False}, evidence=(inp["request_id"],),
            artifacts=(_link(spec, "action", inp["request_id"], f"/ui/actions/{inp['request_id']}"),))

    # quality ----------------------------------------------------------------------------------
    def _qa_info(self, spec, inp, who):
        i = self.hub.qa(spec.id).info(actor=who)
        profs = i.get("profiles") if isinstance(i.get("profiles"), list) else []
        un = [p for p in profs if isinstance(p, Mapping) and not p.get("available", True)]
        ans = f"redevops-qa {i.get('version', '')}: {len(profs) - len(un)} of {len(profs)} profile(s) available" + \
              ("; unavailable: " + ", ".join(f"{p.get('id')} ({(p.get('unavailable') or {}).get('code', 'unavailable')})" for p in un) if un else "") + "."
        return CapabilityResult(capability_id="quality.qa_info", answer=ans, data={"product": spec.id, "info": i, "executed": False})

    def _qa_list_targets(self, spec, inp, who):
        r = self.hub.qa(spec.id).targets(actor=who, source="sidekick")
        items = r.get("items", r.get("targets", [])) if isinstance(r, Mapping) else []
        return CapabilityResult(capability_id="quality.qa_list_targets", answer=(f"{len(items)} target(s): " + "; ".join(
            f"{t.get('id')} {t.get('name')} ({t.get('kind')})" for t in items[:10]) + ".") if items else "No targets registered.",
            data={"product": spec.id, "items": items, "executed": False},
            evidence=tuple(str(t.get("id")) for t in items if t.get("id")))

    def _qa_list_profiles(self, spec, inp, who):
        r = self.hub.qa(spec.id).profiles(inp.get("target_id", ""), actor=who, source="sidekick")
        items = r.get("items", r.get("profiles", [])) if isinstance(r, Mapping) else []
        return CapabilityResult(capability_id="quality.qa_list_profiles", answer=(f"{len(items)} profile(s): " + "; ".join(
            f"{p.get('id')} {'available' if p.get('available', True) else 'unavailable (' + str((p.get('unavailable') or {}).get('reason', '')) + ')'}"
            for p in items[:12]) + ".") if items else "No profiles.", data={"product": spec.id, "items": items, "executed": False})

    def _qa_estimate(self, spec, inp, who):
        r = self.hub.qa(spec.id).estimate(inp["target_id"], inp["profile"], actor=who)
        return CapabilityResult(capability_id="quality.qa_estimate", answer=(
            f"Estimate {r.get('estimate_id', '')} for {inp['profile']} on {inp['target_id']}: digest {r.get('digest', '')}"
            + (" (this run would be refused)" if r.get("will_refuse") else "") + ". Nothing was connected or run."),
            data={"product": spec.id, "estimate": r, "executed": False}, evidence=(str(r.get("estimate_id", "")),))

    def _qa_start_run(self, spec, inp, who):
        body = {k: inp[k] for k in ("target_id", "profile", "estimate_digest", "defect") if k in inp}
        r = self.hub.qa(spec.id).start_run(body, actor=who, source="sidekick")
        rid = str(r.get("id") or r.get("run_id") or "")
        st = str(r.get("state") or "")
        card = qamod.approval_card(spec, r) if st == "awaiting_approval" else None
        self.hub.invalidate(spec.id)
        ans = (f"Run {rid} is {st.replace('_', ' ')}." + (" It needs a person's approval before anything runs." if card else ""))
        return CapabilityResult(capability_id="quality.qa_start_run", answer=ans, data={
            "product": spec.id, "run_id": rid, "state": st, "approval": card, "executed": False},
            evidence=(rid,), artifacts=(_link(spec, "run", rid, f"/ui/runs/{rid}"),))

    def _qa_list_runs(self, spec, inp, who):
        r = self.hub.qa(spec.id).runs(state=inp.get("state", ""), limit=inp.get("limit", 20), actor=who, source="sidekick")
        items = r.get("items", r.get("runs", [])) if isinstance(r, Mapping) else []
        lines = [f"{x.get('id') or x.get('run_id')} {x.get('state')}" + (f" {x.get('outcome')}" if x.get("outcome") else "") for x in items[:10]]
        return CapabilityResult(capability_id="quality.qa_list_runs", answer=(f"{len(items)} run(s): " + "; ".join(lines) + ".") if items else "No runs match.",
                                data={"product": spec.id, "items": items, "executed": False},
                                evidence=tuple(str(x.get("id") or x.get("run_id")) for x in items if x.get("id") or x.get("run_id")))

    def _qa_get_run(self, spec, inp, who):
        r = self.hub.qa(spec.id).run(inp["run_id"], actor=who, source="sidekick")
        state, progress = qamod.mission_state(r)
        return CapabilityResult(capability_id="quality.qa_get_run", answer=f"Run {inp['run_id']}: {r.get('state')} ({progress})"
                                + (f", outcome {r.get('outcome')}, level {r.get('level')}" if r.get("outcome") else "") + ".",
                                data={"product": spec.id, "run": r, "executed": False}, evidence=(inp["run_id"],),
                                artifacts=(_link(spec, "run", inp["run_id"], f"/ui/runs/{inp['run_id']}"),))

    def _qa_run_events(self, spec, inp, who):
        r = self.hub.qa(spec.id).events(inp["run_id"], inp.get("after", 0), inp.get("limit", 50), actor=who, source="sidekick")
        items = r.get("items", []) if isinstance(r, Mapping) else []
        last = items[-1] if items else {}
        return CapabilityResult(capability_id="quality.qa_run_events", answer=(f"{len(items)} event(s); latest: {last.get('type')} {last.get('label', '')}".strip() + ".") if items else "No new events.",
                                data={"product": spec.id, "events": r, "executed": False}, evidence=(inp["run_id"],))

    def _qa_cancel_run(self, spec, inp, who):
        r = self.hub.qa(spec.id).cancel(inp["run_id"], actor=who)
        self.hub.invalidate(spec.id)
        return CapabilityResult(capability_id="quality.qa_cancel_run", answer=f"Cancel requested for run {inp['run_id']}: now {r.get('state')}.",
                                data={"product": spec.id, "run_id": inp["run_id"], "state": r.get("state"), "executed": False},
                                evidence=(inp["run_id"],))

    def _qa_get_evidence(self, spec, inp, who):
        r = self.hub.qa(spec.id).evidence(inp["run_id"], actor=who, source="sidekick")
        return CapabilityResult(capability_id="quality.qa_get_evidence", answer=(
            f"Evidence of {inp['run_id']}: level {r.get('level')} (ceiling {r.get('ceiling')}), {len(r.get('files') or [])} file(s)."),
            data={"product": spec.id, "evidence": r, "executed": False}, evidence=(inp["run_id"],),
            artifacts=(_link(spec, "evidence", inp["run_id"], f"/ui/runs/{inp['run_id']}/evidence"),))

    def _qa_verify(self, spec, inp, who):
        r = self.hub.qa(spec.id).verify(inp["run_id"], actor=who, source="sidekick")
        return CapabilityResult(capability_id="quality.qa_verify", answer=f"Independent verification of {inp['run_id']}: {r.get('outcome', r.get('status', 'done'))}.",
                                data={"product": spec.id, "verify": r, "executed": False}, evidence=(inp["run_id"],))

    # skills (SKILLS_DESIGN.md) -------------------------------------------------------------------
    def _ui_audit(self, spec, inp, who):
        """Audit a UI THROUGH the QA service — the only component with a real browser, budgets, egress gating and
        the evidence chain. The audit profile must be advertised by the service; while it is not, the answer is an
        explicit unavailable (never a simulated audit), so no claim can outrun the redevops-qa CI evidence (§8.4)."""
        qa = self.hub.qa(spec.id)
        want = inp.get("profile_id") or AUDIT_PROFILE_DEFAULT
        prof = qa.profiles(target_id=inp["target_id"], actor=who, source="sidekick")
        entry = next((p for p in (prof.get("items") or []) if isinstance(p, Mapping) and p.get("id") == want), None)
        if entry is None or not entry.get("available"):
            u = dict(entry.get("unavailable") or {}) if isinstance(entry, Mapping) else {}
            return unavailable_result("quality.ui_audit", spec.display_name, {
                "code": str(u.get("code") or "profile_unavailable"),
                "reason": str(u.get("reason") or f"the audit profile {want} is not available on this service"),
                "remedy": str(u.get("remedy") or f"the audit suite lands in redevops-qa with its own CI evidence before "
                                                  f"{want} says anything other than unavailable")})
        est = qa.estimate(inp["target_id"], want, actor=who, source="sidekick")
        body = {"target_id": inp["target_id"], "profile": want}
        if est.get("digest"):
            body["estimate_digest"] = str(est["digest"])
        r = qa.start_run(body, actor=who, source="sidekick")
        rid, st = str(r.get("id") or r.get("run_id") or ""), str(r.get("state") or "")
        card = qamod.approval_card(spec, r) if st == "awaiting_approval" else None
        self.hub.invalidate(spec.id)
        ans = (f"Audit run {rid} is {st.replace('_', ' ')}."
               + (" It needs a person's approval before anything runs." if card else " Findings land with the run."))
        return CapabilityResult(capability_id="quality.ui_audit", answer=ans, data={
            "product": spec.id, "run_id": rid, "state": st, "profile": want, "approval": card,
            "findings": [], "severity_map": AUDIT_RULE_SEVERITY, "executed": False},
            evidence=(rid,), artifacts=(_link(spec, "run", rid, f"/ui/runs/{rid}"),))

    def _plan_generate(self, spec, inp, who):
        """The plan body is composed by the SERVICE from allow-listed builders; the tool carries ids and one closed
        enum. A service without the plan builder answers unavailable with the remedy — never a locally invented plan."""
        if not (self.hub.status(spec.id).get("features") or {}).get("plans"):
            return unavailable_result("quality.plan_generate", spec.display_name, {
                "code": "plans_not_shipped",
                "reason": "this redevops-qa service exposes no plan builder; plan bodies are composed by the service "
                          "itself, never by a caller or a model",
                "remedy": "the allow-listed plan builders (smoke, regression_suite, audit_followup) land in "
                          "redevops-qa behind /v1/plans; until then this tool says unavailable"})
        body = {"target_id": inp["target_id"], "scope": inp["scope"]}
        if inp.get("source_run_id"):
            body["source_run_id"] = inp["source_run_id"]
        r = self.hub.qa(spec.id).plan_generate(body, actor=who, source="sidekick")
        pid, digest = str(r.get("plan_id") or r.get("id") or ""), str(r.get("digest") or "")
        return CapabilityResult(capability_id="quality.plan_generate",
                                answer=f"Frozen plan {pid} ({inp['scope']})" + (f", digest {digest[:19]}…" if digest else "")
                                       + ". A run can be pinned to it.",
                                data={"product": spec.id, "plan": r, "executed": False},
                                evidence=tuple(x for x in (pid, digest) if x),
                                artifacts=(_link(spec, "plan", pid, f"/ui/plans/{pid}"),))

    def _deployment_scan(self, spec, inp, who):
        """The existing GET-only inspection, promoted to a tool: reads ONLY the endpoints the product already serves
        to readers, through the product's own client (one token, one transport — no new network surface)."""
        if inp["product_id"] != spec.id:
            return CapabilityResult(capability_id="security.deployment_scan", confidence=0.0,
                                    answer=f"{inp['product_id']} is not the registered security product (this hub serves {spec.id}).",
                                    data={"status": "refused", "product": inp["product_id"], "executed": False})
        from agentic_os.agent_gateway.external.deployment_inspection import (
            SENTINEL_READ_ENDPOINTS, derive_findings, sample_sources)   # lazy: the gateway's pure read side
        cli = self.hub.client(spec.id)

        def _get(path: str):
            try:
                return cli.get(path, actor=who, source="sidekick")
            except ProductError:
                return None

        snap = self.hub.cached(f"{spec.id}:deployment-scan", lambda: {p: _get(p) for p in SENTINEL_READ_ENDPOINTS})
        findings = [f.view() for f in derive_findings(snap)]
        samples = sample_sources(snap)
        connected = any(v is not None for v in snap.values())
        ans = (f"{spec.display_name} deployment scan: {len(findings)} finding(s) from live answers"
               + (f", {len(samples)} plane(s) answered with sample data" if samples else "") + ".") if connected else \
            f"{spec.display_name} answered none of its read endpoints."
        return CapabilityResult(capability_id="security.deployment_scan", answer=ans, confidence=1.0 if connected else 0.0,
                                data={"product": spec.id, "connected": connected,
                                      "scan": {"target": spec.api_base, "connected": connected, "findings": findings,
                                               "sample_sources": samples},
                                      "severity_map": DEPLOY_SEVERITY, "executed": False},
                                evidence=tuple(f["evidence_ref"] for f in findings),
                                artifacts=(_link(spec, "scan", "deployment", "/ui/"),))

    def _consolidate(self, spec, inp, who):
        """A fold over records each product already exposes through the existing tool paths. No store, no second copy:
        every row cites a product record id and at least one evidence link, and an unknown severity never becomes a row."""
        want = set(inp.get("product_ids") or [])
        floor = SEVERITY_RANK.get(inp.get("severity_min") or "note", 0)
        rows: List[Dict[str, Any]] = []
        sections: Dict[str, Any] = {}

        def add(section: str, sev: Optional[str], finding: str, steps: str, fix: str, record: str,
                links: List[str], status: str) -> None:
            if sev is None or SEVERITY_RANK.get(sev, -1) < floor or not record:
                return
            rows.append({"section": section, "severity": sev, "finding": finding, "reproduction": steps,
                         "recommended_fix": fix, "record_id": record, "links": links, "verification": status})

        sec = next((s for s in self.hub.specs() if s.kind == "security"), None)
        if sec and (not want or sec.id in want):
            try:
                scan = self._deployment_scan(sec, {"product_id": sec.id}, who)
                sd = scan.data or {}
                if not sd.get("connected"):
                    sections["deployment"] = {"included": False, "reason": "the product answered none of its read endpoints"}
                else:
                    flist = (sd.get("scan") or {}).get("findings") or []
                    sections["deployment"] = {"included": True, "count": len(flist)}
                    for f in flist:
                        add("deployment", DEPLOY_SEVERITY.get(str(f.get("severity"))),
                            f"{f.get('source')}: {f.get('detail')}",
                            f"GET {f.get('source')} on the deployment answers this state",
                            "the endpoint's own remedy text where present; otherwise a deployment-side configuration change",
                            str(f.get("evidence_ref")), [sec.ui_link("/ui/")], "derived from live deployment state")
            except ProductError as e:
                sections["deployment"] = {"included": False, "reason": e.code}
            try:
                snag = self.hub.sentinel(sec.id)
                items = [(snag.case(cid, actor=who, source="sidekick"),) for cid in (inp.get("case_ids") or [])]
                pairs = []
                for tup in items:
                    full = tup[0]
                    pairs.append((full.get("summary") if isinstance(full.get("summary"), Mapping) else full, full))
                if not inp.get("case_ids"):
                    pairs = [(c, c) for c in (snag.cases(limit=20, actor=who, source="sidekick").get("items") or [])]
                for summ, full in pairs:
                    ev = full.get("evidence") or []
                    e0 = ev[0] if ev and isinstance(ev[0], Mapping) else {}
                    acts = full.get("actions_detail") or full.get("actions") or []
                    cid = str(summ.get("case_id") or "")
                    add("cases", CASE_SEVERITY.get(str(summ.get("severity"))),
                        str(summ.get("title") or summ.get("case_type") or "security case"),
                        (f"evidence {e0.get('artifact_id')} from {e0.get('source')} at {e0.get('acquired_at')}" if e0
                         else "the case's evidence chain"),
                        sn.action_consequence(acts[0]) if acts else "triage the case; any action goes through a person's approval",
                        cid, [sec.ui_link(f"/ui/cases/{cid}")], str(summ.get("status") or ""))
                sections["cases"] = {"included": True, "count": len(pairs)}
            except ProductError as e:
                sections["cases"] = {"included": False, "reason": e.code}
        qua = next((s for s in self.hub.specs() if s.kind == "quality"), None)
        if qua and (not want or qua.id in want):
            try:
                qa = self.hub.qa(qua.id)
                runs = ([qa.run(rid, actor=who, source="sidekick") for rid in (inp.get("run_ids") or [])]
                        or (qa.runs(limit=20, actor=who, source="sidekick").get("items") or []))
                n = 0
                for run in runs:
                    rid, outcome = str(run.get("id") or ""), str(run.get("outcome") or "")
                    stt = str(run.get("state") or "")
                    if not rid or (outcome or stt) in ("VERIFIED", "CORROBORATED", "awaiting_approval", "queued", "running"):
                        continue          # a passing or unfinished run is not a finding
                    sev = "major" if (outcome == "FAILED" or stt == "failed") else "note"
                    add("runs", sev, f"QA run {rid} on profile {run.get('profile')}",
                        f"the frozen profile {run.get('profile')} against target {run.get('target_id')}",
                        "the run's evidence bundle names the failed assertion and its step", rid,
                        [qua.ui_link(f"/ui/runs/{rid}/evidence")], outcome or stt)
                    n += 1
                sections["runs"] = {"included": True, "count": n}
            except ProductError as e:
                sections["runs"] = {"included": False, "reason": e.code}
        if not any(s.get("included") for s in sections.values()):
            reasons = "; ".join(f"{k}: {v.get('reason')}" for k, v in sections.items() if not v.get("included"))
            return unavailable_result("findings.consolidate", spec.display_name,
                                      {"code": "nothing_to_fold", "reason": reasons or "no product registered",
                                       "remedy": "start the products or check their entries in the products registry"})
        out = sorted(rows, key=lambda r: -SEVERITY_RANK.get(r["severity"], -1))
        ans = (f"Consolidated {len(out)} finding(s): " + "; ".join(f"{r['record_id']} [{r['severity']}]" for r in out[:6]) + "."
               if out else "No findings at or above the requested severity right now.")
        return CapabilityResult(capability_id="findings.consolidate", answer=ans, data={
            "report": {"rows": out, "sections": sections}, "executed": False},
            evidence=tuple(r["record_id"] for r in out),
            artifacts=tuple(ArtifactLink(provider="products", resource_type="finding", resource_id=r["record_id"],
                                         native_url=r["links"][0]) for r in out))


# ── registry ──────────────────────────────────────────────────────────────────────────────────────
def build_registry(hub: ProductHub) -> CapabilityRegistry:
    """A registry holding a bound descriptor per tool whose product KIND is registered. Never registers a forbidden
    (decide/execute) capability, whatever a registry file declares."""
    reg = CapabilityRegistry()
    tools = ProductTools(hub)
    kinds = {s.kind for s in hub.specs()}
    for cid, dom, summary, intents, req, opt, cands in _TOOLS:
        assert cid not in FORBIDDEN_TOOLS and cid in INPUT_SCHEMAS and cid in TOOL_CLASS
        if dom == "findings":
            if not kinds & {"quality", "security"}:
                continue
        elif dom not in kinds:
            continue
        provs = ("product:security", "product:quality") if dom == "findings" else (f"product:{dom}",)
        authority = {"read": ("read",), "stage": ("stage: creates a proposal only; a person decides",),
                     "present": ("read",), "bounded_write": ("bounded write: service token + product role + product approval policy",)}[TOOL_CLASS[cid]]

        def _handler(_cid=cid, **kw):
            actor = kw.pop("_actor", "")
            return tools.call(_cid, kw, actor)
        reg.register(SidekickCapability(
            capability_id=cid, domain=D(dom), summary=summary, intents=intents, required_inputs=req,
            optional_inputs=opt, required_provider_capabilities=provs, candidate_actions=cands,
            authority_requirements=authority,
            verification_contract="the product's own persisted record and, for actions, its post-action read-back",
            learning_contract="", maturity=M.L2_GENERALIZED, status="IMPLEMENTED",
            code_ref="agentic_os/products/tools.py", handler=_handler))
    return reg


__all__ = ["ProductTools", "build_registry", "unavailable_result", "error_result"]
