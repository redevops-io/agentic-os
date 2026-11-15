"""k8s-rbac profile: flag RBAC over-privilege reachable from the operator's own kubeconfig context.

Read-only (`kubectl get`, never `apply`/`create`/`patch`), and scoped to exactly what that context's
credentials already reach — if the identity running this is namespace-scoped, `kubectl` simply returns
less; that is the correct fail-safe, not a gap this scanner needs to work around.

Target shape: "<kubecontext>" (every namespace the credentials can list) or "<kubecontext>/<namespace>"
(bindings in that namespace, plus any ClusterRoleBinding — those are never namespace-scoped).
"""
from __future__ import annotations

import json
import subprocess

from ..findings import Finding

#: Subjects expected to legitimately hold cluster-admin (the k3s/kubeadm bootstrap identities). Anything
#: else bound to cluster-admin is a finding, not an assumption that every admin binding is intentional.
_EXPECTED_CLUSTER_ADMIN_SUBJECTS = {"system:masters", "kubernetes-admin"}

#: Verbs that are dangerous even without a wildcard, because they let a subject expand its own reach.
_DANGEROUS_VERBS = {"impersonate", "escalate", "bind", "*"}
_SENSITIVE_RESOURCES = {"secrets", "*"}


def _kubectl_json(context: str, *args: str) -> dict:
    out = subprocess.run(["kubectl", "--context", context, *args, "-o", "json"],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _split_target(target: str) -> tuple[str, str | None]:
    if "/" in target:
        ctx, ns = target.split("/", 1)
        return ctx, (ns or None)
    return target, None


def _role_key(ref: dict) -> tuple[str, str]:
    kind = ref.get("kind", "")
    return (kind, ref.get("name", ""))


def _rule_is_wildcard_verb(rule: dict) -> bool:
    return "*" in (rule.get("verbs") or [])


def _rule_is_wildcard_resource(rule: dict) -> bool:
    return "*" in (rule.get("resources") or [])


def _rule_dangerous_verbs(rule: dict) -> set[str]:
    return set(rule.get("verbs") or []) & _DANGEROUS_VERBS


def _rule_touches_secrets(rule: dict) -> bool:
    return bool(set(rule.get("resources") or []) & _SENSITIVE_RESOURCES)


def _assess_rules(rules: list[dict]) -> tuple[str, list[str]]:
    """Worst severity + the specific reasons, for one role's rule set."""
    reasons: list[str] = []
    worst = "info"
    for rule in rules:
        if _rule_is_wildcard_verb(rule) and _rule_is_wildcard_resource(rule):
            reasons.append("verbs:* on resources:* (cluster-admin-equivalent)")
            worst = "critical"
        elif _rule_is_wildcard_verb(rule) or _rule_is_wildcard_resource(rule):
            reasons.append(f"wildcard rule: verbs={rule.get('verbs')} resources={rule.get('resources')}")
            worst = worst if worst == "critical" else "high"
        dangerous = _rule_dangerous_verbs(rule)
        if dangerous:
            reasons.append(f"dangerous verb(s) {sorted(dangerous)} on resources={rule.get('resources')}")
            if worst not in ("critical",):
                worst = "high"
        if _rule_touches_secrets(rule) and set(rule.get("verbs") or []) & {"get", "list", "watch", "*"}:
            reasons.append(f"can read secrets: verbs={rule.get('verbs')}")
            if worst == "info":
                worst = "medium"
    return worst, reasons


def run(target: str) -> list[Finding]:
    context, namespace = _split_target(target)

    roles_by_key: dict[tuple[str, str], list[dict]] = {}
    for kind, kubectl_kind in (("Role", "roles"), ("ClusterRole", "clusterroles")):
        doc = _kubectl_json(context, "get", kubectl_kind, "-A") if kind == "Role" else _kubectl_json(context, "get", kubectl_kind)
        for item in doc.get("items", []):
            name = item["metadata"]["name"]
            roles_by_key[(kind, name)] = item.get("rules") or []

    findings: list[Finding] = []

    binding_kinds = [("RoleBinding", "rolebindings"), ("ClusterRoleBinding", "clusterrolebindings")]
    for kind, kubectl_kind in binding_kinds:
        if kind == "RoleBinding":
            doc = _kubectl_json(context, "get", kubectl_kind, "--namespace", namespace) if namespace \
                else _kubectl_json(context, "get", kubectl_kind, "-A")
        else:
            doc = _kubectl_json(context, "get", kubectl_kind)

        for binding in doc.get("items", []):
            meta = binding["metadata"]
            binding_ns = meta.get("namespace", "")
            role_ref = binding.get("roleRef", {})
            rules = roles_by_key.get(_role_key(role_ref), [])
            subjects = binding.get("subjects") or []
            subject_names = [s.get("name", "?") for s in subjects]
            where = f"{binding_ns}/{meta['name']}" if binding_ns else meta["name"]

            if kind == "ClusterRoleBinding" and role_ref.get("name") == "cluster-admin":
                extra = [s for s in subject_names if s not in _EXPECTED_CLUSTER_ADMIN_SUBJECTS]
                if extra:
                    findings.append(Finding(
                        severity="high", name=f"cluster-admin bound to unexpected subject(s) {extra}",
                        target=where, ref=meta["name"],
                        detail="ClusterRoleBinding grants cluster-admin to a subject not in the expected "
                                "bootstrap-identity set.",
                        recommended_action="Bind a narrower role, or confirm this subject is meant to "
                                           "administer the whole cluster."))
                continue

            severity, reasons = _assess_rules(rules)
            if reasons:
                findings.append(Finding(
                    severity=severity,
                    name=f"{kind} '{meta['name']}' over-privileges {subject_names}",
                    target=where, ref=f"{role_ref.get('kind')}/{role_ref.get('name')}",
                    detail="; ".join(reasons),
                    recommended_action="Narrow the bound role's rules to the specific verbs/resources "
                                       "this subject actually needs."))
    return findings
