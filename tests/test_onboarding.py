"""Onboarding provisioning — create + store credentials on-device, notify the user of location.

Includes the installer wiring: a plan needing the Twenty key is satisfied by a provisioner that
mints + stores it, and the receipt carries the human notice with the on-device path.
"""
import os

import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest
from agentic_os.mission.device_posture import DeviceFacts, derive
from agentic_os.mission.provisioning import resolve
from agentic_os.mission.installer import StubDeployer, install
from agentic_os.mission.onboarding import (
    provision_local_secret, TwentyKeyProvisioner, default_secret_dir,
)

VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                       disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)
SECRET = "tok-twenty-abc-123"


def test_provision_local_secret_stores_on_device_and_reports_location(tmp_path):
    pc = provision_local_secret("twenty_api_key", SECRET, secret_dir=str(tmp_path),
                                app="Twenty CRM", app_url="http://localhost:3010")
    # stored at root/namespace/name, 0600, value readable back — but never in the notice
    assert pc.location == str(tmp_path / "default" / "twenty_api_key")
    assert os.path.exists(pc.location)
    assert oct(os.stat(pc.location).st_mode & 0o777) == "0o600"
    with open(pc.location) as fh:
        assert fh.read() == SECRET
    assert SECRET not in pc.notice.human_text            # location + reassurance, never the value
    assert pc.location in pc.notice.human_text
    assert "Twenty CRM" in pc.notice.human_text and "localhost:3010" in pc.notice.human_text


def test_twenty_provisioner_from_key_source(tmp_path):
    prov = TwentyKeyProvisioner(key_source=lambda: SECRET)
    pc = prov.provision(secret_dir=str(tmp_path))
    assert pc is not None and pc.name == "twenty_api_key"
    assert "Twenty CRM" in pc.notice.title or "Twenty CRM" in pc.notice.detail


def test_twenty_provisioner_returns_none_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TWENTY_API_KEY", raising=False)
    assert TwentyKeyProvisioner().provision(secret_dir=str(tmp_path)) is None


def test_default_secret_dir_honours_env(monkeypatch):
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", "/x/secrets")
    assert default_secret_dir() == "/x/secrets"


def _ok_fetch(url, timeout):
    return {"operator": "crm", "capabilities": [{"name": "crm.sync", "operator": "crm"}]}


def test_installer_provisions_twenty_key_and_surfaces_a_notice(tmp_path):
    crm = CapabilitySpec(name="crm.sync", operator="crm", provides=["crm_sync"],
                         isolation_class="sandbox", secrets=["twenty_api_key"])
    plan = resolve("keep my crm_sync running", [CapabilityManifest("crm", [crm])], derive(VERIFIED))
    r = install(plan, deployer=StubDeployer(endpoints={"crm": "http://stub/crm"}),
                fetch=_ok_fetch, has_credential=lambda n: False,       # nothing pre-configured
                provisioners={"twenty_api_key": TwentyKeyProvisioner(key_source=lambda: SECRET)},
                secret_dir=str(tmp_path))
    assert r.ok and r.ready                                  # provisioned ⇒ no missing credential
    assert "twenty_api_key" in r.provisioned
    assert r.installed[0].credentials_obtained == ("twenty_api_key",)
    assert any("Location:" in n and "Twenty CRM" in n for n in r.notices)
    assert not any(SECRET in n for n in r.notices)          # never leak the value
