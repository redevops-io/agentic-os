"""Classification matrix, UNLABELED => CONFIDENTIAL, stricter-only overrides, signed policy, approval binding."""
from __future__ import annotations

import time

import pytest

SHA = "a" * 64
OPERATOR = "document-editor"


@pytest.fixture()
def C(mods, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "unit-test-approval-secret-0123456789abcdef")
    return mods.classification


def pol(C, zone="PRIVATE_NETWORK", **kw):
    return C.TenantPolicy.build("acme", {"onlyoffice": zone}, **kw)


def token(C, kit, doc="d.docx", sha=SHA, backend="onlyoffice", tenant="acme", **kw):
    return kit.mint(OPERATOR, C.TRANSMIT_CAPABILITY, C.transmit_inputs(tenant, doc, sha, backend), mission_id=None,
                    node_id=None, **kw)


def decide(C, label, zone="PRIVATE_NETWORK", backend="onlyoffice", tok=None, policy=None, guard=None):
    return C.decide_transmit(policy or pol(C, zone), doc_id="d.docx", doc_sha256=SHA, label=label, backend=backend,
                             approval_token=tok, replay_guard=guard)


# the full default matrix: label x zone x approval
@pytest.mark.parametrize("label,zone,approved,allowed", [
    ("PUBLIC", "LOCAL", False, True), ("PUBLIC", "PRIVATE_NETWORK", False, True), ("PUBLIC", "EXTERNAL", False, True),
    ("INTERNAL", "LOCAL", False, True), ("INTERNAL", "PRIVATE_NETWORK", False, True),
    ("INTERNAL", "EXTERNAL", False, False), ("INTERNAL", "EXTERNAL", True, False),
    ("CONFIDENTIAL", "LOCAL", False, False), ("CONFIDENTIAL", "LOCAL", True, True),
    ("CONFIDENTIAL", "PRIVATE_NETWORK", False, False), ("CONFIDENTIAL", "PRIVATE_NETWORK", True, True),
    ("CONFIDENTIAL", "EXTERNAL", True, False),
    ("RESTRICTED", "LOCAL", False, False), ("RESTRICTED", "LOCAL", True, False),
    ("RESTRICTED", "PRIVATE_NETWORK", True, False), ("RESTRICTED", "EXTERNAL", True, False),
])
def test_default_matrix(C, kit, label, zone, approved, allowed):
    d = decide(C, label, zone, tok=token(C, kit) if approved else None)
    assert d.allowed is allowed, d.reason


@pytest.mark.parametrize("label", [None, "", "  ", "unknown", "SECRET", "public-ish", 5])
def test_unlabeled_or_unrecognised_is_confidential_fail_closed(C, kit, label):
    d = decide(C, label)
    assert d.effective_label == "CONFIDENTIAL" and not d.allowed and d.needs_approval
    assert decide(C, label, tok=token(C, kit)).allowed                    # only with the bound approval


def test_label_case_is_normalised(C):
    assert decide(C, "public").allowed and decide(C, " Internal ").allowed


def test_restricted_is_never_sent_regardless_of_everything(C, kit):
    for z in C.ZONES:
        assert not decide(C, "RESTRICTED", z, tok=token(C, kit)).allowed


def test_backend_must_be_allowlisted(C, kit):
    d = decide(C, "PUBLIC", backend="collabora")
    assert not d.allowed and "allowlist" in d.reason
    d = C.decide_transmit(C.TenantPolicy.build("acme", {}), doc_id="d", doc_sha256=SHA, label="PUBLIC", backend="onlyoffice")
    assert not d.allowed


# ── approval token binding ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("override", [{"sha": "b" * 64}, {"backend": "collabora"}, {"doc": "other.docx"},
                                      {"tenant": "globex"}])
def test_transmit_approval_is_bound_to_hash_backend_document_tenant(C, kit, override):
    tok = token(C, kit, **override)
    assert not decide(C, "CONFIDENTIAL", tok=tok).allowed


def test_transmit_approval_is_single_use_with_a_replay_guard(C, kit):
    from agentic_os.mission.approval import ReplayGuard
    g = ReplayGuard()
    tok = token(C, kit)
    assert decide(C, "CONFIDENTIAL", tok=tok, guard=g).allowed
    assert not decide(C, "CONFIDENTIAL", tok=tok, guard=g).allowed


def test_transmit_approval_expires(C, kit):
    assert not decide(C, "CONFIDENTIAL", tok=token(C, kit, ttl_s=-5)).allowed


def test_approval_for_another_capability_does_not_count(C, kit):
    tok = kit.mint(OPERATOR, "document.apply_patch", C.transmit_inputs("acme", "d.docx", SHA, "onlyoffice"),
                   mission_id=None, node_id=None)
    assert not decide(C, "CONFIDENTIAL", tok=tok).allowed


def test_garbage_and_forged_tokens(C):
    for t in ("", "x.y", "a" * 40, None, 5):
        assert not decide(C, "CONFIDENTIAL", tok=t).allowed


