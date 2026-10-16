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

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable


def _default_deploy_root() -> str:
    """INFRA_DEPLOY_ROOT if set, else the repo's own ``deploy/`` dir (apps/infra/core.py -> parents[2]).
    If neither exists the root is "" and every terraform/ansible path FAILS CLOSED with a clear error --
    we never guess a directory outside the repo."""
    env = os.environ.get("INFRA_DEPLOY_ROOT")
    if env:
        return env
    repo_deploy = Path(__file__).resolve().parents[2] / "deploy"
    return str(repo_deploy) if repo_deploy.is_dir() else ""


DEPLOY_ROOT = _default_deploy_root()
TF_ROOT = os.path.join(DEPLOY_ROOT, "terraform") if DEPLOY_ROOT else ""
ANSIBLE_ROOT = os.path.join(DEPLOY_ROOT, "ansible") if DEPLOY_ROOT else ""


class InfraInputError(ValueError):
    """Rejected infra input (bad cloud / path escape / unsafe var). Handlers return it as an error result."""


_CLOUD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_VAR_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FORBIDDEN_EXTRA_VARS = frozenset({"ansible_python_interpreter", "ansible_connection",
                                   "ansible_shell_executable"})
# Any connection/privilege/interpreter knob is reserved: extra_vars carry application values only.
_RESERVED_VAR_PREFIXES = ("ansible_",)
# terraform resource address: name(.name | [digits] | ["quoted key"])* -- quoted keys may hold any printable
# text (for_each keys, CIDRs); argv is never shell-parsed and a leading letter rules out flags.
_ADDR_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*|\[(?:[0-9]+|"[^"\\\x00-\x1f\x7f]{0,200}")\])*')
_HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_NUMERICISH_RE = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", re.I)


def _inside(root: str, path: str) -> bool:
    r, p = os.path.realpath(root), os.path.realpath(path)
    return p == r or p.startswith(r + os.sep)


def _check_extra_vars(extra_vars: "dict | None") -> dict:
    out: dict = {}
    for k, v in (extra_vars or {}).items():
        if not isinstance(k, str) or not _VAR_KEY_RE.fullmatch(k):
            raise InfraInputError(f"extra_vars key {k!r} is not allowed (must match {_VAR_KEY_RE.pattern})")
        if k in _FORBIDDEN_EXTRA_VARS:
            raise InfraInputError(f"extra_vars key {k!r} is forbidden")
        if k.lower().startswith(_RESERVED_VAR_PREFIXES):
            raise InfraInputError(f"extra_vars key {k!r} is reserved")
        sv = str(v)
        if sv.lstrip().startswith(("@", "-")):
            raise InfraInputError(f"extra_vars value for {k!r} may not start with '@' or '-'")
        if "{{" in sv or "{%" in sv or "{#" in sv:
            raise InfraInputError(f"extra_vars value for {k!r} may not contain template syntax")
        out[k] = v
    return out


def _resolve_inventory(inventory: "str | None") -> "str | None":
    """Inventory must be a plain (non-executable) file inside the ansible root."""
    if not inventory:
        return None
    if not ANSIBLE_ROOT:
        raise InfraInputError("infra deploy root is not configured")
    if not isinstance(inventory, str) or inventory.startswith("-") or "," in inventory:
        raise InfraInputError("invalid inventory")
    inv = inventory if os.path.isabs(inventory) else os.path.join(ANSIBLE_ROOT, inventory)
    if not _inside(ANSIBLE_ROOT, inv):
        raise InfraInputError(f"inventory {inventory!r} resolves outside the deploy root")
    if os.path.exists(inv) and (not os.path.isfile(inv) or os.access(inv, os.X_OK)):
        raise InfraInputError("inventory must be a non-executable file")
    return inv

Runner = Callable[[list, "str | None"], "tuple[int, str, str]"]   # (argv, cwd) -> (rc, stdout, stderr)
HttpGet = Callable[[str], int]                                     # url -> status code


def _run(argv: list, cwd: "str | None" = None) -> "tuple[int, str, str]":
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _http_get(url: str) -> int:
    """GET with the SSRF guard applied at connect time: resolve once, require every address to be allowed,
    connect to the checked IP (pinned), never follow redirects, ignore proxy environment variables."""
    import http.client
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        host, port = u.hostname or "", u.port or 80
        ips = _check_host(host, port)
        conn = http.client.HTTPConnection(ips[0], port, timeout=10)     # IP pinned; no proxies in http.client
        try:
            conn.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                         headers={"Host": u.netloc, "Connection": "close"})
            return int(conn.getresponse().status)                          # a 3xx is reported, not followed
        finally:
            conn.close()
    except Exception:
        return 0


