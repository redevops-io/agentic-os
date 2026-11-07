"""Readable, neutral, fixed-template Timeline rows for the AOS-DD events and the operator-relevant base events.

The payloads below are trimmed copies of what the real runtime and the AOS-DD integration wrote (their field names and shapes). The rules pinned here:
  * the category and outcome come from the EXISTING closed vocabularies (no schema change) and the summary from a fixed template over STRUCTURED fields only:
    a free-text field (an application's message, a reason an operator typed, a `why`/`detail`/`next` sentence) never reaches a summary;
  * a summary is bounded, has no control characters and no markup characters, whatever the event holds;
  * an event type this table does not know keeps today's behaviour (category other, outcome info, the bare type name);
  * every event type the AOS-DD integration can emit is covered (a scan of its source fails when a new one is added without a row).
"""
from __future__ import annotations

import json
import os
import re

import jsonschema
import pytest

from agentic_os.drilldown import CATEGORIES, OUTCOMES, timeline_schema
from agentic_os.drilldown.fold import actor_of, category_of, outcome_of, summary_of
from agentic_os.mission.store import MemoryEventStore
from tests.drilldown_support import SCOPE, make_service, owner

P = {"id": "approver:default", "kind": "user", "roles": ["approver"], "tenant": "default", "method": "control-token", "issuer": "control-plane"}
OP = "aosdd.mission_3b4350e9d293.S1.a1.b9145c4ea6"

