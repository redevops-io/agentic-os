"""infra core — Terraform (provision) + Ansible (configure) as injectable shell-outs.

The deploy-as-a-governed-mission arm (v6 Phase 6.1): the infra operator's capabilities call these,
so a deploy is an ordinary mission — gated on approval, verified, and saga-rolled-back. Each command
runner (`run`) and the health probe (`http_get`) are injectable, so the operator is tested without
terraform / ansible / a network.

Layout it drives (written by the deploy tree):
  deploy/terraform/envs/<cloud>/   — `terraform -chdir=… plan|apply|destroy` (aws|gcp|azure|digitalocean)
  deploy/ansible/                  — `ansible-playbook playbooks/…`
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

DEPLOY_ROOT = os.environ.get("INFRA_DEPLOY_ROOT",
                             str(Path(__file__).resolve().parents[3] / "deploy"))
TF_ROOT = os.path.join(DEPLOY_ROOT, "terraform")
ANSIBLE_ROOT = os.path.join(DEPLOY_ROOT, "ansible")

Runner = Callable[[list, "str | None"], "tuple[int, str, str]"]   # (argv, cwd) -> (rc, stdout, stderr)
HttpGet = Callable[[str], int]                                     # url -> status code


def _run(argv: list, cwd: "str | None" = None) -> "tuple[int, str, str]":
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _http_get(url: str) -> int:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return int(getattr(r, "status", 0) or 0)
    except Exception:
        return 0


def _env_dir(cloud: str) -> str:
    return os.path.join(TF_ROOT, "envs", cloud)


def _var_args(tf_vars: "dict | None") -> list:
    out: list = []
    for k, v in (tf_vars or {}).items():
        # Python bools stringify as "True"/"False"; terraform bool vars need lowercase true/false.
        if isinstance(v, bool):
            v = "true" if v else "false"
        out += ["-var", f"{k}={v}"]
    return out


def _last_line(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _err_summary(err: str) -> str:
    """The most informative line of a terraform failure — the `Error:` line if present (terraform
    puts the caret-pointed config line last, which is useless on its own), else the last line."""
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("Error:") or ln.startswith("│ Error:"):
            return ln.lstrip("│ ").strip()
    return lines[-1] if lines else ""


# ── Terraform ────────────────────────────────────────────────────────────────
def terraform_plan(cloud: str, tf_vars: "dict | None" = None, *, run: Runner = _run) -> dict:
    argv = ["terraform", f"-chdir={_env_dir(cloud)}", "plan", "-input=false", "-no-color"] + _var_args(tf_vars)
    rc, out, err = run(argv, None)
    return {"status": "done" if rc == 0 else "error", "action": "plan", "cloud": cloud,
            "rc": rc, "summary": _last_line(out) if rc == 0 else _err_summary(err),
            "stdout": out[-4000:], "stderr": err[-3000:]}


def terraform_apply(cloud: str, tf_vars: "dict | None" = None, *, run: Runner = _run) -> dict:
    argv = ["terraform", f"-chdir={_env_dir(cloud)}", "apply", "-input=false",
            "-auto-approve", "-no-color"] + _var_args(tf_vars)
    rc, out, err = run(argv, None)
    return {"status": "done" if rc == 0 else "error", "action": "provision", "cloud": cloud,
            "rc": rc, "outputs": terraform_outputs(cloud, run=run) if rc == 0 else {},
            "summary": _last_line(out) if rc == 0 else _err_summary(err),
            "stdout": out[-4000:], "stderr": err[-3000:]}


def terraform_destroy(cloud: str, tf_vars: "dict | None" = None, *, run: Runner = _run) -> dict:
    """Saga compensation for a failed/rejected provision — tear down the just-applied delta."""
    argv = ["terraform", f"-chdir={_env_dir(cloud)}", "destroy", "-input=false",
            "-auto-approve", "-no-color"] + _var_args(tf_vars)
    rc, out, err = run(argv, None)
    return {"status": "done" if rc == 0 else "error", "action": "destroy_delta", "cloud": cloud, "rc": rc}


def terraform_outputs(cloud: str, *, run: Runner = _run) -> dict:
    rc, out, _ = run(["terraform", f"-chdir={_env_dir(cloud)}", "output", "-json"], None)
    if rc != 0:
        return {}
    try:
        return {k: v.get("value") for k, v in json.loads(out).items()}
    except Exception:
        return {}


# ── Ansible ──────────────────────────────────────────────────────────────────
def ansible_playbook(playbook: str, *, inventory: "str | None" = None,
                     extra_vars: "dict | None" = None, run: Runner = _run) -> dict:
    pb = playbook if os.path.isabs(playbook) else os.path.join(ANSIBLE_ROOT, playbook)
    argv = ["ansible-playbook", pb]
    if inventory:
        argv += ["-i", inventory]
    for k, v in (extra_vars or {}).items():
        argv += ["-e", f"{k}={v}"]
    rc, out, err = run(argv, ANSIBLE_ROOT)
    return {"status": "done" if rc == 0 else "error", "action": "configure",
            "playbook": playbook, "rc": rc, "summary": _last_line(out) if rc == 0 else _last_line(err)}


# ── verification ──────────────────────────────────────────────────────────────
def verify(host: str, *, port: int = 8000, endpoints: tuple = ("/health", "/capabilities"),
           http_get: HttpGet = _http_get) -> dict:
    checks = {ep: http_get(f"http://{host}:{port}{ep}") for ep in endpoints}
    healthy = bool(host) and all(code == 200 for code in checks.values())
    return {"status": "done" if healthy else "error", "action": "verify",
            "host": host, "checks": checks, "healthy": healthy}


# ── drift (the infrastructure + configuration subtypes of Mission Drift, v6 §5) ──
def _ansible_changed(text: str) -> int:
    import re
    return sum(int(n) for n in re.findall(r"changed=(\d+)", text or ""))


def drift(cloud: str, *, check_playbook: "str | None" = None, inventory: "str | None" = None,
          run: Runner = _run) -> dict:
    """Read-only drift check: `terraform plan -detailed-exitcode` (0=clean · 2=drift · 1=error) for
    infrastructure, and `ansible-playbook --check --diff` for configuration. Returns the shape the
    runtime's `drift.check_drift(infra=…)` folds into the Mission Drift taxonomy."""
    rc, out, err = run(["terraform", f"-chdir={_env_dir(cloud)}", "plan", "-detailed-exitcode",
                        "-input=false", "-no-color"], None)
    infra = {"exit_code": rc, "drift": rc == 2, "error": rc == 1,
             "summary": _last_line(out if rc in (0, 2) else err)}
    result = {"action": "drift", "cloud": cloud, "infrastructure": infra}
    if check_playbook:
        argv = ["ansible-playbook", os.path.join(ANSIBLE_ROOT, check_playbook), "--check", "--diff"]
        if inventory:
            argv += ["-i", inventory]
        crc, cout, cerr = run(argv, ANSIBLE_ROOT)
        changed = _ansible_changed(cout)
        result["configuration"] = {"exit_code": crc, "drift": changed > 0, "changed": changed}
    result["drift"] = infra["drift"] or result.get("configuration", {}).get("drift", False)
    result["status"] = "error" if infra["error"] else "done"
    return result
