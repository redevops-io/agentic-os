"""P0.4 — executing an InstallPlan: stand up modules, verify, obtain credentials, record.

P0.3 produced a plan (*what* to install, gated by device posture). This slice runs it: for
each module the plan says to install, a :class:`Deployer` stands it up, the installer verifies
it publishes ``GET /capabilities`` (the federation contract), checks the credentials the
module needs are available (via the secret store — references, never values), registers the
endpoint for federation, and records everything as events. The result is a content-addressed
:class:`InstallReceipt`.

The :class:`Deployer` is a seam, mirroring the matcher seam in P0.3:

* :class:`StubDeployer` — in-process, for offline unit tests (endpoints are pre-assigned and a
  canned ``/capabilities`` is injected via ``fetch``).
* :class:`DockerDeployer` — real containers on a docker host, local or over SSH (so the tests
  run against **proxmox containers** without any Windows/macOS/iOS hardware). It runs a tiny
  embedded stub operator via ``python:3.12-alpine`` — no image to build or publish.

The install itself is posture-gated: :func:`install` refuses a plan that is not
:attr:`~.provisioning.InstallPlan.installable` (a capability the device DENYs), and a module
that fails to deploy or verify becomes a first-class failed :class:`ModuleOutcome`, never a
silent pass — the same "named refusal" discipline as the execution membrane.
"""
from __future__ import annotations

import base64
import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Protocol, Tuple

from runtime_contracts.canonical import content_hash

from .provisioning import InstallPlan, require_installable
from .util import Fetch, get_json

CONTRACT_VERSION = "install-receipt/v1"
INSTALL_SCOPE = "__install__"
MODULE_EVENT = "ModuleInstalled"
DONE_EVENT = "InstallCompleted"


# ── Deployer seam ────────────────────────────────────────────────────────────────────────

class Deployer(Protocol):
    """Stands a module up and returns its base URL. Provider-neutral (docker, compose, k8s…)."""

    def deploy(self, module: str) -> str: ...
    def teardown(self, module: str) -> None: ...


@dataclass
class StubDeployer:
    """In-process deployer for offline tests: endpoints are pre-assigned, nothing really runs."""

    endpoints: Dict[str, str] = field(default_factory=dict)
    deployed: list = field(default_factory=list)
    torn_down: list = field(default_factory=list)
    fail: Tuple[str, ...] = ()          # module ids whose deploy() should raise

    def deploy(self, module: str) -> str:
        if module in self.fail:
            raise RuntimeError("deploy boom")
        self.deployed.append(module)
        return self.endpoints.get(module, f"http://stub/{module}")

    def teardown(self, module: str) -> None:
        self.torn_down.append(module)


# ── Credential availability (references, never values) ───────────────────────────────────

def default_has_credential(name: str) -> bool:
    """True if the named secret is resolvable from the environment secret store. Best-effort:
    a missing store or ref is simply 'not available' (recorded, never raised)."""
    try:
        from runtime_contracts.models import SecretRef
        from runtime_contracts.secrets_local.store import EnvironmentSecretStore
        EnvironmentSecretStore().describe(SecretRef(provider="env", key=name))
        return True
    except Exception:
        return False


# ── Outcomes / receipt ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModuleOutcome:
    module: str
    base_url: str
    verified: bool
    operator: str = ""
    credentials_obtained: Tuple[str, ...] = ()
    credentials_missing: Tuple[str, ...] = ()
    reason: str = ""                    # named failure when not verified

    def canonical_form(self) -> Dict[str, object]:
        return {
            "module": self.module, "base_url": self.base_url, "verified": self.verified,
            "operator": self.operator,
            "credentials_obtained": sorted(self.credentials_obtained),
            "credentials_missing": sorted(self.credentials_missing),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class InstallReceipt:
    plan_id: str
    installed: Tuple[ModuleOutcome, ...]
    skipped: Tuple[str, ...] = ()                  # already-installed modules (from the plan)
    modules_yaml: Dict[str, str] = field(default_factory=dict)   # operator → url for federation
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> Dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "plan_id": self.plan_id,
            "installed": [o.canonical_form() for o in sorted(self.installed, key=lambda o: o.module)],
            "skipped": sorted(self.skipped),
            "modules_yaml": dict(sorted(self.modules_yaml.items())),
        }

    @property
    def receipt_id(self) -> str:
        return content_hash(self.canonical_form())

    @property
    def ok(self) -> bool:
        """Every module that was meant to install came up and verified."""
        return all(o.verified for o in self.installed)

    @property
    def ready(self) -> bool:
        """Verified *and* every needed credential is available — the module can actually run."""
        return self.ok and not any(o.credentials_missing for o in self.installed)


# ── The install ────────────────────────────────────────────────────────────────────────