# type -> (payload, category, outcome, exact summary)
CASES = {
    "AosddCaseOpened": ({"case_key": "R7-PAGES", "attempt": 1, "customer_ref": "AOSDD-HFC-001", "principal": P, "tenant": "default"}, "lifecycle", "info", "Case opened (attempt 1)"),
    "AosddDuplicateSubmit": ({"case_key": "R1-POS", "attempt": 1, "by": "exec-1", "principal": P, "action": "returned the existing mission; nothing started"}, "lifecycle", "info", "Duplicate submission recorded (attempt 1)"),
    "AosddVerifierProvenance": ({"source": "x.json", "apps": {"chatwoot": {"read_only": "get-only-by-code-shared-writer-credential", "same_secret_as_writer": True}, "twenty": {"read_only": "server-enforced", "same_secret_as_writer": False}}}, "verification", "info", "Verifier provenance recorded for 2 applications"),
    "AosddGrantIssued": ({"phase": "read", "digest": "sha256:" + "a" * 64, "expires_at": 1.0, "grants": ["a", "b", "c"], "principal": P}, "policy", "info", "Authority issued (read): 3 grants"),
    "AosddGrantRevoked": ({"principal": P, "reason": "R3: customer disputes the invoice", "at": 1.0}, "policy", "info", "Authority revoked by approver:default"),
    "AosddPlanApproved": ({"claims": {"kind": "aosdd.plan_approval/1", "steps": [["S1", "c", "o", "d"], ["S2", "c", "o", "d"]]}, "sig": "x"}, "approval", "ok", "Plan approved: 2 steps"),
    "AosddApprovalRefused": ({"principal": P, "presented": "0" * 64, "actual": "9" * 64, "reason": "plan_sha256 differs"}, "approval", "denied", "Approval refused by approver:default: the presented plan hash did not match the plan"),
    "AosddOpIntent": ({"op_id": OP, "step": "S1", "app": "erpnext", "capability": "aosdd.erpnext.quotation_draft", "target": {"party": "Acme"}, "fence": 1}, "node", "info", "Operation intended: step S1 (erpnext, aosdd.erpnext.quotation_draft)"),
    "AosddOpApplied": ({"op_id": OP, "step": "S1", "native_id": "SAL-QTN-2026-00002", "created": True, "adopted": False, "fence": 1, "idempotency": "marker-reconciled"}, "node", "ok", "Operation applied: step S1, native id SAL-QTN-2026-00002 (created)"),
    "AosddOpRefused": ({"op_id": OP, "step": "S2", "app": "lago", "kind": "conflict", "status": 409, "message": "domain guard: <b>the invoice</b> already carries metadata", "residual": None, "rule": "conflict", "outcome": "not_applied"}, "node", "denied", "Operation refused by lago: step S2, conflict (status 409), not_applied"),
    "AosddOpReplayed": ({"op_id": OP, "step": "S1", "native_id": "SAL-QTN-2026-00002"}, "node", "info", "Recorded result reused: step S1, native id SAL-QTN-2026-00002"),
    "AosddOpStale": ({"op_id": OP, "step": "S1", "fence": 1, "native_id": "SAL-QTN-2026-00003", "outcome": "stale-generation", "why": "generation 1 is no longer the current claim"}, "node", "denied", "Late result recorded, not applied: step S1, generation 1 (stale-generation), native id SAL-QTN-2026-00003; a duplicate effect is possible"),
    "AosddOpUnresolved": ({"op_id": OP, "step": "S1", "claim": "held", "holder": "exec-1"}, "node", "pending", "Operation unresolved: step S1, claim held"),
    "AosddOpRetried": ({"op_id": OP, "step": "S1", "policy": "operator-only", "previous_fence": 1}, "node", "info", "Operation retried under the same operation id: step S1"),
    "AosddOpSuperseded": ({"op_id": OP, "step": "S1", "fence": 1, "why": "x"}, "node", "denied", "Claim superseded: step S1, generation 1"),
    "AosddOpUnknown": ({"op_id": OP, "step": "S4", "outcome": "unknown"}, "node", "pending", "Outcome unknown: step S4; the effect may or may not exist"),
    "AosddStepUncertain": ({"op_id": OP, "step": "S4", "app": "chatwoot", "why": "already dispatched by an earlier attempt; the outcome is unknown", "via": "runtime-park", "idempotency": "marker-reconciled", "retry_policy": "operator-only", "automatic_retry": False}, "node", "pending", "Outcome unknown: step S4 (chatwoot); the effect may or may not exist; automatic retry: no"),
    "AosddStepVerified": ({"op_id": OP, "step": "S1", "native_id": "X", "status": "VERIFIED", "checks": [{"check": "a", "ok": True}, {"check": "b", "ok": True}], "read_only_kind": "server-enforced", "same_secret_as_writer": False}, "verification", "ok", "Read-back verified: step S1, 2 of 2 checks passed; verifier: server-enforced"),
    "AosddAbsenceObserved": ({"step": "S1", "op_id": OP, "principal": P, "complete_lookup": True, "state": "still UNCERTAIN: nothing was re-sent", "next": "x"}, "node", "pending", "Absence observed: step S1, lookup complete"),
    "AosddRedispatchAuthorized": ({"step": "S1", "op_id": OP, "principal": P, "reason": "x", "residual_risk": "a duplicate effect is possible"}, "approval", "ok", "Redispatch authorized by approver:default: step S1; residual risk recorded"),
    "AosddRedispatchRefused": ({"step": "S1", "op_id": OP, "principal": P, "reason": "too early"}, "approval", "denied", "Redispatch refused: step S1"),
    "RedispatchRefused": ({"node_id": "nd_1", "capability": "aosdd.chatwoot.private_note", "lease_id": "l"}, "node", "denied", "Redispatch refused: aosdd.chatwoot.private_note"),
    "AosddReconciled": ({"step": "S4", "op_id": OP, "principal": P, "found": True, "native_id": 2}, "node", "ok", "Reconciled by approver:default: step S4, the effect was found (native id 2)"),
    "AosddReconcileRefused": ({"step": "S4", "principal": P, "why": "cannot read chatwoot: incomplete 200"}, "node", "denied", "Reconciliation refused: step S4"),
    "AosddUncertainAccepted": ({"step": "S4", "op_id": OP, "principal": P, "reason": "x", "consequence": "y"}, "node", "info", "Uncertain outcome accepted as it is by approver:default: step S4; not retried and not claimed to have happened"),
    "AosddResumeRequested": ({"principal": P, "reason": "R7: killed", "owner": "exec-1"}, "lifecycle", "info", "Resume requested by approver:default"),
    "AosddCompensationRequested": ({"principal": P, "reason": "R9: undo"}, "lifecycle", "info", "Compensation requested by approver:default"),
    "AosddUndoIntent": ({"op_id": OP, "step": "S4", "native_id": "2", "fence": 2}, "node", "info", "Undo intended: step S4, native id 2"),
    "AosddUndoResult": ({"op_id": OP, "step": "S4", "ok": True, "result": {"status": "ok"}, "fence": 2}, "node", "ok", "Undo executed: step S4"),
    "AosddUndoVerified": ({"op_id": OP, "step": "S4", "status": "VERIFIED", "checks": [{"check": "a", "ok": True}], "read_only_kind": "get-only-by-code-shared-writer-credential", "same_secret_as_writer": True,
                           "residual": [{"what": "a tombstone remains"}]}, "verification", "ok", "Undo read-back verified: step S4, 1 of 1 checks passed; verifier: get-only-by-code-shared-writer-credential; residual effects recorded: 1"),
    "AosddMissionHalted": ({"node_id": "n", "capability": "aosdd.lago.invoice_metadata", "rule": "expired", "reason": "x", "effects_remaining": ["a"]}, "lifecycle", "denied", "Mission halted: aosdd.lago.invoice_metadata, rule expired; 1 earlier effect remains"),
    "AosddStepRefused": ({"node_id": "n", "capability": "aosdd.lago.invoice_metadata", "rule": "plan_hash", "reason": "x", "side_effects": 0}, "node", "denied", "Step refused: aosdd.lago.invoice_metadata, rule plan_hash; no side effect occurred"),
    "StaleResultRecorded": ({"node_id": "n", "capability": "aosdd.erpnext.quotation_draft", "kind": "failure", "detail": "StaleGeneration: <script>alert(1)</script>", "lease_id": "l", "why": "x", "action": "y"}, "node", "denied", "Late result from an earlier attempt recorded, not applied: aosdd.erpnext.quotation_draft; a duplicate effect is possible"),
    "AutomaticRetryRefused": ({"node_id": "n", "capability": "aosdd.erpnext.quotation_draft", "uncertain": True, "why": "x", "next": "y", "actor": "system:r"}, "node", "denied", "Automatic retry refused: aosdd.erpnext.quotation_draft; the outcome is uncertain"),
    "ReconciliationRequired": ({"node_id": "n", "capability": "aosdd.chatwoot.private_note", "operator": "aosdd", "lease_id": "l", "attempt": 1, "reason": "x"}, "node", "pending", "Reconciliation required: aosdd.chatwoot.private_note; the outcome of an earlier attempt is unknown"),
    "ReconciliationResolved": ({"node_id": "n", "lease_id": "l", "executed": True, "actor": "requester:aosdd-local", "principal": P, "reason": "x", "evidence": {"kind": "provider_record"}}, "node", "ok", "Reconciliation resolved by requester:aosdd-local: recorded as executed"),
    "AuthorityLeaseUnknown": ({"node_id": "n", "lease_id": "l"}, "node", "pending", "Authority lease state unknown for a step"),
    "CompensationRequested": ({"node_id": "n", "capability": "aosdd.chatwoot.private_note", "undo": "aosdd.chatwoot.note_delete", "policy": "COMPENSATE_DECLARED"}, "node", "info", "Compensation requested: aosdd.chatwoot.private_note (undo aosdd.chatwoot.note_delete)"),
    "CompensationAuthorized": ({"node_id": "n", "capability": "aosdd.chatwoot.private_note", "authority_basis": "operator:approver:default"}, "node", "ok", "Compensation authorized: aosdd.chatwoot.private_note (operator:approver:default)"),
    "CompensationExecuted": ({"node_id": "n", "capability": "aosdd.chatwoot.private_note", "undo": "u", "result": {}}, "node", "ok", "Compensation executed: aosdd.chatwoot.private_note"),
    "CompensationVerified": ({"node_id": "n", "capability": "aosdd.chatwoot.private_note", "method": "readback"}, "verification", "ok", "Compensation read back: aosdd.chatwoot.private_note"),
    "CompensationFailed": ({"node_id": "n", "capability": "aosdd.erpnext.quotation_draft", "reason": "undo raised: erpnext credential 401: {}"}, "node", "failed", "Compensation failed: aosdd.erpnext.quotation_draft"),
    "NodeCompensationFailed": ({"node_id": "n", "undo": "aosdd.erpnext.quotation_delete", "reason": "x"}, "node", "failed", "Step compensation failed (undo aosdd.erpnext.quotation_delete)"),
    "CompensationDenied": ({"node_id": "n", "capability": "aosdd.lago.invoice_metadata", "rule": "out_of_scope", "reason": "x", "effect_remains": True}, "node", "denied", "Compensation denied: aosdd.lago.invoice_metadata, rule out_of_scope; the effect remains"),
    "CompensationOperatorRequested": ({"principal": P, "scope": "default", "reason": "x", "node_id": None, "state": "succeeded"}, "lifecycle", "info", "Compensation requested by approver:default (scope default)"),
    "CompensationOperatorRefused": ({"principal": P, "scope": "default", "reason": "not_authorized"}, "lifecycle", "denied", "Compensation request refused: not_authorized"),
    "CompensationRetryRequested": ({"actor": "approver:default", "reason": "x", "node_ids": ["a", "b"]}, "node", "info", "Compensation retry requested by approver:default: 2 steps"),
    "EffectsRetained": ({"partially_completed": True, "retained_effects": [{"node_id": "a"}, {"node_id": "b"}], "incomplete_steps": [{"node_id": "c"}]}, "lifecycle", "info", "Effects retained: 2 retained, 1 step incomplete"),
    "MissionCancelled": ({"reason": "operator compensation: R9: undo the first UI mission"}, "lifecycle", "info", "Mission cancelled"),
    "ExecutionAuthorityRefused": ({"node_id": "n", "capability": "aosdd.plan.gate", "phase": "dispatch", "approved": True, "verdict": "deny", "rule": "expired", "reason": "x"}, "policy", "denied", "Execution authority refused: aosdd.plan.gate, rule expired"),
    "AuthorityDeclined": ({"node_id": "n", "capability": "aosdd.twenty.task_create", "verdict": "deny", "rule": "revoked"}, "policy", "denied", "Authority declined: aosdd.twenty.task_create, rule revoked"),
    "ScopeAuthorized": ({"node_id": "n", "capability": "aosdd.context.read", "rules": [], "grant_index": 0}, "policy", "info", "Scope authorized: aosdd.context.read"),
    "PrincipalRefused": ({"action": "approve", "reason": "tenant_mismatch", "claimed_id": "x"}, "policy", "denied", "Operator action refused (approve): tenant_mismatch"),
    "TestPolicyActive": ({"name": "pytest-suite-default", "reason": "x", "allow": ["*"], "compensate": [], "anonymous_operators": True, "digest": "sha256:" + "b" * 64}, "policy", "info", "Test policy active: pytest-suite-default"),
    "TestPolicyAuthorized": ({"node_id": "n", "capability": "c.x", "phase": "execute", "policy": "pytest-suite-default", "digest": "d"}, "policy", "info", "Test policy authorized: c.x (execute)"),
    "ReflectionEmitted": ({"success": True, "evidence": [], "failed_assumption": None}, "lifecycle", "info", "Reflection recorded (success: yes)"),
    "SchedulerConfigured": ({"effective_max_concurrency": 1, "scheduler": "serial"}, "plan", "info", "Scheduler configured: concurrency 1"),
}