def test_no_approval_secret_means_no_approvals(C, kit, monkeypatch):
    tok = token(C, kit)
    monkeypatch.delenv("AGENTIC_OS_APPROVAL_SECRET")
    assert not decide(C, "CONFIDENTIAL", tok=tok).allowed


# ── overrides: stricter only unless signed ─────────────────────────────────────────────────────────────────
def test_tenant_may_tighten(C):
    p = pol(C, policy={"matrix": {"INTERNAL": {"zones": ["LOCAL"]}, "PUBLIC": {"zones": ["LOCAL", "PRIVATE_NETWORK"],
                                                                         "needs_approval": True}},
                       "default_label": "RESTRICTED"})
    assert not decide(C, "INTERNAL", "PRIVATE_NETWORK", policy=p).allowed
    assert not decide(C, "PUBLIC", "EXTERNAL", policy=p).allowed
    assert decide(C, None, policy=p).effective_label == "RESTRICTED"


@pytest.mark.parametrize("policy", [
    {"matrix": {"INTERNAL": {"zones": ["LOCAL", "PRIVATE_NETWORK", "EXTERNAL"]}}},          # widens zones
    {"matrix": {"CONFIDENTIAL": {"zones": ["LOCAL"], "needs_approval": False}}},            # drops approval
    {"matrix": {"RESTRICTED": {"zones": ["LOCAL"]}}},                                        # un-nevers RESTRICTED
    {"matrix": {"PUBLIC": {"zones": ["LOCAL"], "never": False}, "RESTRICTED": {"zones": []}}},
    {"default_label": "PUBLIC"}, {"default_label": "INTERNAL"}, {"default_label": "nonsense"},
    {"matrix": {"TOPSECRET": {"zones": []}}}, {"matrix": {"PUBLIC": {"zones": ["MOON"]}}},
])
def test_unsigned_loosening_is_rejected(C, mods, policy):
    with pytest.raises(mods.errors.ConfigError):
        pol(C, policy=policy)


KEY = "policy-signing-key-for-tests"


def signed(C, payload, key=KEY, tenant="acme", exp=None):
    payload = {"tenant": tenant, "expires": exp if exp is not None else time.time() + 3600, **payload}
    return {"payload": payload, "signature": C.sign_policy(payload, key)}


def test_signed_policy_can_loosen(C):
    sp = signed(C, {"matrix": {"INTERNAL": {"zones": ["LOCAL", "PRIVATE_NETWORK", "EXTERNAL"]}}})
    p = pol(C, "EXTERNAL", signed_policy=sp, signing_key=KEY)
    assert p.signed and decide(C, "INTERNAL", "EXTERNAL", policy=p).allowed


def test_signed_policy_cannot_send_restricted_without_explicit_flag(C, mods):
    sp = signed(C, {"matrix": {"RESTRICTED": {"zones": ["LOCAL"], "needs_approval": True}}})
    with pytest.raises(mods.errors.ConfigError, match="allow_restricted"):
        pol(C, signed_policy=sp, signing_key=KEY)
    sp = signed(C, {"allow_restricted": True, "matrix": {"RESTRICTED": {"zones": ["LOCAL"], "needs_approval": True}}})
    p = pol(C, "LOCAL", signed_policy=sp, signing_key=KEY)
    assert decide(C, "RESTRICTED", "LOCAL", policy=p).allowed is False        # still needs the bound approval


@pytest.mark.parametrize("mut", ["bad_sig", "other_tenant", "expired", "wrong_key", "tampered"])
def test_bad_signed_policies_are_rejected(C, mods, mut):
    sp = signed(C, {"matrix": {"INTERNAL": {"zones": ["LOCAL", "PRIVATE_NETWORK", "EXTERNAL"]}}},
                tenant="globex" if mut == "other_tenant" else "acme", exp=1 if mut == "expired" else None,
                key="a-different-key" if mut == "wrong_key" else KEY)
    if mut == "bad_sig":
        sp["signature"] = "0" * 64
    if mut == "tampered":
        sp["payload"]["matrix"]["INTERNAL"]["zones"].append("LOCAL")
        sp["payload"]["matrix"]["PUBLIC"] = {"zones": ["EXTERNAL"]}
    with pytest.raises(mods.errors.ConfigError):
        pol(C, signed_policy=sp, signing_key=KEY)


def test_signed_policy_without_a_key_is_rejected(C, mods, monkeypatch):
    monkeypatch.delenv("DOCEDITOR_POLICY_SIGNING_KEY", raising=False)
    sp = signed(C, {"matrix": {"INTERNAL": {"zones": ["EXTERNAL"]}}})
    with pytest.raises(mods.errors.ConfigError):
        pol(C, signed_policy=sp)


def test_decision_receipt_has_everything_an_auditor_needs(C):
    r = decide(C, "INTERNAL", "EXTERNAL").receipt()
    for k in ("allowed", "reason", "tenant", "doc_id", "doc_sha256", "backend", "zone", "effective_label"):
        assert k in r
    assert r["allowed"] is False
