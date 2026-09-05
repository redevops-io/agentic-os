"""Inference posture — *whether* to use local AI (PAIR), and *whether pairing is appropriate here*.

Device posture (P0.2) answered "what may this device execute?". This answers the inference-specific
questions the PAIR decision needs, and is what Edge Sentinel emits so the governed installer can
propose local AI rather than have Sidekick install it blindly:

* **Can this device run local AI at all?** (`LocalCompute` — GPU + memory.)
* **Is PAIR already here, and is it eligible?**
* **What mode do we recommend?** `LOCAL_PAIR` (use it) · `INSTALL_LOCAL_PAIR` (capable, PAIR absent)
  · `CLOUD` (no adequate local compute).
* **Is pairing appropriate on THIS network?** PAIR's trust bootstrap is a 6-digit PIN and its LAN
  telemetry is unauthenticated, so *where* you pair matters: a home LAN is fine, public Wi-Fi is not.

The layering the whole design turns on: **PAIR provides the capability; ReDevOps governs when using
it is appropriate.** Deny-by-default — an unknown network refuses pairing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from runtime_contracts.canonical import content_hash

from .device_posture import Decision, DeviceFacts
from .pair import PairStatus

CONTRACT_VERSION = "inference-posture/v1"

#: Memory (MB) below which we don't consider a device able to serve a useful local model.
LOCAL_MIN_MEMORY_MB = 8192


class NetworkTrust(str, Enum):
    TRUSTED_HOME_LAN = "trusted_home_lan"
    TRUSTED_OFFICE_VLAN = "trusted_office_vlan"
    PUBLIC_WIFI = "public_wifi"
    SHARED_UNTRUSTED = "shared_untrusted"
    UNKNOWN = "unknown"


class Mode(str, Enum):
    LOCAL_PAIR = "local_pair"                 # PAIR is here and usable — use it
    INSTALL_LOCAL_PAIR = "install_local_pair" # capable device, PAIR absent — propose installing it
    CLOUD = "cloud"                           # no adequate local compute — cloud provider


def pairing_decision(network: NetworkTrust) -> Decision:
    """Whether it is appropriate to run PAIR *pairing* on this network. Deny-by-default.

    Pairing exchanges trust over the LAN with a low-entropy PIN and unauthenticated telemetry, so a
    trusted home LAN is ALLOW, an office VLAN is REVIEW (a human confirms), and anything public or
    unknown is DENY. (Using an *already-paired* cluster is separate; this gates forming trust.)
    """
    return {
        NetworkTrust.TRUSTED_HOME_LAN: Decision.ALLOW,
        NetworkTrust.TRUSTED_OFFICE_VLAN: Decision.REVIEW,
        NetworkTrust.PUBLIC_WIFI: Decision.DENY,
        NetworkTrust.SHARED_UNTRUSTED: Decision.DENY,
    }.get(network, Decision.DENY)


@dataclass(frozen=True)
class LocalCompute:
    available: bool
    gpu: str = ""
    memory_mb: int = 0

    def canonical_form(self) -> dict:
        return {"available": self.available, "gpu": self.gpu, "memory_mb": self.memory_mb}


@dataclass(frozen=True)
class InferencePosture:
    local_compute: LocalCompute
    pair_installed: bool
    pair_eligible: bool
    recommended_mode: Mode
    local_only_supported: bool
    network_trust: NetworkTrust
    pairing: Decision
    reasons: Tuple[str, ...] = ()
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "local_compute": self.local_compute.canonical_form(),
            "pair_installed": self.pair_installed,
            "pair_eligible": self.pair_eligible,
            "recommended_mode": self.recommended_mode.value,
            "local_only_supported": self.local_only_supported,
            "network_trust": self.network_trust.value,
            "pairing": self.pairing.value,
            "reasons": sorted(self.reasons),
        }

    @property
    def posture_id(self) -> str:
        return content_hash(self.canonical_form())

    @property
    def may_pair(self) -> bool:
        return self.pairing is not Decision.DENY


def derive_inference_posture(facts: DeviceFacts, *, pair: Optional[PairStatus] = None,
                             network_trust: NetworkTrust = NetworkTrust.UNKNOWN,
                             total_memory_mb: int = 0) -> InferencePosture:
    """Compose device facts + PAIR status + network trust into an inference posture. Deny-by-default.

    ``total_memory_mb`` is supplied by the launcher (DeviceFacts doesn't carry RAM); 0 = unknown ⇒
    treated as not-adequate. A device is 'local-capable' with a detected GPU or enough RAM.
    """
    reasons: list[str] = []
    capable = bool(facts.gpu) or (total_memory_mb >= LOCAL_MIN_MEMORY_MB)
    if not capable:
        reasons.append("no local GPU and insufficient/unknown RAM for a useful local model")
    local = LocalCompute(available=capable, gpu=facts.gpu, memory_mb=total_memory_mb)

    pair_installed = bool(pair and pair.available)
    pair_eligible = capable and facts.containment_supported

    if pair_installed:
        mode = Mode.LOCAL_PAIR
    elif pair_eligible:
        mode = Mode.INSTALL_LOCAL_PAIR
    else:
        mode = Mode.CLOUD
        if not facts.containment_supported:
            reasons.append("containment unsupported here — local execution can't be contained")

    decision = pairing_decision(network_trust)
    if decision is Decision.DENY:
        reasons.append(f"pairing denied on network '{network_trust.value}' "
                       "(PIN trust + unauthenticated LAN telemetry — pair only on a trusted network)")

    return InferencePosture(
        local_compute=local, pair_installed=pair_installed, pair_eligible=pair_eligible,
        recommended_mode=mode, local_only_supported=capable, network_trust=network_trust,
        pairing=decision, reasons=tuple(reasons))
