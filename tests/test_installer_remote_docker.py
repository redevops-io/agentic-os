"""Installer integration test against REAL containers on a remote docker host.

Gated behind RDO_REMOTE_DOCKER_IT=1 (the ssh target and the host address come from the environment, with no default), so the default offline
suite skips it. It proves P0.4 end-to-end without any Windows/macOS/iOS hardware: the
DockerDeployer stands a module up as a real container on the remote docker host, and the installer verifies
its /capabilities over the network and registers it for federation — then tears it down.

Run:  RDO_REMOTE_DOCKER_IT=1 RDO_REMOTE_DOCKER_SSH=<ssh target> RDO_REMOTE_DOCKER_HOST=<its address> pytest tests/test_installer_remote_docker.py -q
Env:  RDO_REMOTE_DOCKER_SSH, RDO_REMOTE_DOCKER_HOST (both required, no default; docs/DEPLOYMENT_ADDRESSES.md)
"""
import os

import pytest

from agentic_os.mission.types import CapabilitySpec, CapabilityManifest
from agentic_os.mission.device_posture import DeviceFacts, derive
from agentic_os.mission.provisioning import resolve
from agentic_os.mission.installer import DockerDeployer, install

pytestmark = pytest.mark.skipif(
    os.environ.get("RDO_REMOTE_DOCKER_IT") != "1",
    reason="remote-docker integration test (set RDO_REMOTE_DOCKER_IT=1 to run)")

VERIFIED = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                       disk_encryption=True, secure_credential_store=True,
                       network_exposure="private", containment_supported=True)
HELLO = CapabilitySpec(name="hello.echo", operator="hello", provides=["greeting"],
                       isolation_class="sandbox")
CATALOG = [CapabilityManifest("hello", [HELLO])]

def _need(name):
    """The remote docker host is deployment configuration: nothing is assumed (docs/DEPLOYMENT_ADDRESSES.md)."""
    value = os.environ.get(name, "")
    if not value:
        pytest.fail(f"{name} must be set when RDO_REMOTE_DOCKER_IT=1: the ssh target / address of YOUR remote docker host")
    return value


def test_install_a_real_container_on_a_remote_docker_host_and_verify_capabilities():
    dep = DockerDeployer(
        ssh_host=_need("RDO_REMOTE_DOCKER_SSH"),
        host=_need("RDO_REMOTE_DOCKER_HOST"),
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
