"""deployment-conformance/v1 — the installed release must be the approved artifacts. Reaches
CONFORMANT offline (rendered Helm); DEPLOYED_CONFORMANT needs live reachability evidence."""
from agentic_os.mission.deployment_conformance import (
    RenderedRelease, ApprovedArtifacts, evaluate_release, Rung, NO_OBJECTS_INSPECTED, CONTRACT_VERSION,
)

PINNED = "ghcr.io/redevops-io/redevops-runtime@sha256:" + "a" * 64
MANIFEST = f"---\nkind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n      - image: {PINNED}\n"
APPROVED = ApprovedArtifacts(chart_digest="sha256:chart", values_digest="sha256:vals",
                             required_image_digests=(PINNED,), contract_matrix={"execution-plan": "v8"})


def _good(**over):
    base = dict(chart_name="redevops-runtime", chart_version="0.1.0", chart_digest="sha256:chart",
                values_digest="sha256:vals", manifest=MANIFEST, runtime_versions={"execution-plan": "v8"},
                deployment_profile="nvidia-nim")
    base.update(over)
    return RenderedRelease(**base)


def test_clean_render_is_conformant_but_not_deployed():
    rec = evaluate_release(_good(), APPROVED)
    assert rec.rung == Rung.CONFORMANT and rec.conformant
    assert rec.rung != Rung.DEPLOYED_CONFORMANT


def test_live_reachability_promotes_to_deployed_conformant():
    rec = evaluate_release(_good(reachable_services=("runtime", "pgvector")),
                           APPROVED, live_acceptance=True)
    assert rec.rung == Rung.DEPLOYED_CONFORMANT and rec.live_acceptance


def test_claim_without_reachability_cannot_promote():
    # an operator claim alone (no reachable services) must not report green at the live rung
    rec = evaluate_release(_good(), APPROVED, live_acceptance=True)
    assert rec.rung == Rung.CONFORMANT


def test_mutable_image_tag_fails_conformance():
    rec = evaluate_release(_good(manifest="---\nkind: Deployment\nspec:\n  image: ghcr.io/x:latest\n"),
                           APPROVED)
    assert not rec.conformant and rec.rung == Rung.SPECIFIED


def test_unapproved_values_fail():
    assert not evaluate_release(_good(values_digest="sha256:tampered"), APPROVED).conformant


def test_runtime_version_mismatch_fails():
    assert not evaluate_release(_good(runtime_versions={"execution-plan": "v7"}), APPROVED).conformant


def test_empty_render_reports_no_objects_inspected():
    rec = evaluate_release(_good(manifest=""), APPROVED)
    assert any(c.detail == NO_OBJECTS_INSPECTED for c in rec.checks) and not rec.conformant


def test_rollback_mints_new_identity():
    a = evaluate_release(_good(), APPROVED).identity.digest()
    b = evaluate_release(_good(chart_version="0.0.9"), APPROVED).identity.digest()
    assert a != b


def test_same_checks_for_nvidia_and_non_nvidia_profiles():
    nv = evaluate_release(_good(deployment_profile="nvidia-nim"), APPROVED)
    amd = evaluate_release(_good(deployment_profile="amd-instinct"), APPROVED)
    assert [c.name for c in nv.checks] == [c.name for c in amd.checks]


def test_record_digest_is_stable():
    assert evaluate_release(_good(), APPROVED).digest() == evaluate_release(_good(), APPROVED).digest()


def test_contract_version():
    assert CONTRACT_VERSION == "deployment-conformance/v1"
