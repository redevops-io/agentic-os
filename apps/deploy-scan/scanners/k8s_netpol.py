"""k8s-netpol profile: namespaces with no NetworkPolicy are default-allow-all, which is the common real
gap (Kubernetes ships with no network isolation until an operator adds policies). Read-only.

Target shape: "<kubecontext>" (every namespace) or "<kubecontext>/<namespace>" (just that one).
"""
from __future__ import annotations

import json
import subprocess

from ..findings import Finding

#: Namespaces excluded from the "no policy at all" finding — system namespaces an operator rarely
#: isolates and where doing so can break the cluster itself.
_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}


def _kubectl_json(context: str, *args: str) -> dict:
    out = subprocess.run(["kubectl", "--context", context, *args, "-o", "json"],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _split_target(target: str) -> tuple[str, str | None]:
    if "/" in target:
        ctx, ns = target.split("/", 1)
        return ctx, (ns or None)
    return target, None


def _allows_all_namespaces(policy: dict) -> bool:
    """A NetworkPolicy ingress rule with an empty `from: [{namespaceSelector: {}}]` admits every namespace."""
    for rule in (policy.get("spec", {}).get("ingress") or []):
        for peer in (rule.get("from") or []):
            if peer.get("namespaceSelector") == {}:
                return True
    return False


def run(target: str) -> list[Finding]:
    context, namespace = _split_target(target)

    ns_doc = _kubectl_json(context, "get", "namespaces")
    namespaces = [namespace] if namespace else [
        n["metadata"]["name"] for n in ns_doc.get("items", [])
        if n["metadata"]["name"] not in _SYSTEM_NAMESPACES
    ]

    netpol_doc = _kubectl_json(context, "get", "networkpolicies", "-A")
    by_namespace: dict[str, list[dict]] = {}
    for p in netpol_doc.get("items", []):
        by_namespace.setdefault(p["metadata"]["namespace"], []).append(p)

    findings: list[Finding] = []
    for ns in namespaces:
        policies = by_namespace.get(ns, [])
        if not policies:
            findings.append(Finding(
                severity="medium", name=f"namespace '{ns}' has no NetworkPolicy",
                target=ns, ref="",
                detail="With no NetworkPolicy, every pod in this namespace accepts ingress from "
                        "anywhere the cluster network reaches (default-allow).",
                recommended_action="Add a default-deny NetworkPolicy, then allow only the traffic "
                                   "this namespace's workloads actually need."))
            continue
        wide = [p["metadata"]["name"] for p in policies if _allows_all_namespaces(p)]
        if wide:
            findings.append(Finding(
                severity="low", name=f"namespace '{ns}' has NetworkPolicy allowing ingress from ANY namespace",
                target=ns, ref=", ".join(wide),
                detail=f"Polic(ies) {wide} admit traffic from every namespace via an empty "
                        "namespaceSelector, which is broader than a same-namespace or named-namespace rule.",
                recommended_action="Scope the namespaceSelector to the specific namespaces that need "
                                   "cross-namespace access."))
    return findings
