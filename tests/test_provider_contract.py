"""provider-capability/v1 — the vendor-neutral accelerator surface (Phase 0). Contracts only: the result
envelope + provider/deployment identity + target statistics. Adding AMD to this surface as profiles
(not new types) is the test that it isn't NVIDIA-shaped."""
from __future__ import annotations

from agentic_os.mission.provider import (
    CONTRACT_VERSION, CapabilityKind, CapabilityResult, ProviderIdentity,
    ExecutionTargetStats, DeploymentIdentity,
)


def test_contract_version():
    assert CONTRACT_VERSION == "provider-capability/v1"


def test_provider_identity_is_content_addressed():
    a = ProviderIdentity(provider="amd-rocm", service_kind="vllm", model_id="open/llm")
    assert a.digest() == a.digest()
    assert a.digest() != ProviderIdentity(provider="nvidia-nim", service_kind="nim").digest()
    assert a.digest().startswith("sha256:")


def test_same_call_two_providers_equal_canonical_output():
    # one Mission, two backends: canonical request/response equal; only provider identity differs
    common = dict(kind=CapabilityKind.MODEL, output_ref="art-1",
                  request_digest="sha256:req", response_digest="sha256:resp")
    amd = CapabilityResult(provider=ProviderIdentity(provider="amd-rocm", accelerator_kind="amd-instinct-mi300x"), **common)
    nv = CapabilityResult(provider=ProviderIdentity(provider="nvidia-nim", accelerator_kind="nvidia-h100"), **common)
    assert amd.request_digest == nv.request_digest and amd.response_digest == nv.response_digest
    assert amd.provider.digest() != nv.provider.digest()


def test_hbm_distinguishes_targets_and_snapshot_is_reproducible():
    mi300 = ExecutionTargetStats(provider="amd-rocm", accelerator_kind="amd-instinct-mi300x",
                                 accelerator_count=1, hbm_gb=192.0, statistics_version="1")
    h100 = ExecutionTargetStats(provider="nvidia-nim", accelerator_kind="nvidia-h100",
                                accelerator_count=2, hbm_gb=80.0, statistics_version="1")
    assert mi300.digest() != h100.digest()
    again = ExecutionTargetStats(provider="amd-rocm", accelerator_kind="amd-instinct-mi300x",
                                 accelerator_count=1, hbm_gb=192.0, statistics_version="1")
    assert mi300.digest() == again.digest()          # pinnable, reproducible snapshot


def test_deployment_identity_excludes_secret_values():
    d = DeploymentIdentity(chart_name="redevops-runtime", chart_version="0.1.0",
                           deployment_profile="amd-instinct",
                           secret_reference_names=("redevops-amd-provider",))
    # only reference names, never values
    assert d.secret_reference_names == ("redevops-amd-provider",)
    assert d.digest().startswith("sha256:")


def test_surface_covers_the_neutral_kinds():
    assert {k.value for k in CapabilityKind} >= {"model", "embedding", "rerank", "guardrail", "sandbox"}
