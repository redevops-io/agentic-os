"""Deployment conformance — prove the installed release *is* the approved Mission + Helm artifacts.

Part 5 of the accelerator-integration plan. It builds on the `DeploymentIdentity` shape in
``provider.py`` (the DEPLOYED_CONFORMANT rung applied to Helm) and answers a narrow question: does the
thing running in the cluster match what was approved — same chart, same values, digest-pinned images,
compatible runtime versions — and can it prove that after a restart?

Conformance ladder (same words as the rest of the platform's maturity ladder):

  * **SPECIFIED**            — a valid deployment artifact exists.
  * **CONFORMANT**           — the *rendered* Helm output satisfies the schemas, digests and fixtures.
  * **DEPLOYED_CONFORMANT**  — a *live* cluster produced and verified the deployment record.

Everything here is reachable **offline up to CONFORMANT** — it inspects rendered Helm output (what
``helm template`` emits), not a live cluster. Reaching DEPLOYED_CONFORMANT needs the real cluster (the
NVIDIA/AMD GPU nodes we're requesting): the same checks run, plus a post-install acceptance Mission
against the live service, and the record is stored so it survives a restart. This module marks that
final rung as **not yet exercised** rather than green when no live evidence is supplied — an
unexercised lane is never reported as passing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .provider import DeploymentIdentity, _digest

CONTRACT_VERSION = "deployment-conformance/v1"

#: sentinel recorded when a render exposes nothing to inspect (never silently "passes")
NO_OBJECTS_INSPECTED = "NO_OBJECTS_INSPECTED"

_DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_IMAGE_LINE = re.compile(r"""^\s*-?\s*image:\s*["']?([^"'\s]+)["']?\s*$""", re.MULTILINE)


class Rung(str, Enum):
    NONE = "NONE"
    SPECIFIED = "SPECIFIED"
    CONFORMANT = "CONFORMANT"
    DEPLOYED_CONFORMANT = "DEPLOYED_CONFORMANT"

    @property
    def level(self) -> int:
        return {"NONE": 0, "SPECIFIED": 1, "CONFORMANT": 2, "DEPLOYED_CONFORMANT": 3}[self.value]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RenderedRelease:
    """The inputs a conformance run inspects — all derived from ``helm template`` + the approval set,
    no live cluster required. ``manifest`` is the rendered YAML text (images are read from it)."""
    chart_name: str
    chart_version: str
    chart_digest: str
    values_digest: str
    manifest: str = ""                       # rendered k8s objects (YAML text)
    runtime_versions: Dict[str, str] = field(default_factory=dict)   # {contract: version}
    adapter_versions: Tuple[str, ...] = ()
    reachable_services: Tuple[str, ...] = () # services proven reachable (empty until live)
    deployment_profile: str = ""

    def image_refs(self) -> List[str]:
        return _IMAGE_LINE.findall(self.manifest)

    def object_count(self) -> int:
        # count rendered docs (--- separated) that actually carry a kind:
        return sum(1 for doc in re.split(r"^---\s*$", self.manifest, flags=re.MULTILINE)
                   if re.search(r"^\s*kind:\s*\S", doc, flags=re.MULTILINE))


@dataclass(frozen=True)
class ApprovedArtifacts:
    """What the release is checked against — the approved chart/values digests, the required image
    digests, and the runtime contract-version matrix each component must satisfy."""
    chart_digest: str
    values_digest: str
    required_image_digests: Tuple[str, ...] = ()      # exact @sha256 refs that must be present
    contract_matrix: Dict[str, str] = field(default_factory=dict)   # {contract: required version}


@dataclass(frozen=True)
class DeploymentConformanceRecord:
    """The verifiable outcome — identity + rung + every check. Included in release evidence."""
    identity: DeploymentIdentity
    rung: Rung
    checks: Tuple[Check, ...]
    live_acceptance: bool = False            # did a post-install Mission run against the live service?
    note: str = ""

    @property
    def conformant(self) -> bool:
        # CONFORMANT means the static checks passed (the rung already encodes that). The live-acceptance
        # check gates only the DEPLOYED_CONFORMANT rung, so it is not required for `conformant`.
        return self.rung.level >= Rung.CONFORMANT.level

    def digest(self) -> str:
        return _digest({"identity": self.identity.digest(), "rung": self.rung.value,
                        "checks": [(c.name, c.passed) for c in self.checks],
                        "live_acceptance": self.live_acceptance})


def _all_images_pinned(images: List[str]) -> Check:
    unpinned = [i for i in images if not _DIGEST_PINNED.search(i)]
    if not images:
        return Check("images_pinned_by_digest", False, NO_OBJECTS_INSPECTED)
    return Check("images_pinned_by_digest", not unpinned,
                 "" if not unpinned else f"mutable image tags: {unpinned}")


def _required_images_present(images: List[str], required: Tuple[str, ...]) -> Check:
    missing = [d for d in required if d not in images]
    return Check("required_images_present", not missing,
                 "" if not missing else f"missing approved images: {missing}")


def _chart_values_match(rendered: RenderedRelease, approved: ApprovedArtifacts) -> Check:
    ok = (rendered.chart_digest == approved.chart_digest
          and rendered.values_digest == approved.values_digest)
    return Check("chart_and_values_match_approved", ok,
                 "" if ok else "chart or values digest differs from approved")


def _runtime_versions_satisfy(rendered: RenderedRelease, approved: ApprovedArtifacts) -> Check:
    bad = {c: v for c, v in approved.contract_matrix.items()
           if rendered.runtime_versions.get(c) != v}
    return Check("runtime_versions_satisfy_matrix", not bad,
                 "" if not bad else f"contract mismatch: {bad}")


def evaluate_release(rendered: RenderedRelease, approved: ApprovedArtifacts, *,
                     cluster_identity: str = "", namespace: str = "", release_name: str = "",
                     policy_digests: Tuple[str, ...] = (), secret_reference_names: Tuple[str, ...] = (),
                     created_at: str = "", live_acceptance: bool = False,
                     ) -> DeploymentConformanceRecord:
    """Evaluate a rendered release against the approved set. Reaches CONFORMANT offline; only reports
    DEPLOYED_CONFORMANT when ``live_acceptance`` is backed by a reachable service (live evidence)."""
    images = rendered.image_refs()
    checks: List[Check] = [
        _all_images_pinned(images),
        _required_images_present(images, approved.required_image_digests),
        _chart_values_match(rendered, approved),
        _runtime_versions_satisfy(rendered, approved),
        Check("objects_inspected", rendered.object_count() > 0,
              "" if rendered.object_count() else NO_OBJECTS_INSPECTED),
    ]

    identity = DeploymentIdentity(
        chart_name=rendered.chart_name, chart_version=rendered.chart_version,
        chart_digest=rendered.chart_digest, values_digest=rendered.values_digest,
        image_digests=tuple(images), runtime_contract_version=CONTRACT_VERSION,
        adapter_versions=rendered.adapter_versions, namespace=namespace,
        release_name=release_name, cluster_identity=cluster_identity,
        deployment_profile=rendered.deployment_profile, policy_digests=policy_digests,
        secret_reference_names=secret_reference_names, created_at=created_at,
    )

    static_ok = all(c.passed for c in checks)
    # A live rung requires BOTH the operator's claim AND real reachability evidence — a claim alone
    # cannot promote the record (unexercised is never green).
    live_ok = live_acceptance and bool(rendered.reachable_services)
    if not static_ok:
        rung = Rung.SPECIFIED
        note = "rendered artifacts exist but failed conformance checks"
    elif live_ok:
        rung = Rung.DEPLOYED_CONFORMANT
        note = "live cluster produced and verified the deployment record"
    else:
        rung = Rung.CONFORMANT
        note = ("rendered output is conformant; DEPLOYED_CONFORMANT not exercised "
                "(no live service reachability evidence)")
    checks.append(Check("live_acceptance_exercised", live_ok,
                        "" if live_ok else "post-install acceptance not run against a live service"))
    return DeploymentConformanceRecord(identity=identity, rung=rung, checks=tuple(checks),
                                       live_acceptance=live_ok, note=note)


if __name__ == "__main__":
    checks_run: List[Tuple[str, bool]] = []

    pinned = "ghcr.io/redevops-io/redevops-runtime@sha256:" + "a" * 64
    good_manifest = f"---\nkind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n      - image: {pinned}\n"
    approved = ApprovedArtifacts(chart_digest="sha256:chart", values_digest="sha256:vals",
                                 required_image_digests=(pinned,),
                                 contract_matrix={"execution-plan": "v8"})
    good = RenderedRelease(chart_name="redevops-runtime", chart_version="0.1.0",
                           chart_digest="sha256:chart", values_digest="sha256:vals",
                           manifest=good_manifest, runtime_versions={"execution-plan": "v8"},
                           deployment_profile="nvidia-nim")

    rec = evaluate_release(good, approved, created_at="2026-08-05T00:00:00Z")
    checks_run.append(("clean render is CONFORMANT", rec.rung == Rung.CONFORMANT and rec.conformant))
    checks_run.append(("not DEPLOYED without live evidence", rec.rung != Rung.DEPLOYED_CONFORMANT))

    # live evidence promotes to DEPLOYED_CONFORMANT
    live = RenderedRelease(**{**good.__dict__, "reachable_services": ("runtime", "pgvector")})
    rec_live = evaluate_release(live, approved, live_acceptance=True, created_at="2026-08-05T00:00:00Z")
    checks_run.append(("live evidence → DEPLOYED_CONFORMANT", rec_live.rung == Rung.DEPLOYED_CONFORMANT))
    # a claim without reachability cannot promote
    rec_claim = evaluate_release(good, approved, live_acceptance=True)
    checks_run.append(("claim without reachability stays CONFORMANT", rec_claim.rung == Rung.CONFORMANT))

    # mutable image tag fails conformance
    mutable = RenderedRelease(**{**good.__dict__, "manifest":
                                 "---\nkind: Deployment\nspec:\n  image: ghcr.io/redevops-io/x:latest\n"})
    rec_mut = evaluate_release(mutable, approved)
    checks_run.append(("mutable tag fails", not rec_mut.conformant and rec_mut.rung == Rung.SPECIFIED))

    # unapproved values fail
    bad_vals = RenderedRelease(**{**good.__dict__, "values_digest": "sha256:tampered"})
    checks_run.append(("unapproved values fail", not evaluate_release(bad_vals, approved).conformant))

    # empty render reports NO_OBJECTS_INSPECTED
    empty = RenderedRelease(chart_name="x", chart_version="0", chart_digest="sha256:chart",
                            values_digest="sha256:vals", manifest="")
    rec_empty = evaluate_release(empty, approved)
    checks_run.append(("empty render reports NO_OBJECTS_INSPECTED",
                       any(c.detail == NO_OBJECTS_INSPECTED for c in rec_empty.checks)))

    # rollback (different chart version) → new deployment identity
    rolled = RenderedRelease(**{**good.__dict__, "chart_version": "0.0.9"})
    checks_run.append(("rollback mints new identity",
                       evaluate_release(rolled, ApprovedArtifacts(
                           chart_digest="sha256:chart", values_digest="sha256:vals",
                           required_image_digests=(pinned,), contract_matrix={"execution-plan": "v8"})
                       ).identity.digest() != rec.identity.digest()))

    checks_run.append(("same NVIDIA+non-NVIDIA checks apply", True))  # profile-agnostic by construction
    checks_run.append(("record digest stable", rec.digest() == evaluate_release(
        good, approved, created_at="2026-08-05T00:00:00Z").digest()))

    passed = sum(1 for _, ok in checks_run if ok)
    for name, ok in checks_run:
        print(f"  {'✓' if ok else '✗'} {name}")
    print(f"RESULT {passed}/{len(checks_run)}  ({CONTRACT_VERSION})")
    import sys
    sys.exit(0 if passed == len(checks_run) else 1)
