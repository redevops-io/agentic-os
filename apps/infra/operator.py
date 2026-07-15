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

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_infra_operator(*, run=None, http_get=None) -> Operator:
    run = run or core._run
    http_get = http_get or core._http_get

    def _plan(i):
        return core.terraform_plan(i.get("cloud", "aws"), i.get("vars"), run=run)

    def _provision(i):
        return core.terraform_apply(i.get("cloud", "aws"), i.get("vars"), run=run)

    def _configure(i):
        return core.ansible_playbook(i.get("playbook", "playbooks/deploy-app.yml"),
                                     inventory=i.get("inventory"), extra_vars=i.get("extra_vars"), run=run)

    def _verify(i):
        return core.verify(i.get("host", ""), port=i.get("port", 8000), http_get=http_get)

    def _destroy(i):
        return core.terraform_destroy(i.get("cloud", "aws"), i.get("vars"), run=run)

    def _rollback(i):
        return core.ansible_playbook(i.get("rollback_playbook", "playbooks/rollback.yml"),
                                     inventory=i.get("inventory"), extra_vars=i.get("extra_vars"), run=run)

    def _drift(i):
        return core.drift(i.get("cloud", "aws"), check_playbook=i.get("check_playbook"),
                          inventory=i.get("inventory"), run=run)

    return Operator("infra", [
        capability("infra.plan", _plan, provides=["infra_planned"],
                   permissions=["infra:read"], estimated_value="medium", latency_ms=3000),
        capability("infra.provision", _provision, provides=["infra_provisioned"],
                   side_effecting=True, approval_required=True, undo="infra.destroy_delta",
                   permissions=["infra:write"], estimated_value="high", latency_ms=60000),
        capability("infra.configure", _configure, provides=["app_configured"],
                   side_effecting=True, undo="infra.rollback_release",
                   permissions=["infra:write"], estimated_value="high", latency_ms=45000),
        capability("infra.verify", _verify, provides=["deploy_verified"],
                   permissions=["infra:read"], latency_ms=5000),
        capability("infra.drift", _drift, provides=["drift_report"],
                   permissions=["infra:read"], estimated_value="medium", latency_ms=4000),
        capability("infra.destroy_delta", _destroy, side_effecting=True, permissions=["infra:write"]),
        capability("infra.rollback_release", _rollback, side_effecting=True, permissions=["infra:write"]),
    ])