@pytest.mark.parametrize("etype", sorted(CASES))
def test_category_outcome_and_summary(etype):
    payload, cat, out, summary = CASES[etype]
    assert category_of(etype) == cat, etype
    assert outcome_of(etype, payload) == out, etype
    assert summary_of(etype, payload) == summary, etype
    assert cat in CATEGORIES and out in OUTCOMES                      # the existing closed vocabularies, nothing new


@pytest.mark.parametrize("etype", ["AosddStepVerified", "AosddUndoVerified"])
def test_a_read_back_that_did_not_verify_is_failed_and_says_so(etype):
    p = dict(CASES[etype][0], status="MISMATCH")
    assert outcome_of(etype, p) == "failed" and "mismatch" in summary_of(etype, p)
    assert outcome_of("AosddUndoResult", {"ok": False, "step": "S1"}) == "failed" and summary_of("AosddUndoResult", {"ok": False, "step": "S1"}) == "Undo failed: step S1"


def test_a_read_back_with_the_writers_credential_names_the_kind_exactly_as_recorded():
    s = summary_of("AosddStepVerified", dict(CASES["AosddStepVerified"][0], read_only_kind="get-only-by-code-shared-writer-credential", same_secret_as_writer=True))
    assert s.endswith("verifier: get-only-by-code-shared-writer-credential")
    assert "application-enforced" not in s and "read-only" not in s


