"""The one Sidekick hook: turn a product-related message into a typed tool call, or ask for what is missing.

``sidekick_reply`` calls :func:`try_handle` once, before its knowledge-base fallback. It answers only when

1. the message names a registered product's subject (its name, its domain words, or an exact record id), AND
2. deterministic discovery (``CapabilityRegistry.discover``) ranks a BOUND tool above the threshold, AND
3. every required input is present: taken from the structured context (``ctx.objectRef`` such as ``case:case-…``), or an
   exact id / IP address in the text. It never invents an input: a missing one becomes a QUESTION, not a guess.

Otherwise it returns None and Sidekick behaves exactly as before. Decide and execute are not tools, so no message can make
this hook approve or execute anything: a ban request produces a proposal and its approval card.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentic_os.sidekick.capability import CapabilityDomain as D, CapabilityRegistry
from agentic_os.sidekick.orchestrator import CapabilityResult, SidekickOrchestrator

from .hub import ProductHub
from .schemas import INPUT_SCHEMAS

_CASE = re.compile(r"\bcase-[0-9a-f]{16}\b")
_ACT = re.compile(r"\bact-[0-9a-f]{16}\b")
_RUN = re.compile(r"\br[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\b")
_TGT = re.compile(r"\btgt-[0-9a-f]{12}\b")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_HOURS = re.compile(r"\bfor\s+(\d{1,4})\s*(?:h|hr|hrs|hours?)\b", re.I)
_REASON = re.compile(r"\b(?:because|reason\s*[:=-]?|since)\s+(.{3,200})$", re.I)

#: words that mark a message as being about a product at all (besides its name and exact record ids)
_SUBJECT: Dict[str, Tuple[str, ...]] = {
    "security": ("sentinel", "crowdsec", "security", "ban", "unban", "block", "alert", "alerts", "intrusion", "threat"),
    "quality": ("qa", "redevops-qa", "test run", "test runs", "run tests", "quality", "test profile", "test target", "evidence bundle"),
}
THRESHOLD = 0.3


def _first_ip(text: str) -> str:
    for m in list(_IPV4.finditer(text)) + list(_IPV6.finditer(text)):
        try:
            return str(ipaddress.ip_address(m.group(0)))
        except ValueError:
            continue
    return ""


def extract_inputs(text: str, ctx: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    obj = str(ctx.get("objectRef") or "")
    for rx, key in ((_CASE, "case_id"), (_ACT, "request_id"), (_RUN, "run_id"), (_TGT, "target_id")):
        m = rx.search(text) or rx.search(obj)
        if m:
            out[key] = m.group(0)
    ip = _first_ip(text)
    if ip:
        out["ip"] = ip
    h = _HOURS.search(text)
    if h:
        out["duration_hours"] = int(h.group(1))
    r = _REASON.search(text.strip())
    if r:
        out["reason"] = r.group(1).strip().rstrip(".")
    m = re.search(r"\bprofile\s+([a-z0-9-]{1,40})\b", text, re.I)
    if m:
        out["profile"] = m.group(1).lower()
    return out


_DECIDE_WORDS = re.compile(r"\b(approve|approved|approving|execute|executing|decide|confirm|go ahead|authori[sz]e|run it|do it)\b", re.I)
_UNBAN = re.compile(r"\b(unban|unblock|lift(?:\s+the)?\s+ban|remove(?:\s+the)?\s+ban)\b", re.I)
_BAN = re.compile(r"\b(ban|block)\b", re.I)
_PROPOSE = re.compile(r"\bpropos(?:e|ing)\b", re.I)


_STOP = frozenset("the a an of me my to is are in on and please can could you i us we with about all any this that it for".split())


def _mask_values(text: str) -> str:
    """The text with ids and addresses removed, so numbers and hex never dilute intent ranking."""
    for rx in (_CASE, _ACT, _RUN, _TGT, _IPV4, _IPV6):
        text = rx.sub(" ", text)
    return " ".join(w for w in re.findall(r"[A-Za-z0-9-]+", text) if w.lower() not in _STOP)


def _subject_hit(hub: ProductHub, t: str, ctx_obj: str = "") -> List[str]:
    kinds: List[str] = []
    low = t.lower()
    t = t + " " + ctx_obj
    for spec in hub.specs():
        words = set(_SUBJECT.get(spec.kind, ())) | {spec.id, spec.display_name.lower()}
        if any(re.search(r"(?<![a-z0-9-])" + re.escape(w) + r"(?![a-z0-9-])", low) for w in words) \
                or (spec.kind == "security" and (_CASE.search(t) or _ACT.search(t))) \
                or (spec.kind == "quality" and (_RUN.search(t) or _TGT.search(t))):
            if spec.kind not in kinds:
                kinds.append(spec.kind)
    return kinds


def _args_for(cap_id: str, found: Mapping[str, Any]) -> Dict[str, Any]:
    props = INPUT_SCHEMAS[cap_id]["properties"]
    return {k: v for k, v in found.items() if k in props}


_DECIDE_REFUSAL = ("I can't approve or execute anything: a person decides. Review the proposal and approve it in Projects "
                   "(Today or Attention) or in the product itself; execution is a separate step after the approval.")


_READBACK = re.compile(r"\b(read-?\s?back|verif\w*|was it (?:blocked|banned))\b", re.I)
_ACTIONS = re.compile(r"\b(proposals?|proposed actions?|approval queue|pending approvals?|waiting for approval|actions)\b", re.I)
_CASES = re.compile(r"\bcases?\b", re.I)
_POSTURE = re.compile(r"\b(posture|status|overview|how is|how are we|state of|summary)\b", re.I)
_QA_RULES = ((re.compile(r"\b(events?|progress)\b", re.I), "run_id", "quality.qa_run_events"),
             (re.compile(r"\bevidence\b", re.I), "run_id", "quality.qa_get_evidence"),
             (re.compile(r"\bverif\w*\b", re.I), "run_id", "quality.qa_verify"),
             (re.compile(r"\bcancel|stop\b", re.I), "run_id", "quality.qa_cancel_run"),
             (re.compile(r"\b(run|status|going)\b", re.I), "run_id", "quality.qa_get_run"))


def _rule_route(text: str, found: Mapping[str, Any], registry: CapabilityRegistry) -> Optional[str]:
    """Deterministic routing for the common phrasings (a ban/unban request is a proposal tool, never a fuzzy match).
    Anything not matched falls back to ranked discovery."""
    for rx, key, cid in _QA_RULES:
        if key in found and rx.search(text) and registry.get(cid) is not None:
            return cid
    if registry.get("security.sentinel_propose_unban") is None:
        return None
    if _UNBAN.search(text) and ("ip" in found or _PROPOSE.search(text)):
        return "security.sentinel_propose_unban"
    if _BAN.search(text) and ("ip" in found or _PROPOSE.search(text)):
        return "security.sentinel_propose_ban"
    if "request_id" in found:
        return "security.sentinel_read_back" if _READBACK.search(text) else "security.sentinel_request_decision"
    if "case_id" in found and _CASES.search(text):
        return "security.sentinel_get_case"
    if _ACTIONS.search(text):
        return "security.sentinel_list_actions"
    if re.search(r"\bcase\b", text, re.I):                    # one case: needs its id (asks when absent)
        return "security.sentinel_get_case"
    if _CASES.search(text):
        return "security.sentinel_list_cases"
    if _POSTURE.search(text) or re.search(r"\b(alerts?|threats?)\b", text, re.I):
        return "security.sentinel_posture"
    return None


def try_handle(hub: ProductHub, registry: CapabilityRegistry, ctx: Mapping[str, Any], text: str) -> Optional[Dict[str, Any]]:
    kinds = _subject_hit(hub, text, str(ctx.get("objectRef") or ""))
    if not kinds:
        return None
    found = extract_inputs(text, ctx)
    orch = SidekickOrchestrator(registry=registry)
    if _DECIDE_WORDS.search(text) and not _PROPOSE.search(text):
        # decide/execute are not tools. Show the approval card when a proposal is named, and say who decides.
        if "request_id" in found and registry.get("security.sentinel_request_decision") is not None:
            res = orch.invoke("security.sentinel_request_decision", request_id=found["request_id"])
            reply = _reply(res, "security.sentinel_request_decision")
            reply["text"] = _DECIDE_REFUSAL + " " + res.answer
            return reply
        return {"text": _DECIDE_REFUSAL, "topic": "Products", "tool": "", "data": {"executed": False, "status": "refused"}}
    best = None
    routed = _rule_route(text, found, registry)
    if routed:
        cap = registry.get(routed)
    else:
        for kind in kinds:
            matches = [m for m in orch.discover(_mask_values(text), available_inputs=tuple(found), domain=D(kind), limit=5)
                       if m.capability.bound and m.score >= THRESHOLD]
            if matches and (best is None or matches[0].score > best.score):
                best = matches[0]
        if best is None:
            return None
        cap = best.capability
    missing = [k for k in cap.required_inputs if k not in found]
    kind = cap.capability_id.split(".", 1)[0]
    down = next((s.id for s in hub.specs() if s.kind == kind and not hub.ready(s.id)), None)
    if missing and down is not None:          # say WHY it is unavailable before asking for inputs it could not use
        missing = []
    if missing:
        ask = {"case_id": "Which case? Give its id (case-…), or open the case and ask again.",
               "request_id": "Which proposal? Give its id (act-…).",
               "run_id": "Which run? Give its id (r…Z-…).",
               "target_id": "Which target? Give its id (tgt-…).",
               "ip": "Which IP address?",
               "reason": "What is the reason? Say it as: … because <reason>.",
               "profile": "Which test profile? Say it as: … profile <name>."}
        return {"text": ask.get(missing[0], f"I need {missing[0]} to do that."), "topic": "Products",
                "tool": cap.capability_id, "missing": missing}
    res: CapabilityResult = orch.invoke(cap.capability_id, **_args_for(cap.capability_id, found))
    return _reply(res, cap.capability_id)


def _reply(res: CapabilityResult, cap_id: str) -> Dict[str, Any]:
    data = res.data if isinstance(res.data, dict) else {}
    actions: List[Dict[str, Any]] = []
    card = data.get("approval")
    if isinstance(card, dict):
        actions.append({"label": "Review in Attention", "kind": "navigate", "ref": "attention"})
        if isinstance(card.get("open"), dict) and card["open"].get("url"):
            actions.append({"label": card["open"].get("label", "Open"), "kind": "open", "ref": card["open"]["url"]})
    for a in res.artifacts[:3]:
        if a.native_url and not any(x.get("ref") == a.native_url for x in actions):
            actions.append({"label": f"Open {a.resource_type} {a.resource_id}"[:60], "kind": "open", "ref": a.native_url})
    reply: Dict[str, Any] = {"text": res.answer, "topic": data.get("product", "Products"), "tool": cap_id,
                             "data": {k: v for k, v in data.items() if k in ("product", "status", "case_id", "request_id", "run_id",
                                                                          "state", "executed", "code", "reason", "remedy")},
                             "evidence": list(res.evidence), "actions": actions}
    if "items" in data:
        reply["items"] = data["items"]
    return reply


__all__ = ["try_handle", "extract_inputs"]
