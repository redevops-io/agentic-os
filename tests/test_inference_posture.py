"""Inference posture — local-compute detection, PAIR mode recommendation, pairing network policy."""
from dataclasses import replace

from agentic_os.mission.device_posture import DeviceFacts, Decision
from agentic_os.mission.pair import PairStatus
from agentic_os.mission.inference_posture import (
    NetworkTrust, Mode, derive_inference_posture, pairing_decision, LOCAL_MIN_MEMORY_MB,
)

GPU_BOX = DeviceFacts(platform="linux-x86_64", container_runtime="docker",
                      containment_supported=True, gpu="NVIDIA RTX 4090")
THIN = DeviceFacts(platform="windows-amd64", container_runtime="docker",
                   containment_supported=True, gpu="")


def test_pairing_policy_is_deny_by_default():
    assert pairing_decision(NetworkTrust.TRUSTED_HOME_LAN) is Decision.ALLOW
    assert pairing_decision(NetworkTrust.TRUSTED_OFFICE_VLAN) is Decision.REVIEW
    assert pairing_decision(NetworkTrust.PUBLIC_WIFI) is Decision.DENY
    assert pairing_decision(NetworkTrust.SHARED_UNTRUSTED) is Decision.DENY
    assert pairing_decision(NetworkTrust.UNKNOWN) is Decision.DENY


def test_pair_present_recommends_local_pair():
    p = derive_inference_posture(GPU_BOX, pair=PairStatus(available=True, models=("qwen",)),
                                 network_trust=NetworkTrust.TRUSTED_HOME_LAN)
    assert p.recommended_mode is Mode.LOCAL_PAIR
    assert p.pair_installed and p.pair_eligible and p.local_only_supported
    assert p.may_pair and p.pairing is Decision.ALLOW


def test_capable_but_no_pair_recommends_install():
    p = derive_inference_posture(GPU_BOX, pair=PairStatus(available=False),
                                 network_trust=NetworkTrust.TRUSTED_HOME_LAN)
    assert p.recommended_mode is Mode.INSTALL_LOCAL_PAIR and p.pair_eligible


def test_thin_device_recommends_cloud():
    p = derive_inference_posture(THIN, pair=PairStatus(available=False),
                                 total_memory_mb=4096)
    assert p.recommended_mode is Mode.CLOUD
    assert not p.local_compute.available and not p.local_only_supported


def test_enough_ram_counts_as_local_capable_without_gpu():
    p = derive_inference_posture(THIN, total_memory_mb=LOCAL_MIN_MEMORY_MB)
    assert p.local_compute.available and p.recommended_mode is Mode.INSTALL_LOCAL_PAIR


def test_public_wifi_denies_pairing_even_on_a_capable_box():
    p = derive_inference_posture(GPU_BOX, pair=PairStatus(available=False),
                                 network_trust=NetworkTrust.PUBLIC_WIFI)
    assert p.pairing is Decision.DENY and not p.may_pair
    assert any("pairing denied" in r for r in p.reasons)


def test_posture_is_content_addressed():
    a = derive_inference_posture(GPU_BOX, network_trust=NetworkTrust.TRUSTED_HOME_LAN)
    b = derive_inference_posture(GPU_BOX, network_trust=NetworkTrust.TRUSTED_HOME_LAN)
    assert a.posture_id == b.posture_id and a.posture_id.startswith("rcv1:")
    c = derive_inference_posture(GPU_BOX, network_trust=NetworkTrust.PUBLIC_WIFI)
    assert c.posture_id != a.posture_id
