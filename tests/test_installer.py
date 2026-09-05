"""Installer tests (offline) — execute an InstallPlan with a StubDeployer + injected fetch.

The plan is built by the real P0.3 resolver so posture-gating flows end-to-end; only the
deploy backend and the /capabilities fetch are stubbed.
"""
from dataclasses import replace

import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest, NodeCost
from agentic_os.mission.device_posture import DeviceFacts, derive
from agentic_os.mission.provisioning import resolve
from agentic_os.mission.installer import (
    StubDeployer, InstallReceipt, install, default_has_credential,
    MODULE_EVENT, DONE_EVENT,
)
from agentic_os.mission.installer import InstallPlan  # re-export check
from agentic_os.mission.provisioning import InstallBlocked
from agentic_os.mission.store import EventStore

VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                       disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)
GITHUB = CapabilitySpec(name="github.review", operator="devtools", provides=["code_review"],
                        isolation_class="sandbox", secrets=["github_token"], cost=NodeCost(usd=0.02))
CRM = CapabilitySpec(name="crm.followup", operator="crm", provides=["customer_followup"],
                     isolation_class="in_process", secrets=["salesforce_key"])
CATALOG = [CapabilityManifest("devtools", [GITHUB]), CapabilityManifest("crm", [CRM])]

# a fetch that returns a valid /capabilities manifest for the deployed module
def _ok_fetch(url, timeout):
    return {"operator": "devtools", "capabilities": [{"name": "github.review", "operator": "devtools"}]}

def _has(name):        # github_token present, everything else missing
    return name == "github_token"


def _github_plan():
    return resolve("help with code_review", CATALOG, derive(VERIFIED))


def test_installs_verifies_and_registers_for_federation():
    dep = StubDeployer(endpoints={"devtools": "http://stub/devtools"})
    r = install(_github_plan(), deployer=dep, fetch=_ok_fetch, has_credential=_has)
    assert isinstance(r, InstallReceipt) and r.ok and r.ready
    assert dep.deployed == ["devtools"]
    (o,) = r.installed
    assert o.verified and o.operator == "devtools"
    assert o.credentials_obtained == ("github_token",) and o.credentials_missing == ()
    assert r.modules_yaml == {"devtools": "http://stub/devtools"}   # federation registration


def test_missing_credential_installs_but_is_not_ready():
    dep = StubDeployer(endpoints={"devtools": "http://stub/devtools"})
    r = install(_github_plan(), deployer=dep, fetch=_ok_fetch, has_credential=lambda n: False)
    assert r.ok                       # the module came up and verified
    assert not r.ready                # ...but its credential is missing
    assert r.installed[0].credentials_missing == ("github_token",)


def test_deploy_failure_is_a_named_outcome_not_a_crash():
    dep = StubDeployer(endpoints={"devtools": "http://stub/devtools"}, fail=("devtools",))
    r = install(_github_plan(), deployer=dep, fetch=_ok_fetch, has_credential=_has)
    assert not r.ok
    assert r.installed[0].verified is False
    assert r.installed[0].reason.startswith("deploy_failed")
    assert r.modules_yaml == {}       # nothing registered


def test_verify_failure_is_recorded_and_not_registered():
    def boom(url, timeout):
        raise RuntimeError("unreachable")
    dep = StubDeployer(endpoints={"devtools": "http://stub/devtools"})
    r = install(_github_plan(), deployer=dep, fetch=boom, has_credential=_has)
    assert not r.ok
    assert r.installed[0].reason.startswith("verify_failed")
    assert r.modules_yaml == {}


def test_posture_blocked_plan_is_refused_at_the_door():
    weak = derive(replace(VERIFIED, disk_encryption=False))       # HOST_PROCESS denied
    plan = resolve("prepare a customer_followup", CATALOG, weak)
    assert not plan.installable
    with pytest.raises(InstallBlocked):
        install(plan, deployer=StubDeployer(), fetch=_ok_fetch)


def test_already_installed_modules_are_skipped():
    plan = resolve("code_review", CATALOG, derive(VERIFIED), installed={"devtools"})
    dep = StubDeployer(endpoints={"devtools": "http://stub/devtools"})
    r = install(plan, deployer=dep, fetch=_ok_fetch, has_credential=_has)
    assert dep.deployed == [] and r.installed == ()
    assert r.skipped == ("devtools",)


def test_events_are_recorded_for_audit_and_replay():
    store = EventStore()
    install(_github_plan(), deployer=StubDeployer(endpoints={"devtools": "http://stub/devtools"}),
            store=store, fetch=_ok_fetch, has_credential=_has)
    types = [e.type for e in store.for_mission("__install__")]
    assert MODULE_EVENT in types and DONE_EVENT in types


def test_receipt_is_content_addressed():
    dep = StubDeployer(endpoints={"devtools": "http://stub/devtools"})
    r = install(_github_plan(), deployer=dep, fetch=_ok_fetch, has_credential=_has)
    assert r.receipt_id.startswith("rcv1:")
    assert r.receipt_id == install(_github_plan(),
                                   deployer=StubDeployer(endpoints={"devtools": "http://stub/devtools"}),
                                   fetch=_ok_fetch, has_credential=_has).receipt_id


def test_default_has_credential_is_conservative_for_absent_env():
    assert default_has_credential("definitely_not_set_ZZZ_123") is False
