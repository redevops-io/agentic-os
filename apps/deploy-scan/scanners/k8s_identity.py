"""k8s-identity profile: ServiceAccount identity over-exposure.

The cloud-IAM-over-permission checks from the original design (Azure Workload Identity / AWS IRSA / GCP
Workload Identity) do not apply here — this instance targets a self-hosted k3s cluster with no cloud
control plane to cross-check against. The self-hosted equivalent risk is the same shape (a workload
running with more identity than it needs): a pod that automounts its namespace's ServiceAccount token
when it never calls the Kubernetes API, or a `default` ServiceAccount that accumulated RBAC over time
and is now silently granted to every pod that doesn't request one explicitly. Read-only.

Target shape: "<kubecontext>" (every namespace) or "<kubecontext>/<namespace>".
"""
from __future__ import annotations

import json
import subprocess

from ..findings import Finding


def _kubectl_json(context: str, *args: str) -> dict:
    out = subprocess.run(["kubectl", "--context", context, *args, "-o", "json"],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _split_target(target: str) -> tuple[str, str | None]:
    if "/" in target:
        ctx, ns = target.split("/", 1)
        return ctx, (ns or None)
    return target, None


def _bound_service_accounts(context: str) -> set[tuple[str, str]]:
    """(namespace, name) of every ServiceAccount that is the subject of some RoleBinding/ClusterRoleBinding."""
    bound: set[tuple[str, str]] = set()
    for kind, kubectl_kind in (("RoleBinding", "rolebindings"), ("ClusterRoleBinding", "clusterrolebindings")):
        doc = _kubectl_json(context, "get", kubectl_kind, "-A") if kind == "RoleBinding" \
            else _kubectl_json(context, "get", kubectl_kind)
        for b in doc.get("items", []):
            default_ns = b["metadata"].get("namespace", "")
            for s in (b.get("subjects") or []):
                if s.get("kind") == "ServiceAccount":
                    bound.add((s.get("namespace") or default_ns, s.get("name", "")))
    return bound


def run(target: str) -> list[Finding]:
    context, namespace = _split_target(target)
    bound = _bound_service_accounts(context)

    pods_doc = _kubectl_json(context, "get", "pods", "--namespace", namespace) if namespace \
        else _kubectl_json(context, "get", "pods", "-A")

    findings: list[Finding] = []
    for pod in pods_doc.get("items", []):
        meta = pod["metadata"]
        ns = meta.get("namespace", "")
        where = f"{ns}/{meta['name']}"
        spec = pod.get("spec", {})
        sa_name = spec.get("serviceAccountName") or "default"
        automount = spec.get("automountServiceAccountToken")
        is_bound = (ns, sa_name) in bound

        if automount is not False and sa_name == "default" and not is_bound:
            # Low severity: an unbound default SA with the token mounted grants the pod a live API
            # credential it very likely never needs, even though that credential currently authorizes
            # nothing — a future binding to "default" silently grants every such pod that permission too.
            findings.append(Finding(
                severity="low", name="pod automounts the unbound 'default' ServiceAccount token",
                target=where, ref=sa_name,
                detail="automountServiceAccountToken is not set to false, and this pod uses the "
                        "namespace's 'default' ServiceAccount, which currently has no RBAC bound to "
                        "it. The token is live infrastructure the workload almost certainly never "
                        "calls, and any future binding to 'default' extends to every such pod.",
                recommended_action="Set automountServiceAccountToken: false on pods that never call "
                                   "the Kubernetes API, or give this one its own named ServiceAccount."))
        elif automount is not False and sa_name == "default" and is_bound:
            findings.append(Finding(
                severity="medium", name="pod uses the 'default' ServiceAccount, which IS bound to RBAC",
                target=where, ref=sa_name,
                detail="The namespace's 'default' ServiceAccount has a RoleBinding or "
                        "ClusterRoleBinding, so every pod that doesn't request its own ServiceAccount "
                        "(like this one) silently inherits that access.",
                recommended_action="Give this workload its own named ServiceAccount scoped to what it "
                                   "actually needs, instead of sharing 'default'."))
    return findings
