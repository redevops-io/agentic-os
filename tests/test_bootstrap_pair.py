"""Bootstrap ↔ PAIR wiring — a capable, credential-less device is offered governed local AI."""
from agentic_os.mission.types import CapabilitySpec, CapabilityManifest
from agentic_os.mission.device_posture import DeviceFacts
from agentic_os.mission.pair import PairStatus
from agentic_os.mission.inference_posture import NetworkTrust, Mode
from agentic_os.mission.store import EventStore
from agentic_os.mission.installer import StubDeployer
from agentic_os.mission.bootstrap import bootstrap, INSTALLED

GPU_BOX = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                      disk_encryption=True, secure_credential_store=True,
                      network_exposure="private", containment_supported=True, gpu="NVIDIA RTX 4090")
THIN = DeviceFacts(platform="windows-amd64", container_runtime="docker",
                   disk_encryption=True, secure_credential_store=True,
                   network_exposure="private", containment_supported=True)

SANDBOX = CapabilitySpec(name="a.run", operator="acap", provides=["alpha"], isolation_class="sandbox")
CATALOG = [CapabilityManifest("acap", [SANDBOX])]

def _ok_fetch(url, timeout): return {"operator": "acap", "capabilities": []}
def _stub(): return StubDeployer(endpoints={"acap": "http://stub/acap"})
def _pair_up(): return PairStatus(available=True, base_url="http://127.0.0.1:1234/v1", models=("qwen",))
def _pair_down(): return PairStatus(available=False)


def test_capable_device_no_creds_is_offered_local_ai():
    r = bootstrap("do alpha", CATALOG, store=EventStore(), deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: GPU_BOX, env={}, has_credential=lambda n: True,
                  pair_detect=_pair_down, network_trust=NetworkTrust.TRUSTED_HOME_LAN)
    assert r.status == INSTALLED                       # the app still installs
    assert r.pair_proposal is not None                # ...and local AI is offered
    assert r.inference_posture.recommended_mode is Mode.INSTALL_LOCAL_PAIR
    assert any("run AI locally" in n for n in r.notices)
    assert any("Ollama" in n for n in r.notices)      # the proposal card is shown


def test_pair_present_uses_it_and_makes_no_offer():
    r = bootstrap("do alpha", CATALOG, store=EventStore(), deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: GPU_BOX, env={}, has_credential=lambda n: True,
                  pair_detect=_pair_up, network_trust=NetworkTrust.TRUSTED_HOME_LAN)
    assert r.llm.source == "pair" and r.pair_proposal is None
    assert any("Local AI found" in n for n in r.notices)


def test_user_creds_make_no_local_offer():
    r = bootstrap("do alpha", CATALOG, store=EventStore(), deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: GPU_BOX, env={"GROQ_API_KEY": "x"}, has_credential=lambda n: True,
                  pair_detect=_pair_down)
    assert r.llm.source == "user_creds" and r.pair_proposal is None


def test_thin_device_falls_back_to_guided_cloud_no_offer():
    r = bootstrap("do alpha", CATALOG, store=EventStore(), deployer=_stub(), fetch=_ok_fetch,
                  prober=lambda: THIN, env={}, has_credential=lambda n: True,
                  pair_detect=_pair_down, total_memory_mb=4096)
    assert r.pair_proposal is None                     # not capable → no local offer
    assert r.inference_posture.recommended_mode is Mode.CLOUD
    assert any("LLM setup needed" in n for n in r.notices)
