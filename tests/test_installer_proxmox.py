"""Installer integration test against REAL containers on the proxmox docker host.

Gated behind RDO_PROXMOX_IT=1 (and configurable host/ssh target), so the default offline
suite skips it. It proves P0.4 end-to-end without any Windows/macOS/iOS hardware: the
DockerDeployer stands a module up as a real container on proxmox, and the installer verifies
its /capabilities over the LAN and registers it for federation — then tears it down.

Run:  RDO_PROXMOX_IT=1 pytest tests/test_installer_proxmox.py -q
Env:  RDO_PROXMOX_SSH (default "proxmox"), RDO_PROXMOX_HOST (default "192.168.40.105")
"""
import os

import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest
from agentic_os.mission.device_posture import DeviceFacts, derive
from agentic_os.mission.provisioning import resolve
from agentic_os.mission.installer import DockerDeployer, install

pytestmark = pytest.mark.skipif(
    os.environ.get("RDO_PROXMOX_IT") != "1",
    reason="proxmox integration test (set RDO_PROXMOX_IT=1 to run)")

VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                       disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)
HELLO = CapabilitySpec(name="hello.echo", operator="hello", provides=["greeting"],
                       isolation_class="sandbox")
CATALOG = [CapabilityManifest("hello", [HELLO])]


def test_install_a_real_container_on_proxmox_and_verify_capabilities():
    dep = DockerDeployer(
        ssh_host=os.environ.get("RDO_PROXMOX_SSH", "proxmox"),
        host=os.environ.get("RDO_PROXMOX_HOST", "192.168.40.105"),
        manifests={"hello": [{"name": "hello.echo", "operator": "hello"}]})
    plan = resolve("please give me a greeting", CATALOG, derive(VERIFIED))
    assert plan.to_install == ("hello",) and plan.installable
    try:
        r = install(plan, deployer=dep, verify_timeout=10.0)
        assert r.ok, r.installed
        (o,) = r.installed
        assert o.verified and o.operator == "hello"
        assert r.modules_yaml.get("hello", "").startswith("http://")
    finally:
        dep.teardown("hello")
