"""Registry / conformance / compensation model / demo allowlist for the document-editor operator."""
from __future__ import annotations

import importlib

import pytest

from agentic_os.app_kit import AppRegistry
from agentic_os.mission import compensation as comp
from agentic_os.mission.verify import FAILED_STATUSES, proves_execution, result_failed

man = importlib.import_module("document-editor.manifest")
oper = importlib.import_module("document-editor.operator")


@pytest.fixture(scope="module")
def op():
    return man.default_operator()


def spec(op, name):
    return op._specs[name]


def test_registers_conformantly_with_a_verifier_per_side_effecting_capability(op):
    reg = man.register(AppRegistry())
    m = reg.manifest
    assert m.name == "document-editor" and m.verified_capabilities() <= m.capability_names()
    side = {c.name for c in op.manifest.capabilities if c.side_effecting}
    assert side == {"document.convert", "document.export", "document.apply_patch", "document.publish"}
    assert m.verified_capabilities() == side
    assert m.deploy.approval == ("apply_patch", "publish")


def test_reads_do_not_have_side_effects_and_proposals_never_mutate(op):
    for n in ("connect_check", "inspect", "open_session", "propose_patch"):
        s = spec(op, f"document.{n}")
        assert not s.side_effecting and not s.approval_required, n


def test_writes_that_change_or_publish_a_document_require_human_approval(op):
    for n in ("apply_patch", "publish"):
        s = spec(op, f"document.{n}")
        assert s.side_effecting and s.approval_required, n
        assert s.required_authority, n


def test_apply_patch_is_preserve_never_automatically_undone(op):
    s = spec(op, "document.apply_patch")
    c = comp.coerce(s.compensation)
    assert c.disposition == comp.PRESERVE and not c.supported and s.undo is None
    assert comp.is_preserved(s) and comp.declared_undo(s) is None
    assert comp.lint_capability(s) is None


def test_publish_is_irreversible_with_a_note(op):
    s = spec(op, "document.publish")
    assert s.irreversible and "cannot be recalled" in s.irreversible_note
    assert comp.reversibility_of(s) is comp.Reversibility.IRREVERSIBLE and comp.lint_capability(s) is None


@pytest.mark.parametrize("n", ["convert", "export"])
def test_derived_artifacts_are_new_versioned_artifacts_with_no_destructive_undo(op, n):
    s = spec(op, f"document.{n}")
    c = comp.coerce(s.compensation)
    assert s.side_effecting and not s.approval_required and c.disposition == comp.PRESERVE and not c.supported
    assert comp.lint_capability(s) is None


def test_registry_level_compensation_problems_are_empty(op):
    assert comp.registry_problems(list(op._specs.values()), op._specs.get) == []


def test_network_and_data_classifications_are_declared_for_the_security_monitor(op):
    for s in op._specs.values():
        assert s.network and s.data_classifications == ["customer_confidential"], s.name


def test_write_capabilities_are_never_on_any_demo_auto_approve_allowlist(op):
    safe = oper.DEMO_AUTO_APPROVE_SAFE
    assert safe <= {s.name for s in op._specs.values()}
    for name in safe:
        s = spec(op, name)
        assert not s.side_effecting and not s.approval_required
    assert not {"document.apply_patch", "document.publish", "document.convert", "document.export"} & safe


def test_staged_results_are_not_execution_proof(op):
    h = op._handlers["document.apply_patch"]
    staged = h({"tenant": "acme", "proposal_id": "0" * 32})              # no _approval
    assert staged["status"] in ("pending_approval", "not_configured") and staged["executed"] is False
    assert not proves_execution(staged) and result_failed(staged, side_effecting=True)[0]
    pub = op._handlers["document.publish"]({"tenant": "acme", "doc_id": "a.docx"})
    assert pub["status"] == "pending_approval" and not proves_execution(pub)
    # with a configured service the unapproved call is the uniform staged shape (see test_secure_invoke)


def test_every_refusal_is_failure_shaped_so_the_verifier_can_never_read_it_as_success(mods):
    E = mods.errors
    for cls in (E.ConfigError, E.UnsupportedCapability, E.BackendUnavailable, E.BackendError, E.OutcomeUnknown,
                E.PolicyDenied, E.ApprovalRequired, E.StaleVersion, E.Conflict, E.InvalidPath, E.NotFound,
                E.TenantMismatch, E.SSRFBlocked, E.InvalidInput, E.VerificationFailed):
        r = cls("x").to_result()
        assert r["status"] in FAILED_STATUSES, cls.__name__
        assert result_failed(r, side_effecting=True)[0] and not proves_execution(r)
        assert r["executed"] is False


def test_forged_approval_on_the_in_process_path_still_requires_a_real_service(op):
    # without a configured service even an `_approval` marker cannot do anything: typed failure, not a write
    r = op._handlers["document.apply_patch"]({"tenant": "acme", "_approval": {"approved": True}, "proposal_id": "0" * 32,
                                              "idempotency_key": "idem-0000001", "expected_version": 1,
                                              "base_hash": "0" * 64, "backend": "onlyoffice", "patch_digest": "0" * 64})
    assert r["status"] == "not_configured"