def _env_dir(cloud: str) -> str:
    if not TF_ROOT:
        raise InfraInputError("infra deploy root is not configured: set INFRA_DEPLOY_ROOT "
                              "(no repo-relative deploy/ directory found) -- refusing to guess a path")
    if not isinstance(cloud, str) or not _CLOUD_RE.fullmatch(cloud):
        raise InfraInputError(f"invalid cloud {cloud!r}: must match {_CLOUD_RE.pattern}")
    envs = os.path.join(TF_ROOT, "envs")
    d = os.path.join(envs, cloud)
    if not _inside(envs, d):
        raise InfraInputError(f"cloud {cloud!r} resolves outside the deploy root")
    return d


def _guard(action: str, cloud: "str | None" = None):
    """Decorator-free helper: turn an InfraInputError into the uniform error result."""
    def wrap(fn):
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except InfraInputError as e:
                r = {"status": "error", "action": action, "error": str(e), "rc": None}
                if action == "provision":
                    r["applied"] = False
                return r
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return wrap


def _var_args(tf_vars: "dict | None") -> list:
    out: list = []
    for k, v in (tf_vars or {}).items():
        if not isinstance(k, str) or not _VAR_KEY_RE.fullmatch(k):
            raise InfraInputError(f"terraform var name {k!r} is not allowed")
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
# The approved plan is SAVED here by `plan` and applied VERBATIM by `provision`, so a deploy executes
# exactly what a human approved — never a fresh plan computed against state that drifted after approval
# (the classic `apply -auto-approve` TOCTOU). The file name is relative to the env dir (terraform -chdir).
_PLAN_FILE = "redevops-approved.tfplan"


def _plan_path(cloud: str) -> str:
    return os.path.join(_env_dir(cloud), _PLAN_FILE)


def plan_digest(cloud: str) -> str:
    """sha256 of the SAVED plan file's bytes (empty string if no plan is on disk). This is the identity a
    human approves; `terraform_apply` re-hashes the file at apply time and refuses on mismatch, so the plan
    cannot be swapped between approval and apply (TOCTOU)."""
    try:
        with open(_plan_path(cloud), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, InfraInputError):
        return ""


@_guard("plan")
def terraform_plan(cloud: str, tf_vars: "dict | None" = None, *, run: Runner = _run) -> dict:
    # `-out` writes the plan to disk so `provision` can apply exactly this plan (and nothing else).
    argv = ["terraform", f"-chdir={_env_dir(cloud)}", "plan", "-input=false", "-no-color",
            f"-out={_PLAN_FILE}"] + _var_args(tf_vars)
    rc, out, err = run(argv, None)
    # The digest of the just-written plan is the approval identity (#6): it travels with the approval and is
    # re-checked at apply, so a human approves THIS exact plan — not whatever is on disk at apply time.
    return {"status": "done" if rc == 0 else "error", "action": "plan", "cloud": cloud,
            "rc": rc, "plan_file": _PLAN_FILE if rc == 0 else None,
            "plan_digest": plan_digest(cloud) if rc == 0 else None,
            "summary": _last_line(out) if rc == 0 else _err_summary(err),
            "stdout": out[-4000:], "stderr": err[-3000:]}


