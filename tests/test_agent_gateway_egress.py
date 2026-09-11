"""Data-egress enforcement — Phase 4 (plan §6, §13 Phase-4 acceptance).

PII can be redacted or denied; aggregates pass while raw rows are blocked; the egress decision is
recorded (audit/EXPLAIN); restricted output never reaches the client. Tested both as a unit and
end-to-end through the gateway.
"""
from __future__ import annotations

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, CapabilityKind, CapabilityManifest, DataClass, EgressAction, EgressRule,
    GatewayPrincipal, GatewayRequest, GatewayStatus, HandlerResult, InMemoryAuditSink,
    PolicyEgressEngine, redact, tokenize)
from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.agent_gateway.egress import summarize
from agentic_os.agent_gateway.registry import CapabilityRegistry


def _gp(scopes=(), tenant="acme"):
    return GatewayPrincipal(Principal("agent", "service", (), tenant), scopes=tuple(scopes))


# ── redaction helpers ──────────────────────────────────────────────────────────────
def test_redact_masks_sensitive_keys_and_secret_shaped_strings():
    out = redact({"name": "Tasha", "email": "t@acme.com", "access_token": "abc",
                  "note": "reach at t@acme.com or Bearer sk-ABCDEFGH1234",
                  "rows": [{"password": "p"}]})
    assert out["name"] == "Tasha"                       # ordinary value kept
    assert out["access_token"] == "«redacted»"          # sensitive KEY masked
    assert "t@acme.com" not in out["note"] and "«redacted»" in out["note"]   # email in a string masked
    assert out["rows"][0]["password"] == "«redacted»"   # nested + list walked


def test_tokenize_is_stable_and_referential():
    a = tokenize({"secret": "x"}); b = tokenize({"secret": "x"})
    assert a["secret"].startswith("tok_") and a == b     # same input → same token


def test_summarize_withholds_rows_but_keeps_shape():
    s = summarize([{"a": 1}, {"a": 2}, {"a": 3}])
    assert s["count"] == 3 and "withheld" in s["summary"]


# ── the engine ──────────────────────────────────────────────────────────────────────
_M = CapabilityManifest("x.read", "d", CapabilityKind.DIRECT, risk_tier=RiskTier.READ)


def test_secret_is_always_denied():
    eng = PolicyEgressEngine()
    action, out, rules = eng.decide({"secret": "s"}, (DataClass.SECRET,), _gp(), _M)
    assert action is EgressAction.DENY and out is None and "egress:secret:deny" in rules


def test_pii_redacted_by_default_allowed_with_scope():
    eng = PolicyEgressEngine()
    a, out, rules = eng.decide({"email": "t@acme.com"}, (DataClass.PII,), _gp(), _M)
    assert a is EgressAction.REDACT and out["email"] == "«redacted»" and "egress:pii:redact" in rules
    a2, out2, rules2 = eng.decide({"email": "t@acme.com"}, (DataClass.PII,),
                                  _gp(scopes=("egress:pii",)), _M)
    assert a2 is EgressAction.ALLOW and out2 == {"email": "t@acme.com"} and rules2 == ()


def test_aggregate_allowed_while_rows_blocked():
    eng = PolicyEgressEngine()
    agg, out, _ = eng.decide({"count": 42}, (DataClass.INTERNAL,), _gp(), _M)      # aggregate
    rows, out2, _ = eng.decide([{"email": "a@b.com"}], (DataClass.CUSTOMER_CONTENT,), _gp(), _M)
    assert agg is EgressAction.ALLOW and out == {"count": 42}
    assert rows is EgressAction.REDACT and out2[0]["email"] == "«redacted»"


def test_strictest_action_wins_across_classes():
    eng = PolicyEgressEngine()
    a, out, _ = eng.decide({"x": 1}, (DataClass.PII, DataClass.SECRET), _gp(), _M)
    assert a is EgressAction.DENY and out is None          # SECRET(deny) beats PII(redact)


def test_custom_rule_can_deny_pii_outright():
    eng = PolicyEgressEngine(rules=(EgressRule(DataClass.PII, EgressAction.DENY, rule_id="no-pii"),))
    a, out, rules = eng.decide({"email": "t@acme.com"}, (DataClass.PII,), _gp(), _M)
    assert a is EgressAction.DENY and out is None and rules == ("no-pii",)


# ── end to end through the gateway ────────────────────────────────────────────────
def _gw_with(output, classes, egress=None, scopes=()):
    m = CapabilityManifest("crm.lookup_account", "d", CapabilityKind.DIRECT,
                           permissions=("crm.read",), risk_tier=RiskTier.READ, data_classes=classes)
    reg = CapabilityRegistry().register(m, lambda req, env: HandlerResult(ok=True, output=output,
                                                                          data_classes=classes))
    gw = AgentGateway(registry=reg, authorize=lambda p, perm: perm == "crm.read",
                      egress=egress or PolicyEgressEngine(), audit=InMemoryAuditSink())
    return gw, GatewayPrincipal(Principal("agent", "service", (), "acme"), scopes=tuple(scopes))


def test_gateway_redacts_pii_and_records_the_decision():
    gw, gp = _gw_with({"email": "t@acme.com", "name": "Tasha"}, (DataClass.PII,))
    r = gw.invoke(GatewayRequest(gp, "crm.lookup_account", {"query": "Tasha"}))
    assert r.status is GatewayStatus.OK
    assert r.output["email"] == "«redacted»" and r.output["name"] == "Tasha"
    assert r.decision.egress_action is EgressAction.REDACT
    assert "egress:pii:redact" in r.decision.policy_rule_ids
    assert gw.audit.events[-1].decision.egress_action is EgressAction.REDACT   # EXPLAIN/audit


def test_gateway_denies_secret_output_never_reaches_client():
    gw, gp = _gw_with({"api_key": "sk-secret"}, (DataClass.SECRET,))
    r = gw.invoke(GatewayRequest(gp, "crm.lookup_account", {}))
    assert r.status is GatewayStatus.OK and r.output is None      # withheld
    assert r.decision.egress_action is EgressAction.DENY
    assert r.client_view()["output"] is None                     # restricted output never leaves


def test_gateway_allows_when_principal_holds_the_egress_scope():
    gw, gp = _gw_with({"email": "t@acme.com"}, (DataClass.PII,), scopes=("egress:pii",))
    r = gw.invoke(GatewayRequest(gp, "crm.lookup_account", {}))
    assert r.output == {"email": "t@acme.com"} and r.decision.egress_action is EgressAction.ALLOW