def test_an_undo_with_no_residual_does_not_mention_residual_effects():
    p = dict(CASES["AosddUndoVerified"][0], residual=[])
    assert "residual" not in summary_of("AosddUndoVerified", p)
    assert "residual" not in summary_of("AosddUndoVerified", {k: v for k, v in p.items() if k != "residual"})


# ───────────────────────── nothing external or free-text reaches a summary ─────────────────────────
FREE_TEXT = ("message", "why", "detail", "reason", "next", "state", "action", "consequence", "residual_risk_text", "error", "result")


@pytest.mark.parametrize("etype", sorted(CASES))
def test_free_text_fields_never_reach_a_summary(etype):
    payload = dict(CASES[etype][0])
    base = summary_of(etype, payload)
    for k in FREE_TEXT:
        if k in payload or k in ("message", "why", "detail", "reason", "next"):
            payload[k] = "SENTINEL-FREE-TEXT <i>x</i>"
    s = summary_of(etype, payload)
    assert "SENTINEL" not in s and s == base, (etype, s)


HOSTILE = "<img src=x onerror=alert(1)>\n\x00\x1b[31m" + "A" * 500


@pytest.mark.parametrize("etype", sorted(CASES))
def test_a_summary_is_bounded_and_has_no_markup_or_control_characters_whatever_the_fields_hold(etype):
    payload = {k: (HOSTILE if isinstance(v, str) else v) for k, v in CASES[etype][0].items()}
    for k in ("step", "app", "capability", "native_id", "rule", "kind", "outcome", "claim", "status", "read_only_kind", "phase", "actor", "authority_basis", "undo", "policy", "name", "scope"):
        if k in payload:
            payload[k] = HOSTILE
    payload["principal"] = {"id": HOSTILE}
    s = summary_of(etype, payload)
    assert len(s) <= 240 and not re.search(r"[<>\x00-\x1f\x7f]", s), (etype, s)


