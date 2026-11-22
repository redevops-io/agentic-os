"""deploy-scan — the runtime-exposure scanning engine behind edge-sentinel's deploy_scan.py client.

Self-contained, same philosophy as the client it serves (apps/edge-sentinel/deploy_scan.py): no
context-runtime / agentic_os imports, so it can run as its own small process wherever the operator's
own credentials reach (a laptop, a jump host, a CI runner with a kubeconfig mounted) — never bundled
into a publicly-pulled image with any default network or cluster reach of its own.

Implements the exact MCP JSON-RPC contract the client already expects (tools/call, structuredContent),
see server.py. Two families of scanner:

  * network/web profiles (nmap) — exposed ports and services on a host the operator names explicitly.
  * k8s-* profiles — RBAC over-privilege, NetworkPolicy coverage, and pod/workload exposure, read-only
    against a kubeconfig context the operator supplies. No cloud-IAM scanning (Workload Identity /
    IRSA) is implemented: this instance targets a self-hosted k3s cluster, not a managed cloud control
    plane, so there is no cloud IAM surface to check.

Authorization model (the actual enforcement, not a policy claim): every scan target must be listed in
SCAN_ALLOWLIST before `scan_start` will run it — fail closed, no autonomous discovery, no target this
process wasn't explicitly told about. The process only ever reaches what the operator's own nmap
network access / kubeconfig already reaches; it does not escalate, chain credentials, or persist.
"""
from __future__ import annotations
