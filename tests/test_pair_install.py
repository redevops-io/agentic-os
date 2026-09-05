"""Governed PAIR install — proposal, approval gate, saga rollback, guided model step."""
import pytest

from agentic_os.mission.store import EventStore
from agentic_os.mission.device_posture import DeviceFacts
from agentic_os.mission.pair import PairStatus
from agentic_os.mission.inference_posture import derive_inference_posture, NetworkTrust
from agentic_os.mission.pair_install import (
    propose_pair_install, request_pair_install, approve_pair_install,
    execute_pair_install, pair_install_status, StubPairRunner,
    PairInstallNotApproved, PairInstallBlocked,
    S_APPROVED, S_REJECTED, S_COMPLETED, S_ROLLED_BACK, S_AWAITING, DEFAULT_MODEL,
)

GPU_BOX = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                      containment_supported=True, gpu="NVIDIA RTX 4090")
THIN = DeviceFacts(platform="windows-amd64", container_runtime="docker", containment_supported=True)

def _capable_posture():
    return derive_inference_posture(GPU_BOX, pair=PairStatus(available=False),
                                    network_trust=NetworkTrust.TRUSTED_HOME_LAN)


def test_proposal_lists_components_and_is_gated():
    p = propose_pair_install(_capable_posture())
    assert p.eligible and p.requires_approval
    assert "Ollama" in p.components and p.model == DEFAULT_MODEL
    assert p.network_access == "local network only"
    assert "local network only" in p.as_notice() and "GB" in p.as_notice()


def test_thin_device_proposal_is_blocked():
    thin_posture = derive_inference_posture(THIN, total_memory_mb=2048)
    p = propose_pair_install(thin_posture)
    assert not p.eligible
    with pytest.raises(PairInstallBlocked):
        request_pair_install(EventStore(), p)


def test_park_approve_execute_installs_with_model_pull():
    store = EventStore()
    p = propose_pair_install(_capable_posture())
    iid = request_pair_install(store, p)
    assert pair_install_status(store, iid) == S_AWAITING
    with pytest.raises(PairInstallNotApproved):                 # cannot run while parked
        execute_pair_install(store, iid, p, runner=StubPairRunner())
    assert approve_pair_install(store, iid, actor="alex") == S_APPROVED
    runner = StubPairRunner()
    result = execute_pair_install(store, iid, p, runner=runner)
    assert result.available
    assert runner.steps == ["install_pair", "install_ollama",
                            f"ensure_model:{DEFAULT_MODEL}", "start_services", "health"]
    assert pair_install_status(store, iid) == S_COMPLETED


def test_rejected_install_never_runs():
    store = EventStore()
    p = propose_pair_install(_capable_posture())
    iid = request_pair_install(store, p)
    assert approve_pair_install(store, iid, decision="cloud") == S_REJECTED
    with pytest.raises(PairInstallNotApproved):
        execute_pair_install(store, iid, p, runner=StubPairRunner())


def test_failure_rolls_back_in_reverse():
    store = EventStore()
    p = propose_pair_install(_capable_posture())
    iid = request_pair_install(store, p)
    approve_pair_install(store, iid)
    runner = StubPairRunner(fail_at=f"ensure_model:{DEFAULT_MODEL}")   # model download fails
    result = execute_pair_install(store, iid, p, runner=runner)
    assert not result.available
    assert runner.undone == ["uninstall"]                        # single rollback
    assert pair_install_status(store, iid) == S_ROLLED_BACK
