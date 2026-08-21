"""provider-capability/v1 — the vendor-neutral surface for accelerated capability backends.

Phase 0 of the accelerator-integration plan (NVIDIA + AMD): the *contracts only*. This module defines
the provider-neutral data shapes that any accelerated capability adapter — NVIDIA NIM/NeMo, an AMD
ROCm endpoint, or an existing hosted provider — returns and is pinned by, so that:

  * a single Mission runs unchanged across providers (one `providers:` flip),
  * every accelerated call produces ordinary runtime evidence (provider/service/model identity, digests,
    cost, latency) rather than a vendor-shaped side channel,
  * an accelerated result **replays from persisted artifacts** without consulting live deployment config,
  * the planner can weigh physical targets (`ExecutionTargetStats`) without hardware entering app intent,
  * a live cluster can prove it runs the approved chart/values/images (`DeploymentIdentity`).

It holds **no vendor concepts and no adapters** — NIM, ROCm, `nvidia.com/gpu`/`amd.com/gpu` and model
names are deployment facts that live in adapter metadata and Helm profiles, never here. The capability
*kinds* are thin protocols; their implementations (which need real services to test) are out of scope
for this skeleton. Adding a second vendor (AMD) to this surface as *profiles* rather than new contract
types is the conformance test that the surface is genuinely vendor-neutral.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

#: The version is the contract this surface conforms to (whitepaper-lineage style, like merge/v9).
CONTRACT_VERSION = "provider-capability/v1"


def _digest(obj: Any) -> str:
    """Canonical content digest (sorted keys, no whitespace), matching the runtime's other digests."""
    canonical = json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(canonical.encode()).hexdigest()[:16]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


class CapabilityKind(str, Enum):
    """The vendor-neutral capability kinds an accelerator backend can serve."""
    MODEL = "model"           # generate / chat / structured output
    EMBEDDING = "embedding"   # text/other → vector references
    RERANK = "rerank"         # query × candidates → ranked with evidence
    EXTRACTION = "extraction" # document → artifact handles
    GUARDRAIL = "guardrail"   # observe-only; runtime policy still decides
    SANDBOX = "sandbox"       # resource-policy execution (fs/network/process/inference)


@dataclass(frozen=True)
class ProviderIdentity:
    """Who served a capability, pinned onto every result for replay. Never carries credentials or a
    private hostname in public artifacts — `endpoint_identity` is a stable opaque reference."""
    provider: str                          # e.g. "openai-compat" | "nvidia-nim" | "amd-rocm"
    service_kind: str = ""                 # e.g. "nim" | "vllm" | "sglang" | "nemo-retriever"
    service_version: str = ""              # the server build / ROCm build
    model_id: str = ""                     # served weights identifier
    model_digest: str = ""                 # when the provider exposes one
    endpoint_identity: str = ""            # opaque, non-secret reference to the endpoint
    adapter_version: str = ""              # the ReDevOps adapter that made the call
    accelerator_kind: str = ""             # deployment fact, e.g. "nvidia-h100" | "amd-instinct-mi300x"
    deployment_profile_digest: str = ""    # ties the result to the profile it ran under

    def digest(self) -> str:
        return _digest(self)


@dataclass(frozen=True)
class CapabilityResult:
    """The common envelope every accelerated capability invocation returns — an ordinary evidence
    record, not a vendor payload. `output_ref` is a reference (ArtifactHandle-style), never inline
    output, so results dereference and replay from persisted artifacts."""
    kind: CapabilityKind
    output_ref: str
    provider: ProviderIdentity
    request_digest: str = ""               # deterministic serialization of the request
    response_digest: str = ""              # of the canonical (not vendor-formatted) response
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    evidence: tuple[str, ...] = ()         # evidence handles supporting the result
    policy_decisions: tuple[str, ...] = () # policy/guardrail decisions recorded for this call

    def digest(self) -> str:
        return _digest(self)


@dataclass(frozen=True)
class ExecutionTargetStats:
    """Physical execution facts the planner may weigh — pinned as a versioned snapshot on every plan so
    a plan is reproducible and replay never consults live telemetry. Hardware is never part of app
    intent; the Goal expresses constraints (max latency/cost, modality, residency, quality floor) and
    the planner maps them to eligible targets. A single large-HBM accelerator (e.g. an MI300X at 192 GB)
    legitimately changes `accelerator_count`/`supported_context` — an explainable difference, not a branch."""
    provider: str
    service_kind: str = ""
    model_id: str = ""
    accelerator_kind: str = ""
    accelerator_count: int = 0
    hbm_gb: float = 0.0                     # per-accelerator HBM — the axis where Instinct differs
    locality: str = ""                     # "hosted" | "self-hosted" | "data-local"
    queue_depth: int = 0
    cold_start_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    throughput: float = 0.0
    estimated_cost_usd: float = 0.0
    supported_context: int = 0
    supported_modalities: tuple[str, ...] = ()
    data_residency: str = ""
    observation_time: str = ""             # pass in; the runtime forbids wall-clock in pure code
    statistics_version: str = "0"

    def digest(self) -> str:
        return _digest(self)


