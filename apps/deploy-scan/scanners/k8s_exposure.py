"""k8s-exposure profile: pod-spec-level exposure — privileged/hostPath/hostNetwork/hostPID workloads,
and secrets referenced as literal env values rather than secretKeyRef. Read-only.

Target shape: "<kubecontext>" (every namespace) or "<kubecontext>/<namespace>".
"""
from __future__ import annotations

import json
import re
import subprocess

from ..findings import Finding

#: A literal env value that LOOKS like a credential (not a reference) — conservative, high-signal
#: patterns only, to keep this a real finding and not a guessing game over every env var.
_SECRET_LOOKING_NAME = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)$", re.IGNORECASE)


def _kubectl_json(context: str, *args: str) -> dict:
    out = subprocess.run(["kubectl", "--context", context, *args, "-o", "json"],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _split_target(target: str) -> tuple[str, str | None]:
    if "/" in target:
        ctx, ns = target.split("/", 1)
        return ctx, (ns or None)
    return target, None


def _containers(pod_spec: dict) -> list[dict]:
    return list(pod_spec.get("containers") or []) + list(pod_spec.get("initContainers") or [])


def run(target: str) -> list[Finding]:
    context, namespace = _split_target(target)
    doc = _kubectl_json(context, "get", "pods", "--namespace", namespace) if namespace \
        else _kubectl_json(context, "get", "pods", "-A")

    findings: list[Finding] = []
    for pod in doc.get("items", []):
        meta = pod["metadata"]
        where = f"{meta.get('namespace', '')}/{meta['name']}"
        spec = pod.get("spec", {})

        host_flags = [k for k in ("hostNetwork", "hostPID", "hostIPC") if spec.get(k)]
        if host_flags:
            findings.append(Finding(
                severity="high", name=f"pod shares the host namespace(s): {host_flags}",
                target=where, ref=", ".join(host_flags),
                detail="A pod with hostNetwork/hostPID/hostIPC can observe or interact with processes "
                        "and network traffic on the node itself, not just its own container.",
                recommended_action="Remove the host namespace flag unless this pod genuinely needs "
                                   "node-level access (e.g. a CNI or node-exporter daemonset)."))

        for vol in (spec.get("volumes") or []):
            if "hostPath" in vol:
                findings.append(Finding(
                    severity="high", name=f"pod mounts hostPath {vol['hostPath'].get('path')}",
                    target=where, ref=vol.get("name", ""),
                    detail="hostPath gives the container direct access to a path on the node's "
                            "filesystem, which can be a container-escape or node-tampering vector.",
                    recommended_action="Use a PersistentVolumeClaim or emptyDir instead of hostPath "
                                       "where possible."))

        for c in _containers(spec):
            sc = c.get("securityContext") or {}
            if sc.get("privileged"):
                findings.append(Finding(
                    severity="critical", name=f"container '{c['name']}' runs privileged",
                    target=where, ref=c["name"],
                    detail="A privileged container has effectively all capabilities of the host kernel "
                            "and can escape to the node.",
                    recommended_action="Drop privileged; grant only the specific Linux capabilities "
                                       "this container needs."))
            if sc.get("allowPrivilegeEscalation") is True and not sc.get("privileged"):
                findings.append(Finding(
                    severity="medium", name=f"container '{c['name']}' allows privilege escalation",
                    target=where, ref=c["name"],
                    detail="allowPrivilegeEscalation lets a process gain more privileges than its "
                            "parent (e.g. via a setuid binary) even without the privileged flag.",
                    recommended_action="Set allowPrivilegeEscalation: false unless a specific binary "
                                       "needs it."))
            for env in (c.get("env") or []):
                name = env.get("name", "")
                if "value" in env and env["value"] and _SECRET_LOOKING_NAME.search(name):
                    findings.append(Finding(
                        severity="high", name=f"container '{c['name']}' has a literal credential-shaped env var",
                        target=where, ref=name,
                        detail=f"Env var '{name}' is a plain literal value, not a secretKeyRef — its "
                                "value is visible in the pod spec (kubectl get pod -o yaml) and any "
                                "tooling that reads it, not just in Secret objects.",
                        recommended_action=f"Move '{name}' into a Secret and reference it with "
                                           "valueFrom.secretKeyRef instead of a literal value."))
    return findings
