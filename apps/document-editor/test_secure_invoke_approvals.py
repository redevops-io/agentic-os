"""Signed approvals over the authenticated /invoke: forged markers never execute; a valid token executes once."""
from __future__ import annotations

import pytest

ADMIN, ACME, GLOBEX = "admin-token-xxxxxxxxxxxxxxxx", "acme-token-yyyyyyyyyyyyyyyy", "globex-token-zzzzzzzzzzzzzz"   # == conftest


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def invoke(c, tok, cap, inputs=None, **kw):
    return c.post("/invoke", json={"capability": cap, "inputs": inputs or {}, **kw}, headers=H(tok) if tok else {})


# ── approvals over HTTP ──────────────────────────────────────────────────────────────────────────────────────
def _propose(client, kit, env, text="Price is 100 EUR."):
    env.put("acme", "c.docx", kit.make_docx(["Hello", text]))
    return invoke(client, ACME, "document.propose_patch", {
        "doc_id": "c.docx", "ops": [{"type": "replace_text", "search": "100 EUR", "replace": "120 EUR"}]}).json()["result"]


def _apply_inputs(p):
    return {"proposal_id": p["proposal_id"], "base_hash": p["base_hash"], "backend": p["backend"],
            "patch_digest": p["patch_digest"], "expected_version": p["base_version"]}


def test_forged_approval_marker_does_not_execute(client, env, kit):
    p = _propose(client, kit, env)
    assert p["status"] == "ok"
    for forged in ({"_approval": True}, {"_approval": {"approved": True}}, {"Approval": True}, {"__approval": 1}):
        r = invoke(client, ACME, "document.apply_patch", {**_apply_inputs(p), **forged}, idempotency_key="idem-forged-1").json()["result"]
        assert r["status"] == "pending_approval" and r["executed"] is False
    assert "apply_edit" not in env.backend.calls                          # zero backend writes before approval


def test_garbage_and_foreign_approval_tokens_do_not_execute(client, env, kit):
    p = _propose(client, kit, env)
    inputs = _apply_inputs(p)
    other = kit.mint("document-editor", "document.apply_patch", {**inputs, "patch_digest": "0" * 64})
    for tok in ("garbage", "a.b", other, kit.mint("billing", "document.apply_patch", inputs)):
        r = invoke(client, ACME, "document.apply_patch", inputs, approval_token=tok, mission_id="m-1", node_id="n-1",
                   idempotency_key="idem-bad-token-1").json()["result"]
        assert r["status"] == "pending_approval"
    assert "apply_edit" not in env.backend.calls


def test_valid_approval_token_executes_once_and_replay_is_staged(client, env, kit):
    p = _propose(client, kit, env)
    inputs = _apply_inputs(p)
    tok = kit.mint("document-editor", "document.apply_patch", inputs)
    r = invoke(client, ACME, "document.apply_patch", inputs, approval_token=tok, mission_id="m-1", node_id="n-1",
               idempotency_key="idem-good-token-1").json()["result"]
    assert r["status"] == "ok" and r["verified"] is True and r["new_version"] == 2
    # same token, different idempotency key: single-use => staged, no second write
    r2 = invoke(client, ACME, "document.apply_patch", inputs, approval_token=tok, mission_id="m-1", node_id="n-1",
                idempotency_key="idem-good-token-2").json()["result"]
    assert r2["status"] == "pending_approval"
    assert env.backend.calls.count("apply_edit") == 1


def test_approval_token_for_one_tenant_document_cannot_be_used_by_another_tenant(client, env, kit):
    p = _propose(client, kit, env)
    inputs = _apply_inputs(p)
    tok = kit.mint("document-editor", "document.apply_patch", inputs)
    r = invoke(client, GLOBEX, "document.apply_patch", inputs, approval_token=tok, mission_id="m-1", node_id="n-1",
               idempotency_key="idem-cross-tenant-1").json()["result"]
    assert r["status"] != "ok" and "apply_edit" not in env.backend.calls
