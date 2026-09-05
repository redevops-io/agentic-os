"""End-to-end bootstrap — posture → LLM → plan → governed install → notices, in one call."""
import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest
from agentic_os.mission.device_posture import DeviceFacts
from agentic_os.mission.store import EventStore
from agentic_os.mission.installer import StubDeployer
from agentic_os.mission.onboarding import TwentyKeyProvisioner
from agentic_os.mission.bootstrap import (
    bootstrap, render_summary, BLOCKED, AWAITING_APPROVAL, INSTALLED,
)

VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                       disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)
BLOCKED_DEVICE = DeviceFacts()                        # all unknown ⇒ posture BLOCKED

SANDBOX = CapabilitySpec(name="a.run", operator="acap", provides=["alpha"], isolation_class="sandbox")
INPROC = CapabilitySpec(name="b.run", operator="bcap", provides=["beta"], isolation_class="in_process")
CRM = CapabilitySpec(name="crm.sync", operator="crm", provides=["crm_sync"],
                     isolation_class="sandbox", secrets=["twenty_api_key"])
CATALOG = [CapabilityManifest("acap", [SANDBOX]), CapabilityManifest("bcap", [INPROC]),
           CapabilityManifest("crm", [CRM])]

def _ok_fetch(url, timeout):
    return {"operator": "op", "capabilities": []}

def _stub():
    return StubDeployer(endpoints={"acap": "http://s/acap", "bcap": "http://s/bcap", "crm": "http://s/crm"})


def test_blocked_device_stops_before_any_install():
    store = EventStore()
    r = bootstrap("do alpha", CATALOG, store=store, deployer=_stub(),
                  prober=lambda: BLOCKED_DEVICE, env={})
    assert r.status == BLOCKED
    assert any("can't run" in n.lower() for n in r.notices)
    assert not any(e.type == "InstallRequested" for e in store.all())   # nothing was installed


def test_happy_path_standalone_installs():
    store = EventStore()
    r = bootstrap("do alpha", CATALOG, store=store, deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: VERIFIED, env={}, has_credential=lambda n: True)
    assert r.status == INSTALLED and r.ok and r.install_id
    assert r.receipt.ok


def test_review_tier_parks_for_approval():
    store = EventStore()
    r = bootstrap("do beta", CATALOG, store=store, deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: VERIFIED, env={}, has_credential=lambda n: True)
    assert r.status == AWAITING_APPROVAL and r.install_id and r.receipt is None
    assert any("approval" in n.lower() for n in r.notices)


def test_review_tier_auto_approve_runs():
    store = EventStore()
    r = bootstrap("do beta", CATALOG, store=store, deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: VERIFIED, env={}, has_credential=lambda n: True, auto_approve=True)
    assert r.status == INSTALLED


def test_default_llm_guided_when_no_creds():
    store = EventStore()
    r = bootstrap("do alpha", CATALOG, store=store, deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: VERIFIED, env={}, has_credential=lambda n: True)
    assert r.llm.source == "guided"
    assert any("LLM setup needed" in n for n in r.notices)


def test_twenty_key_provisioned_through_bootstrap(tmp_path):
    store = EventStore()
    r = bootstrap("keep crm_sync running", CATALOG, store=store, deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: VERIFIED, env={}, has_credential=lambda n: False,
                  provisioners={"twenty_api_key": TwentyKeyProvisioner(key_source=lambda: "tok-xyz")},
                  secret_dir=str(tmp_path))
    assert r.status == INSTALLED
    assert any("Location:" in n and "Twenty CRM" in n for n in r.notices)
    assert not any("tok-xyz" in n for n in r.notices)     # value never surfaced


def test_render_summary_is_human_readable():
    store = EventStore()
    r = bootstrap("do alpha", CATALOG, store=store, deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: VERIFIED, env={}, has_credential=lambda n: True)
    text = render_summary(r)
    assert text.startswith("✅ Ready.")
    assert "•" in text