@_guard("provision")
def terraform_apply(cloud: str, tf_vars: "dict | None" = None, *, approved_digest: "str | None" = None,
                    run: Runner = _run) -> dict:
    # Apply the SAVED, approved plan verbatim — NOT a fresh `-auto-approve` plan. `tf_vars` are already
    # baked into the saved plan (passing `-var` with a plan file is an error), so they're intentionally
    # ignored here. terraform refuses a missing or stale saved plan, so if state drifted since approval the
    # apply fails closed instead of applying something the human never saw.
    #
    # TOCTOU bind (#6): when the approval carried a plan digest, re-hash the on-disk plan NOW and refuse if it
    # differs — the plan file could have been overwritten between approval and apply. Fail closed: a mismatch
    # (or a missing plan where a digest was expected) never applies.
    if not approved_digest:
        return {"status": "error", "action": "provision", "cloud": cloud, "rc": None,
                "error": "plan_digest_required", "applied": False,
                "summary": ("terraform apply requires the approved plan digest (the `plan_digest` emitted by "
                            "infra.plan and bound into the approval) -- refusing to apply whatever plan "
                            "file happens to be on disk.")}
    if approved_digest:
        on_disk = plan_digest(cloud)
        if on_disk != approved_digest:
            return {"status": "error", "action": "provision", "cloud": cloud, "rc": None,
                    "error": "plan_digest_mismatch", "applied": False,
                    "approved_digest": approved_digest, "on_disk_digest": on_disk,
                    "summary": ("saved plan was overwritten since approval (digest mismatch) — refusing to "
                                "apply a plan the human never approved." if on_disk else
                                "approved plan is no longer on disk — refusing to apply.")}
    argv = ["terraform", f"-chdir={_env_dir(cloud)}", "apply", "-input=false", "-no-color", _PLAN_FILE]
    before = _state_list(cloud, run)
    rc, out, err = run(argv, None)
    # Record exactly which resources this apply added (state after minus state before) so the compensating
    # destroy can target only those. Unknown (state unreadable) is recorded as None: undo then refuses.
    after = _state_list(cloud, run)
    created = (sorted(a for a in set(after) - set(before) if not a.startswith("data.") and ".data." not in a)
               if before is not None and after is not None else None)
    recovery = ("" if rc == 0 or created else
                "apply did not complete and no created resources could be recorded: inspect the terraform "
                "state and remove any partial resources manually")
    return {"status": "done" if rc == 0 else "error", "action": "provision", "cloud": cloud, "recovery": recovery,
            "rc": rc, "outputs": terraform_outputs(cloud, run=run) if rc == 0 else {},
            "applied": rc == 0, "created_resources": created,
            "summary": _last_line(out) if rc == 0 else _err_summary(err),
            "stdout": out[-4000:], "stderr": err[-3000:]}


_DESTROY_PLAN = "redevops-destroy.tfplan"


def _state_list(cloud: str, run: Runner) -> "list | None":
    rc, out, err = run(["terraform", f"-chdir={_env_dir(cloud)}", "state", "list"], None)
    if rc != 0:
        # terraform exits 1 with "No state file was found" before the first apply: that is an EMPTY state
        if re.search(r"no state file|state file was not found|does not have any|no state", f"{err}\n{out}", re.I):
            return []
        return None
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


@_guard("destroy_delta")
def terraform_destroy(cloud: str, created: "list | None" = None, *, run: Runner = _run) -> dict:
    """Saga compensation for a provision: destroy ONLY the resources the forward apply recorded as created
    (`-target` each). With no recorded resources there is nothing safe to destroy, so it refuses."""
    _env_dir(cloud)
    if not isinstance(created, list) or not created:
        raise InfraInputError("no created resources recorded by the forward apply -- refusing to destroy")
    for a in created:
        if not isinstance(a, str) or not _ADDR_RE.fullmatch(a):
            raise InfraInputError(f"recorded resource address {a!r} is not valid")
    chdir = f"-chdir={_env_dir(cloud)}"
    targets = [f"-target={a}" for a in created]
    # Preflight: terraform also destroys dependants of a target. Plan the destroy and refuse if it touches
    # anything beyond the recorded resources.
    prc, _, perr = run(["terraform", chdir, "plan", "-destroy", "-input=false", "-no-color",
                        f"-out={_DESTROY_PLAN}"] + targets, None)
    src, sout, _ = run(["terraform", chdir, "show", "-json", _DESTROY_PLAN], None) if prc == 0 else (1, "", "")
    try:
        planned = {c["address"] for c in json.loads(sout).get("resource_changes", [])
                   if "delete" in (c.get("change") or {}).get("actions", [])}
    except Exception:
        planned = None
    if prc != 0 or src != 0 or planned is None:
        raise InfraInputError("could not verify the destroy plan -- refusing to destroy")
    extra = sorted(planned - set(created))
    if extra:
        raise InfraInputError(f"destroy would also remove resources not created by this apply: {extra[:5]}")
    argv = ["terraform", chdir, "destroy", "-input=false", "-auto-approve", "-no-color"] + targets
    rc, out, err = run(argv, None)
    return {"status": "done" if rc == 0 else "error", "action": "destroy_delta", "cloud": cloud, "rc": rc,
            "destroyed_resources": list(created) if rc == 0 else []}


def terraform_outputs(cloud: str, *, run: Runner = _run) -> dict:
    rc, out, _ = run(["terraform", f"-chdir={_env_dir(cloud)}", "output", "-json"], None)
    if rc != 0:
        return {}
    try:
        return {k: v.get("value") for k, v in json.loads(out).items()}
    except Exception:
        return {}