def test_a_non_dict_payload_and_missing_fields_are_survived():
    for etype in CASES:
        for p in ({}, {"principal": "x"}, {"step": None, "checks": "no"}):
            s = summary_of(etype, p)
            assert isinstance(s, str) and 0 < len(s) <= 240
            assert category_of(etype) in CATEGORIES and outcome_of(etype, p) in OUTCOMES


def test_unknown_types_keep_todays_behaviour():
    assert (category_of("SomethingNew"), outcome_of("SomethingNew", {}), summary_of("SomethingNew", {"x": 1})) == ("other", "info", "SomethingNew")


def test_types_that_were_already_known_are_unchanged():
    assert summary_of("NodeSucceeded", {"capability": "c"}) == "Step succeeded: c" and outcome_of("NodeSucceeded", {}) == "ok"
    assert summary_of("MissionOutcome", {"kind": "success"}) == "Outcome recorded: success" and outcome_of("MissionOutcome", {"success": False}) == "failed"


def test_the_principal_that_is_a_record_is_the_actor():
    assert actor_of({"principal": P}) == {"available": True, "value": "approver:default"}
    assert actor_of({"principal": {"id": 5}})["available"] is False and actor_of({})["available"] is False
    assert actor_of({"actor": "system:x", "principal": P})["value"] == "system:x"


