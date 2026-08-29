"""infra as a Mission Runtime operator (v6 Phase 6.1 — deployment as a governed mission).

Exposes the capabilities the `deploy_app` mission template composes, over Terraform + Ansible:

  infra.plan             — terraform plan (read-only; the diff as evidence)
  infra.provision        — terraform apply         [approval gate] undo=infra.destroy_delta
  infra.configure        — ansible-playbook rollout                undo=infra.rollback_release
  infra.verify           — /health + /capabilities smoke
  infra.destroy_delta    — terraform destroy   (saga compensation)
  infra.rollback_release — ansible redeploy prior release (saga compensation)

`build_infra_operator(run=…, http_get=…)` injects the command runner + health probe so the operator
is tested without terraform / ansible / network.
"""
from __future__ import annotations

import json
import os

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def _cloud(i) -> str:
    """Target cloud for a step: explicit step input wins, else the DEMO_CLOUD env (multi-cloud demo),
    else 'aws'. Keeps existing single-cloud missions byte-identical."""
    return i.get("cloud") or os.environ.get("DEMO_CLOUD") or "aws"


def _vars(i) -> "dict | None":
    """Terraform vars: explicit step input wins, else DEMO_TF_VARS (JSON) from the env."""
    if i.get("vars"):
        return i["vars"]
    raw = os.environ.get("DEMO_TF_VARS")
    if raw:
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
    return None


def build_infra_operator(*, run=None, http_get=None) -> Operator:
    run = run or core._run
    http_get = http_get or core._http_get

    def _plan(i):
        return core.terraform_plan(_cloud(i), _vars(i), run=run)

    def _provision(i):
        return core.terraform_apply(_cloud(i), _vars(i), run=run)

    def _configure(i):
        return core.ansible_playbook(i.get("playbook", "playbooks/deploy-app.yml"),
                                     inventory=i.get("inventory"), extra_vars=i.get("extra_vars"), run=run)

    def _verify(i):
        return core.verify(i.get("host", ""), port=i.get("port", 8000), http_get=http_get)

    def _destroy(i):
        return core.terraform_destroy(_cloud(i), _vars(i), run=run)

    def _rollback(i):
        return core.ansible_playbook(i.get("rollback_playbook", "playbooks/rollback.yml"),
                                     inventory=i.get("inventory"), extra_vars=i.get("extra_vars"), run=run)

    def _drift(i):
        return core.drift(_cloud(i), check_playbook=i.get("check_playbook"),
                          inventory=i.get("inventory"), run=run)

    # Concurrency safety (v0.3.x): a terraform apply/destroy holds the state lock for its cloud/workspace,
    # an ansible rollout holds its inventory/host — so writes to the SAME target serialize, writes to
    # INDEPENDENT targets run concurrently. Keys resolve from the step's `cloud`/`inventory` inputs; when a
    # mission doesn't carry them (env-driven demo), the template stays unresolved and same-cap writes
    # serialize conservatively (one terraform state → one apply at a time). Plan/verify/drift are read-only
    # (they take the state lock in shared mode at most) so they never block each other.
    TF_STATE = "tf:state:{cloud}"            # terraform backend/workspace lock (per cloud)
    ANSIBLE_HOST = "ansible:inventory:{inventory}"   # ansible target lock (per inventory/host)
    return Operator("infra", [
        capability("infra.plan", _plan, provides=["infra_planned"],
                   permissions=["infra:read"], estimated_value="medium", latency_ms=3000,
                   concurrency_mode="read_only"),
        capability("infra.provision", _provision, provides=["infra_provisioned"],
                   side_effecting=True, approval_required=True, undo="infra.destroy_delta",
                   permissions=["infra:write"], estimated_value="high", latency_ms=60000,
                   concurrency_mode="exclusive", concurrency_key=TF_STATE),
        capability("infra.configure", _configure, provides=["app_configured"],
                   side_effecting=True, undo="infra.rollback_release",
                   permissions=["infra:write"], estimated_value="high", latency_ms=45000,
                   concurrency_mode="exclusive", concurrency_key=ANSIBLE_HOST),
        capability("infra.verify", _verify, provides=["deploy_verified"],
                   permissions=["infra:read"], latency_ms=5000, concurrency_mode="read_only"),
        capability("infra.drift", _drift, provides=["drift_report"],
                   permissions=["infra:read"], estimated_value="medium", latency_ms=4000,
                   concurrency_mode="read_only"),
        capability("infra.destroy_delta", _destroy, side_effecting=True, permissions=["infra:write"],
                   concurrency_mode="exclusive", concurrency_key=TF_STATE),
        capability("infra.rollback_release", _rollback, side_effecting=True, permissions=["infra:write"],
                   concurrency_mode="exclusive", concurrency_key=ANSIBLE_HOST),
    ])