# ── Ansible ──────────────────────────────────────────────────────────────────
def _resolve_playbook(playbook: str) -> str:
    if not ANSIBLE_ROOT:
        raise InfraInputError("infra deploy root is not configured: set INFRA_DEPLOY_ROOT "
                              "(no repo-relative deploy/ directory found) -- refusing to guess a path")
    if not isinstance(playbook, str) or not playbook.strip():
        raise InfraInputError("playbook is required")
    pb = playbook if os.path.isabs(playbook) else os.path.join(ANSIBLE_ROOT, playbook)
    if not _inside(ANSIBLE_ROOT, pb):
        raise InfraInputError(f"playbook {playbook!r} resolves outside the deploy root")
    return pb


@_guard("configure")
def ansible_playbook(playbook: str, *, inventory: "str | None" = None,
                     extra_vars: "dict | None" = None, run: Runner = _run) -> dict:
    pb = _resolve_playbook(playbook)
    extra_vars = _check_extra_vars(extra_vars)
    inventory = _resolve_inventory(inventory)
    argv = ["ansible-playbook", pb]
    if inventory:
        argv += ["-i", inventory]
    if extra_vars:
        argv += ["-e", json.dumps({k: v if isinstance(v, (bool, int, float)) else str(v)
                                   for k, v in extra_vars.items()})]
    rc, out, err = run(argv, ANSIBLE_ROOT)
    return {"status": "done" if rc == 0 else "error", "action": "configure",
            "playbook": playbook, "rc": rc, "summary": _last_line(out) if rc == 0 else _last_line(err)}


# ── verification ──────────────────────────────────────────────────────────────
def _resolve_host(host: str, port: int) -> "list[str]":
    import socket
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _check_host(host, port: int = 80) -> "list[str]":
    """Return the checked IPs to connect to. Rejects non-canonical numeric hosts (127.1, 0x7f.1, 2130706433),
    localhost-style names, and any host with ANY resolved address that is loopback, link-local (incl. cloud
    metadata), multicast, unspecified, reserved or (unless INFRA_VERIFY_ALLOW_PRIVATE=1) private/CGNAT."""
    import ipaddress
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
        raise InfraInputError(f"invalid host {host!r}")
    low = host.lower().rstrip(".")
    if low == "localhost" or low.endswith((".localhost", ".local", ".internal")):
        raise InfraInputError(f"host {host!r} is not an allowed verification target")
    if _NUMERICISH_RE.fullmatch(host):
        try:
            ipaddress.IPv4Address(host)                 # strict dotted quad only
        except ValueError:
            raise InfraInputError(f"host {host!r} is not a canonical address") from None
        addrs = [host]
    else:
        try:
            addrs = [str(ipaddress.ip_address(host))] if ":" in host else _resolve_host(host, port)
        except OSError:
            raise InfraInputError(f"host {host!r} does not resolve") from None
    if not addrs:
        raise InfraInputError(f"host {host!r} does not resolve")
    allow_private = os.environ.get("INFRA_VERIFY_ALLOW_PRIVATE") == "1"
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
                or str(ip) in ("169.254.169.254", "100.100.100.200", "192.0.0.192")):
            raise InfraInputError(f"host {host!r} resolves to a disallowed address")
        if not ip.is_global and not allow_private:
            raise InfraInputError(f"host {host!r} resolves to a non-public address "
                                  "(set INFRA_VERIFY_ALLOW_PRIVATE=1 to allow)")
    return addrs


@_guard("verify")
def verify(host: str, *, port: int = 8000, endpoints: tuple = ("/health", "/capabilities"),
           http_get: HttpGet = _http_get) -> dict:
    if not host:
        return {"status": "error", "action": "verify", "host": host, "checks": {}, "healthy": False}
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise InfraInputError("invalid port")
    _check_host(host, port)
    checks = {ep: http_get(f"http://{host}:{port}{ep}") for ep in endpoints}
    healthy = bool(host) and all(code == 200 for code in checks.values())
    return {"status": "done" if healthy else "error", "action": "verify",
            "host": host, "checks": checks, "healthy": healthy}


# ── drift (the infrastructure + configuration subtypes of Mission Drift, v6 §5) ──
def _ansible_changed(text: str) -> int:
    import re
    return sum(int(n) for n in re.findall(r"changed=(\d+)", text or ""))


@_guard("drift")
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
        argv = ["ansible-playbook", _resolve_playbook(check_playbook), "--check", "--diff"]
        if inventory:
            argv += ["-i", _resolve_inventory(inventory)]
        crc, cout, cerr = run(argv, ANSIBLE_ROOT)
        changed = _ansible_changed(cout)
        result["configuration"] = {"exit_code": crc, "drift": changed > 0, "changed": changed}
    result["drift"] = infra["drift"] or result.get("configuration", {}).get("drift", False)
    result["status"] = "error" if infra["error"] else "done"
    return result