# ───────────────────────── coverage: every type the integration can emit has a row ─────────────────────────
def test_every_aosdd_event_type_in_the_integration_source_is_classified():
    root = os.path.join(os.path.dirname(__file__), "..", "agentic_os", "integrations", "aosdd")
    found = set()
    for f in os.listdir(root):
        if f.endswith(".py"):
            found |= set(re.findall(r'"(Aosdd[A-Z][A-Za-z]+)"', open(os.path.join(root, f)).read()))
    found -= {"AosddConfig"}                                          # a class name that is quoted in a type hint, not an event
    missing = sorted(t for t in found if category_of(t) == "other")
    assert missing == [], f"event types with no Timeline row: {missing}"


def test_every_operator_relevant_base_event_has_a_row():
    for t in ("StaleResultRecorded", "AutomaticRetryRefused", "RedispatchRefused", "ReconciliationRequired", "ReconciliationResolved", "CompensationOperatorRequested",
              "CompensationOperatorRefused", "CompensationDenied", "CompensationAuthorized", "CompensationExecuted", "CompensationVerified", "CompensationFailed",
              "MissionCancelled", "ExecutionAuthorityRefused", "PrincipalRefused", "TestPolicyActive", "TestPolicyAuthorized", "EffectsRetained", "ScopeAuthorized"):
        assert category_of(t) != "other" and summary_of(t, {}) != t, t


# ───────────────────────── through the service: schema-valid, cited, unchanged contract ─────────────────────────
def test_the_timeline_rows_for_these_events_validate_against_the_unchanged_schema(tmp_path, monkeypatch):
    store = MemoryEventStore()
    store.append("MissionCreated", "m1", {"goal": "g", "policy_refs": [], "tenant": None})
    for etype, (payload, *_r) in CASES.items():
        store.append(etype, "m1", payload)
    svc, _sink, _perm = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    t = svc.timeline(owner(), "m1", limit=500)
    jsonschema.validate(t, timeline_schema())
    rows = {e["type"]: e for e in t["entries"]}
    for etype, (payload, cat, out, summary) in CASES.items():
        r = rows[etype]
        assert (r["category"], r["outcome"], r["summary"]) == (cat, out, summary), etype
        assert r["cite"]["event_sha256"].startswith("sha256:")
    assert rows["AosddRedispatchAuthorized"]["actor"] == {"available": True, "value": "approver:default"}


def test_read_back_events_are_evidence_rows_with_the_verifier_kind_as_recorded(tmp_path):
    store = MemoryEventStore()
    store.append("MissionCreated", "m1", {"goal": "g", "policy_refs": [], "tenant": None})
    store.append("AosddStepVerified", "m1", CASES["AosddStepVerified"][0])
    store.append("AosddUndoVerified", "m1", CASES["AosddUndoVerified"][0])
    svc, _s, _p = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    lst = svc.evidence_list(owner(), "m1")
    items = {i["summary"]: i for i in lst["items"]}
    assert len(lst["items"]) == 2 and all(i["kind"] == "verification" for i in lst["items"])
    assert lst["coverage"]["verifications"] == 2
    one = svc.evidence_inspect(owner(), "m1", lst["items"][1]["id"])
    assert one["content"]["read_only_kind"] == "get-only-by-code-shared-writer-credential" and one["content"]["same_secret_as_writer"] is True
    assert any("verifier: server-enforced" in s for s in items) and any("get-only-by-code-shared-writer-credential" in s for s in items)
