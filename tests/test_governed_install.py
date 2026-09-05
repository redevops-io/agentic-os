"""Governed install saga — HITL approval, saga/undo teardown, and replay over the ledger."""
import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest
from agentic_os.mission.device_posture import DeviceFacts, derive
from agentic_os.mission.provisioning import resolve
from agentic_os.mission.store import EventStore
from agentic_os.mission.event_backends import DuckDBEventStore
from agentic_os.mission.installer import StubDeployer
from agentic_os.mission.governed_install import (
    request_install, approve_install, execute_install, install_status,
    InstallNotApproved, InstallAlreadyDone,
    S_AWAITING, S_APPROVED, S_REJECTED, S_COMPLETED, S_ROLLED_BACK,
)

VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                       disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)
# LOCAL_CONTAINER (ALLOW) — no approval needed
SANDBOX = CapabilitySpec(name="a.run", operator="acap", provides=["alpha"], isolation_class="sandbox")
# HOST_PROCESS (REVIEW) — needs approval, still installable
INPROC = CapabilitySpec(name="b.run", operator="bcap", provides=["beta"], isolation_class="in_process")
CATALOG = [CapabilityManifest("acap", [SANDBOX]), CapabilityManifest("bcap", [INPROC])]

def _ok_fetch(url, timeout):
    return {"operator": url.split("/")[-2] if "/" in url else "op", "capabilities": []}

def _plan(outcome):
    return resolve(outcome, CATALOG, derive(VERIFIED))

def _stub():
    return StubDeployer(endpoints={"acap": "http://stub/acap", "bcap": "http://stub/bcap"})


def test_needs_approval_parks_then_runs_after_approval():
    store = EventStore()
    plan = _plan("do beta")                              # in_process ⇒ needs approval
    req = request_install(store, plan)
    assert req.status == S_AWAITING and "b.run" in req.needs_approval
    with pytest.raises(InstallNotApproved):             # cannot run while parked
        execute_install(store, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch)
    assert approve_install(store, req.install_id, actor="alex") == S_APPROVED
    r = execute_install(store, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch,
                        has_credential=lambda n: True)
    assert r.ok and install_status(store, req.install_id) == S_COMPLETED


def test_rejected_install_never_runs():
    store = EventStore()
    plan = _plan("do beta")
    req = request_install(store, plan)
    assert approve_install(store, req.install_id, decision="deny", actor="alex") == S_REJECTED
    with pytest.raises(InstallNotApproved):
        execute_install(store, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch)
    assert install_status(store, req.install_id) == S_REJECTED


def test_no_approval_needed_is_auto_approved():
    store = EventStore()
    plan = _plan("do alpha")                             # sandbox ⇒ LOCAL_CONTAINER, no approval
    req = request_install(store, plan)
    assert req.status == S_APPROVED and req.needs_approval == ()
    r = execute_install(store, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch,
                        has_credential=lambda n: True)
    assert r.ok and install_status(store, req.install_id) == S_COMPLETED


def test_failure_triggers_saga_teardown_and_rollback():
    store = EventStore()
    plan = _plan("do alpha")
    req = request_install(store, plan)
    dep = _stub()
    def boom(url, timeout):
        raise RuntimeError("verify unreachable")
    r = execute_install(store, req.install_id, plan, deployer=dep, fetch=boom,
                        has_credential=lambda n: True)
    assert not r.ok
    assert dep.torn_down == ["acap"]                    # the stood-up module was compensated
    assert install_status(store, req.install_id) == S_ROLLED_BACK


def test_completed_install_is_idempotent():
    store = EventStore()
    plan = _plan("do alpha")
    req = request_install(store, plan)
    execute_install(store, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch,
                    has_credential=lambda n: True)
    with pytest.raises(InstallAlreadyDone):
        execute_install(store, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch)


def test_state_survives_restart_via_durable_fold(tmp_path):
    path = str(tmp_path / "ledger.duckdb")
    s1 = DuckDBEventStore(path)
    plan = _plan("do alpha")
    req = request_install(s1, plan)
    execute_install(s1, req.install_id, plan, deployer=_stub(), fetch=_ok_fetch,
                    has_credential=lambda n: True)
    s1.close()
    s2 = DuckDBEventStore(path)                          # "restart" — reopen the same ledger
    assert install_status(s2, req.install_id) == S_COMPLETED
