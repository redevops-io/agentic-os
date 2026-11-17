"""Documented routes for the analysis modules that can be exposed safely: each takes validated, bounded input, is deterministic, and
none of them acts on anything. They live under `/api/v1` and follow its authentication (every route needs a credential).

  POST /api/v1/hunt/validate         is this hunt query read-only and well-formed? (hunt.validate_query; nothing is ever executed)
  GET  /api/v1/attack/map            which ATT&CK techniques does a CrowdSec scenario name map to, and why (attack.map_scenario_to_techniques)
  POST /api/v1/ioc/normalize         canonical STIX observable and indicator for an IOC string (stix.normalize_ioc)
  POST /api/v1/detections/validate   structural validation of a Sigma or YARA rule (detection.validate_*), never published or deployed
  POST /api/v1/agent-events          one agent-runtime event becomes an INVESTIGATING case: a signal, no action (agent_events)
  GET  /api/v1/soc2/controls         the SOC 2 control catalogue the evidence modules test against (soc2.controls)

The modules that are NOT routed, and why, are listed in `/api/v1/info` (`unavailable`, code NOT_WIRED).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request

from . import api_v1, attack, detection, live, ratelimit, stix
from .hunt import Query, validate_query
from .identity import Principal

router = APIRouter(prefix="/api/v1")
_YAML_ALIAS = re.compile(r"(^|[\s\[{,:-])[&*][A-Za-z0-9_-]+")


@router.post("/hunt/validate")
async def hunt_validate(request: Request, p: Principal = Depends(api_v1.viewer), _rl=Depends(ratelimit.rate_limit)):
    b = api_v1._body(await api_v1._json(request))
    text, backend = b.get("text"), b.get("backend")
    if not isinstance(text, str) or not isinstance(backend, str) or len(text) > 4000 or len(backend) > 40:
        raise api_v1._err(422, "invalid_body", "backend (<=40 characters) and text (<=4000 characters) are required strings")
    read_only = b.get("read_only", True)
    if not isinstance(read_only, bool):
        raise api_v1._err(422, "invalid_body", "read_only must be true or false")
    q = Query(backend=backend, text=text, read_only=read_only)
    ok, reason = validate_query(q)
    return {"ok": ok, "reason": reason, "query_id": q.query_id, "executed": False}


@router.get("/attack/map")
def attack_map(scenario: str = "", p: Principal = Depends(api_v1.viewer)):
    if not scenario or len(scenario) > 200:
        raise api_v1._err(422, "invalid_query", "scenario is required (at most 200 characters)")
    items = []
    for tid, token in attack.map_scenario_to_techniques(scenario):
        obj = attack.attack_pattern(tid)
        items.append({"technique_id": tid, "name": obj.props["name"], "tactic": obj.props["kill_chain_phases"][0]["phase_name"], "basis": token,
                      "url": obj.props["external_references"][0]["url"]})
    return {"scenario": scenario, "techniques": items, "attack_version": attack.ATTACK_VERSION, "method": "deterministic scenario-name table, not a model"}


@router.post("/ioc/normalize")
async def ioc_normalize(request: Request, p: Principal = Depends(api_v1.viewer), _rl=Depends(ratelimit.rate_limit)):
    b = api_v1._body(await api_v1._json(request))
    value = b.get("value")
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise api_v1._err(422, "invalid_body", "value is required (at most 2048 characters)")
    sco, indicator = stix.normalize_ioc(value.strip(), source="analyst")
    kind = stix.classify_ioc(value.strip())[1]
    return {"kind": kind, "observable": sco.to_dict(), "indicator": indicator.to_dict()}


@router.post("/detections/validate")
async def detections_validate(request: Request, p: Principal = Depends(api_v1.viewer), _rl=Depends(ratelimit.rate_limit)):
    b = api_v1._body(await api_v1._json(request))
    language, content = b.get("language"), b.get("content")
    if language not in ("sigma", "yara") or not isinstance(content, str) or not content.strip() or len(content) > 16384:
        raise api_v1._err(422, "invalid_body", "language (sigma or yara) and content (at most 16384 characters) are required")
    if language == "sigma" and _YAML_ALIAS.search(content):
        raise api_v1._err(422, "invalid_body", "YAML anchors and aliases are not accepted in a Sigma rule")
    res = detection.validate_sigma(content) if language == "sigma" else detection.validate_yara(content)
    return {"language": language, **res, "published": False}


@router.post("/agent-events", status_code=201)
async def agent_events(request: Request, p: Principal = Depends(api_v1.operator), _rl=Depends(ratelimit.rate_limit)):
    b = api_v1._body(await api_v1._json(request))
    minimum = b.pop("min_severity", "medium")
    if minimum not in ("info", "low", "medium", "high", "critical"):
        raise api_v1._err(422, "invalid_body", "min_severity must be info, low, medium, high or critical")
    try:
        return live.ingest_agent_event(b, actor=p.actor if p.kind == "user" else "service", via=p.via, min_severity=minimum)
    except ValueError as e:
        raise api_v1._err(422, "invalid_event", str(e)) from None
    except live.ExecutionError as e:
        raise api_v1._err(409, e.code, str(e)) from None


@router.get("/soc2/controls")
def soc2_controls(p: Principal = Depends(api_v1.viewer)):
    from .soc2 import controls
    return {"items": [{"control_id": c.control_id, "version": c.version, "title": c.title, "criterion_refs": list(c.criterion_refs),
                       "owner_role": c.owner_role, "frequency_days": c.frequency_days, "evidence_kinds": list(c.evidence_kinds),
                       "automated": bool(c.procedure), "manual_dependency": c.manual_dependency, "scope_note": c.scope_note} for c in controls.CATALOG],
            "note": "a control that maps to a criterion supports it; it does not satisfy it. Assessments need evidence collected into a vault, which this deployment does not configure."}