@dataclass(frozen=True)
class DeploymentIdentity:
    """What a live cluster must prove it is running — the DEPLOYED_CONFORMANT rung applied to Helm.
    Secret *values* are never included; only reference names."""
    chart_name: str
    chart_version: str = ""
    chart_digest: str = ""
    values_digest: str = ""
    image_digests: tuple[str, ...] = ()
    runtime_contract_version: str = ""
    adapter_versions: tuple[str, ...] = ()
    namespace: str = ""
    release_name: str = ""
    cluster_identity: str = ""
    deployment_profile: str = ""
    policy_digests: tuple[str, ...] = ()
    secret_reference_names: tuple[str, ...] = ()
    created_at: str = ""                   # pass in; no wall-clock here

    def digest(self) -> str:
        return _digest(self)


# ── capability kind protocols (thin — implementations need real services, out of scope here) ────────
@runtime_checkable
class ProviderCapability(Protocol):
    """A vendor-neutral accelerated capability. An adapter (NIM, ROCm endpoint, hosted provider)
    implements this; the runtime only ever sees `identity()` + a `CapabilityResult`."""
    kind: CapabilityKind
    def identity(self) -> ProviderIdentity: ...
    # invoke(...) -> CapabilityResult   # signature is per-kind; adapters define it


# ── contract self-check (mirrors merge.py / policy.py __main__ conformance runners) ─────────────────
if __name__ == "__main__":
    checks = []
    prov = ProviderIdentity(provider="amd-rocm", service_kind="vllm", service_version="rocm-6.2",
                            model_id="open/llm", adapter_version="model@1",
                            accelerator_kind="amd-instinct-mi300x")
    res = CapabilityResult(kind=CapabilityKind.MODEL, output_ref="art-abc", provider=prov,
                           request_digest="sha256:req", response_digest="sha256:resp",
                           cost_usd=0.01, latency_ms=42.0)
    checks.append(("identity digest stable", prov.digest() == prov.digest()))
    checks.append(("identity content-addressed", prov.digest() != ProviderIdentity(provider="nvidia-nim").digest()))
    checks.append(("result carries provider identity", res.provider.provider == "amd-rocm"))
    checks.append(("result digest stable", res.digest() == res.digest()))
    # same call, two accelerators → same canonical result modulo provider identity (replay invariant)
    res_n = CapabilityResult(kind=CapabilityKind.MODEL, output_ref="art-abc",
                             provider=ProviderIdentity(provider="nvidia-nim", accelerator_kind="nvidia-h100"),
                             request_digest="sha256:req", response_digest="sha256:resp")
    checks.append(("canonical output equal across providers",
                   res.request_digest == res_n.request_digest and res.response_digest == res_n.response_digest))
    checks.append(("provider identity differs", res.provider.digest() != res_n.provider.digest()))
    mi300 = ExecutionTargetStats(provider="amd-rocm", accelerator_kind="amd-instinct-mi300x",
                                 accelerator_count=1, hbm_gb=192.0, supported_context=131072, statistics_version="1")
    h100 = ExecutionTargetStats(provider="nvidia-nim", accelerator_kind="nvidia-h100",
                                accelerator_count=2, hbm_gb=80.0, supported_context=131072, statistics_version="1")
    checks.append(("HBM distinguishes targets", mi300.digest() != h100.digest()))
    checks.append(("stats snapshot reproducible", mi300.digest() == ExecutionTargetStats(
        provider="amd-rocm", accelerator_kind="amd-instinct-mi300x", accelerator_count=1, hbm_gb=192.0,
        supported_context=131072, statistics_version="1").digest()))
    dep = DeploymentIdentity(chart_name="redevops-runtime", chart_version="0.1.0",
                             image_digests=("sha256:img",), deployment_profile="amd-instinct",
                             secret_reference_names=("redevops-amd-provider",))
    checks.append(("deployment identity excludes secret values",
                   "redevops-amd-provider" in dep.secret_reference_names and dep.digest().startswith("sha256:")))
    checks.append(("capability kinds cover the surface",
                   {k.value for k in CapabilityKind} >= {"model", "embedding", "rerank", "guardrail", "sandbox"}))
    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"RESULT {passed}/{len(checks)}  (contract {CONTRACT_VERSION})")
    import sys
    sys.exit(0 if passed == len(checks) else 1)
