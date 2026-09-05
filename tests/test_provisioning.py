"""Install-planner tests — outcome → capabilities → modules, gated by device posture.

Offline and deterministic: a synthetic capability catalog + synthesised device postures.
The planner reuses CapabilitySpec/CapabilityManifest (no new capability model) and the
posture from P0.2 as the gate.
"""
from dataclasses import replace

import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest, NodeCost
from agentic_os.mission.device_posture import DeviceFacts, Decision, ExecutionClass, derive
from agentic_os.mission.provisioning import (
    InstallPlan, InstallBlocked, Topology, execution_class_for,
    resolve, require_installable, default_matcher,
)

VERIFIED = DeviceFacts(
    platform="linux-x86_64", container_runtime="docker",
    disk_encryption=True, secure_credential_store=True,
    network_exposure="private", containment_supported=True)

# github.review — contained (sandbox → LOCAL_CONTAINER), needs a token, cheap.
GITHUB = CapabilitySpec(
    name="github.review", operator="devtools", provides=["code_review"],
    isolation_class="sandbox", secrets=["github_token"], cost=NodeCost(usd=0.02))
# crm.followup — must run in-process (→ HOST_PROCESS), handles PII, approval-gated.
CRM = CapabilitySpec(
    name="crm.followup", operator="crm", provides=["customer_followup"],
    isolation_class="in_process", secrets=["salesforce_key"], approval_required=True,
    data_classifications=["pii"], cost=NodeCost(usd=0.10))
# notes.local — undeclared isolation (→ HOST_PROCESS by conservative default), free, no creds.
NOTES = CapabilitySpec(name="notes.local", operator="notes", provides=["local_notes"])

CATALOG = [CapabilityManifest("devtools", [GITHUB]),
           CapabilityManifest("crm", [CRM]),
           CapabilityManifest("notes", [NOTES])]


def test_isolation_maps_to_execution_class():
    assert execution_class_for(GITHUB) is ExecutionClass.LOCAL_CONTAINER
    assert execution_class_for(CRM) is ExecutionClass.HOST_PROCESS
    assert execution_class_for(NOTES) is ExecutionClass.HOST_PROCESS   # unknown ⇒ restricted tier


def test_matcher_hits_on_provided_outcome():
    assert default_matcher("help me with code_review please", GITHUB)
    assert not default_matcher("book me a flight", GITHUB)


def test_resolves_a_contained_outcome_on_a_verified_device():
    p = resolve("help me with code_review on this repo", CATALOG, derive(VERIFIED))
    assert p.to_install == ("devtools",)
    assert p.credentials_needed == ("github_token",)
    assert p.installable and p.needs_approval == ()
    assert not p.storage_needed                      # github.review declares no data class
    assert p.est_cost_usd_milli == 20                # 0.02 usd → 20 milli
    (rc,) = p.resolved
    assert rc.decision is Decision.ALLOW and rc.execution_class is ExecutionClass.LOCAL_CONTAINER


def test_host_tier_outcome_needs_approval_but_is_installable_on_verified():
    p = resolve("prepare a customer_followup", CATALOG, derive(VERIFIED))
    assert p.installable                               # HOST_PROCESS is REVIEW (issuable), not DENY
    assert p.needs_approval == ("crm.followup",)
    assert p.storage_needed                            # handles PII
    assert p.credentials_needed == ("salesforce_key",)
    require_installable(p)                             # does not raise


def test_host_tier_outcome_is_blocked_when_posture_denies():
    # no disk encryption ⇒ HOST_PROCESS DENY, so the in-process capability can't run here
    weak = derive(replace(VERIFIED, disk_encryption=False))
    p = resolve("prepare a customer_followup", CATALOG, weak)
    assert not p.installable
    assert p.blocked and p.blocked[0][0] == "crm.followup"
    with pytest.raises(InstallBlocked):
        require_installable(p)


def test_already_installed_modules_are_not_reinstalled():
    p = resolve("code_review", CATALOG, derive(VERIFIED), installed={"devtools"})
    assert p.already_installed == ("devtools",)
    assert p.to_install == ()


def test_unmatched_outcome_is_empty_and_installable():
    p = resolve("translate this document", CATALOG, derive(VERIFIED))
    assert p.resolved == () and p.to_install == ()
    assert p.installable
    require_installable(p)                             # nothing to block


def test_cost_aggregates_across_matched_capabilities():
    # an outcome that names two provided outcomes matches both capabilities
    p = resolve("do code_review then a customer_followup", CATALOG, derive(VERIFIED))
    assert set(p.to_install) == {"devtools", "crm"}
    assert p.est_cost_usd_milli == 120                # 20 + 100
    assert set(p.credentials_needed) == {"github_token", "salesforce_key"}


def test_plan_id_is_content_addressed():
    a = resolve("code_review", CATALOG, derive(VERIFIED))
    b = resolve("code_review", CATALOG, derive(VERIFIED))
    assert a.plan_id == b.plan_id and a.plan_id.startswith("rcv1:")
    c = resolve("customer_followup", CATALOG, derive(VERIFIED))
    assert c.plan_id != a.plan_id


def test_topology_is_carried_on_the_plan():
    p = resolve("code_review", CATALOG, derive(VERIFIED), topology=Topology.MEMBER)
    assert p.topology is Topology.MEMBER
    assert isinstance(p, InstallPlan)