def install(plan: InstallPlan, *, deployer: Deployer, store=None,
            fetch: Optional[Fetch] = None,
            has_credential: Callable[[str], bool] = default_has_credential,
            scope: str = INSTALL_SCOPE, verify_timeout: float = 8.0) -> InstallReceipt:
    """Execute ``plan``: deploy each to-install module, verify ``/capabilities``, check
    credentials, register endpoints, and record events. Posture-gated at the door."""
    require_installable(plan)                       # refuse a plan the device may not run

    secrets_by_module: Dict[str, set] = {}
    for rc in plan.resolved:
        secrets_by_module.setdefault(rc.module, set()).update(rc.secrets)

    outcomes: list[ModuleOutcome] = []
    yaml_map: Dict[str, str] = {}

    for module in plan.to_install:
        need = sorted(secrets_by_module.get(module, ()))
        obtained = tuple(s for s in need if has_credential(s))
        missing = tuple(s for s in need if not has_credential(s))

        try:
            base_url = deployer.deploy(module)
        except Exception as e:                      # deploy failure is a named outcome, not a crash
            outcomes.append(ModuleOutcome(module, "", False, "", obtained, missing,
                                          f"deploy_failed:{type(e).__name__}"))
            _emit(store, MODULE_EVENT, scope, outcomes[-1])
            continue

        try:
            doc = get_json(f"{base_url.rstrip('/')}/capabilities", timeout=verify_timeout, fetch=fetch)
            operator = str(doc.get("operator") or module)
            outcome = ModuleOutcome(module, base_url, True, operator, obtained, missing)
            yaml_map[operator] = base_url
        except Exception as e:
            outcome = ModuleOutcome(module, base_url, False, "", obtained, missing,
                                    f"verify_failed:{type(e).__name__}")
        outcomes.append(outcome)
        _emit(store, MODULE_EVENT, scope, outcome)

    receipt = InstallReceipt(plan_id=plan.plan_id, installed=tuple(outcomes),
                             skipped=plan.already_installed, modules_yaml=yaml_map)
    if store is not None:
        store.append(DONE_EVENT, scope, receipt.canonical_form())
    return receipt


def _emit(store, event: str, scope: str, outcome: ModuleOutcome) -> None:
    if store is not None:
        store.append(event, scope, outcome.canonical_form())


# ── DockerDeployer — real containers (local or over SSH, e.g. proxmox) ────────────────────

# A tiny stdlib-only operator: serves GET /capabilities (federation contract) + /health + POST
# /invoke. Run via python:3.12-alpine with no bind-mount or image build — operator name and the
# capabilities manifest arrive base64-encoded in the environment, so it is safe over SSH.
_STUB_OPERATOR = r'''
import base64, json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
OP = base64.b64decode(os.environ.get("OP_OPERATOR_B64", "")).decode() or "op"
CAPS = json.loads(base64.b64decode(os.environ.get("OP_MANIFEST_B64", "")).decode() or "[]")
class H(BaseHTTPRequestHandler):
    def _s(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.rstrip("/")
        if p == "/capabilities": self._s(200, {"operator": OP, "capabilities": CAPS})
        elif p in ("/health", ""): self._s(200, {"ok": True})
        else: self._s(404, {"error": "not_found"})
    def do_POST(self): self._s(200, {"ok": True, "operator": OP})
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''


@dataclass
class DockerDeployer:
    """Deploy each module as a real container on a docker host — local, or remote over SSH.

    For the proxmox integration test: ``DockerDeployer(ssh_host="proxmox", host="192.168.40.105",
    manifests={...})``. Each module runs the embedded stub operator (no image to build); the
    installer then verifies its ``/capabilities`` over the LAN exactly as federation would.
    """

    ssh_host: str = ""                              # "" = local docker; else `ssh <host> docker …`
    host: str = "127.0.0.1"                         # how the installer reaches the container
    image: str = "python:3.12-alpine"
    manifests: Dict[str, list] = field(default_factory=dict)   # module → capabilities list
    port_base: int = 34000
    _ports: Dict[str, int] = field(default_factory=dict)
    name_prefix: str = "rdo-mod-"

    def _docker(self, *args: str) -> subprocess.CompletedProcess:
        base = ["docker", *args]
        if self.ssh_host:
            remote = " ".join(shlex.quote(a) for a in base)   # quote for the remote shell
            base = ["ssh", self.ssh_host, remote]
        return subprocess.run(base, capture_output=True, text=True, timeout=60)

    def deploy(self, module: str) -> str:
        import random
        port = self.port_base + random.randint(0, 900)
        self._ports[module] = port
        name = f"{self.name_prefix}{module}"
        self._docker("rm", "-f", name)              # best-effort clean of a stale container
        b64 = lambda s: base64.b64encode(s.encode()).decode()
        exec_arg = f"import base64;exec(base64.b64decode('{b64(_STUB_OPERATOR)}'))"
        r = self._docker(
            "run", "-d", "--rm", "--name", name, "-p", f"{port}:8080",
            "-e", f"OP_OPERATOR_B64={b64(module)}",
            "-e", f"OP_MANIFEST_B64={b64(json.dumps(self.manifests.get(module, [])))}",
            self.image, "python", "-c", exec_arg)
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {r.stderr.strip()[:200]}")
        url = f"http://{self.host}:{port}"
        self._wait_healthy(url)
        return url

    def _wait_healthy(self, url: str, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                get_json(f"{url}/health", timeout=2.0)
                return
            except Exception as e:                  # noqa: BLE001 — poll until up
                last = str(e)
                time.sleep(0.5)
        raise RuntimeError(f"container did not become healthy at {url}: {last}")

    def teardown(self, module: str) -> None:
        self._docker("rm", "-f", f"{self.name_prefix}{module}")
