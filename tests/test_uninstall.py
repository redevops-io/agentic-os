"""Uninstall — reverse the install: teardown modules, optional data/local-AI removal, ledger-recorded."""
import pytest

from agentic_os.mission.store import EventStore
from agentic_os.mission.installer import StubDeployer
from agentic_os.mission.pair_install import StubPairRunner
from agentic_os.mission.onboarding import provision_local_secret
from agentic_os.mission.uninstall import (
    uninstall, installed_modules, UninstallNotConfirmed,
    MODULE_EVENT, SECRETS_EVENT, LOCAL_AI_EVENT, DONE_EVENT,
)


def test_refuses_without_confirmation():
    with pytest.raises(UninstallNotConfirmed):
        uninstall(EventStore(), deployer=StubDeployer(), modules=["crm"])


def test_tears_down_modules_and_records():
    store = EventStore()
    dep = StubDeployer()
    r = uninstall(store, deployer=dep, modules=["crm", "devtools"], confirmed=True)
    assert r.ok and r.torn_down == ("crm", "devtools")
    assert dep.torn_down == ["crm", "devtools"]
    assert r.kept_data                       # default keeps data
    types = [e.type for e in store.for_mission("__uninstall__")]
    assert types.count(MODULE_EVENT) == 2 and DONE_EVENT in types


def test_installed_modules_folds_from_the_ledger():
    store = EventStore()
    store.append("InstallCompleted", "install-1", {"modules": ["crm", "devtools"]})
    store.append("InstallCompleted", "install-2", {"modules": ["devtools", "notes"]})
    assert installed_modules(store) == ["crm", "devtools", "notes"]
    # uninstall with no explicit list uses the ledger
    dep = StubDeployer()
    uninstall(store, deployer=dep, confirmed=True)
    assert dep.torn_down == ["crm", "devtools", "notes"]


def test_keeps_credentials_by_default(tmp_path):
    pc = provision_local_secret("twenty_api_key", "tok-DO-NOT-STORE", secret_dir=str(tmp_path))
    uninstall(EventStore(), deployer=StubDeployer(), modules=[], secret_dir=str(tmp_path),
              confirmed=True)                # remove_data defaults False
    import os
    assert os.path.exists(pc.location)       # the key is preserved


def test_full_wipe_removes_credentials(tmp_path):
    import os
    pc = provision_local_secret("twenty_api_key", "tok-DO-NOT-STORE", secret_dir=str(tmp_path))
    store = EventStore()
    r = uninstall(store, deployer=StubDeployer(), modules=[], secret_dir=str(tmp_path),
                  remove_data=True, confirmed=True)
    assert not os.path.exists(pc.location)   # removed
    assert "twenty_api_key" in r.secrets_removed and not r.kept_data
    assert any(e.type == SECRETS_EVENT for e in store.for_mission("__uninstall__"))


def test_remove_local_ai_calls_pair_uninstall():
    store = EventStore()
    runner = StubPairRunner()
    r = uninstall(store, deployer=StubDeployer(), modules=[], pair_runner=runner,
                  remove_local_ai=True, confirmed=True)
    assert r.local_ai_removed and runner.undone == ["uninstall"]
    assert any(e.type == LOCAL_AI_EVENT for e in store.for_mission("__uninstall__"))


def test_teardown_failure_is_recorded_not_raised():
    class FailingDeployer:
        def deploy(self, m): ...
        def teardown(self, m):
            if m == "crm":
                raise RuntimeError("stuck container")
    r = uninstall(EventStore(), deployer=FailingDeployer(), modules=["crm", "notes"], confirmed=True)
    assert not r.ok and r.failed == ("crm",) and r.torn_down == ("notes",)


def test_receipt_is_content_addressed():
    r = uninstall(EventStore(), deployer=StubDeployer(), modules=["crm"], confirmed=True)
    assert r.receipt_id.startswith("rcv1:")
