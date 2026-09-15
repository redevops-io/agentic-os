"""HTTP operator bridge — drive the DEPLOYED agent services as capability operators.

The in-process ``LocalOperatorClient`` runs co-located operators; this is its remote twin. It lets the
unified control surface (Sidekick / Missions) discover and invoke capabilities on the *deployed* Agentic
App services over their ``GET /capabilities`` + ``POST /invoke`` seam — so one Sidekick UI issues real
commands across all apps instead of each app being operated through its own UI.

Credential-invisible by construction: the bridge never sends a credential to an agent. Each deployed agent
resolves its OWN provider credentials through its OWN CredentialBroker at execution time (see crm.py). The
caller only sends ``{capability, inputs}`` and gets a result — the secret never crosses the wire.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

Transport = Callable[[str, str, Optional[bytes], Dict[str, str]], Dict[str, Any]]


def _urllib_transport(method: str, url: str, body: Optional[bytes], headers: Dict[str, str]) -> Dict[str, Any]:
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteCapability:
    """A capability discovered on a deployed agent (UI-safe view)."""
    operator: str
    name: str
    inputs: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, str] = field(default_factory=dict)
    side_effecting: bool = False
    approval_required: bool = False
    required_authority: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {"operator": self.operator, "name": self.name, "inputs": self.inputs,
                "outputs": self.outputs, "side_effecting": self.side_effecting,
                "approval_required": self.approval_required,
                "required_authority": list(self.required_authority)}


class HTTPOperatorClient:
    """An ``OperatorClient`` (same shape the Executor calls) over the agents' HTTP ``/invoke``. ``bases``
    maps an operator name → the agent's base URL. ``secrets`` is intentionally NOT forwarded — a deployed
    agent resolves its own credentials; the bridge stays credential-free."""

    def __init__(self, bases: Dict[str, str], *, transport: Optional[Transport] = None) -> None:
        self._bases = {k: v.rstrip("/") for k, v in bases.items()}
        self._t = transport or _urllib_transport
        self.calls: List[Tuple[str, str]] = []   # (operator, capability) — for assertions

    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str = "",
               *, secrets: "dict | None" = None) -> dict:
        base = self._bases.get(operator)
        if base is None:
            raise BridgeError(f"no deployed agent registered for operator '{operator}'")
        self.calls.append((operator, capability))
        body = json.dumps({"capability": capability, "inputs": inputs or {},
                           "idempotency_key": idempotency_key or ""}).encode()
        try:
            resp = self._t("POST", f"{base}/invoke", body,
                           {"Content-Type": "application/json", "Idempotency-Key": idempotency_key or ""})
        except Exception as e:  # noqa: BLE001
            raise BridgeError(f"invoke {operator}.{capability} failed: {type(e).__name__}") from None
        # the agent's router returns {"result": {...}}; unwrap to the plain result
        return resp.get("result", resp)


def discover_capabilities(agent_urls: Dict[str, str], *, transport: Optional[Transport] = None
                          ) -> Tuple[List[RemoteCapability], Dict[str, str]]:
    """GET ``/capabilities`` from each agent URL and return (capabilities, operator→url). Agents that don't
    publish an operator (no ``/capabilities``) are skipped gracefully — the bridge only lists what's real."""
    t = transport or _urllib_transport
    caps: List[RemoteCapability] = []
    bases: Dict[str, str] = {}
    for _key, url in agent_urls.items():
        url = url.rstrip("/")
        try:
            manifest = t("GET", f"{url}/capabilities", None, {})
        except Exception:  # noqa: BLE001 — an agent without an operator surface is simply not bridged
            continue
        op = manifest.get("operator") or _key
        for c in manifest.get("capabilities") or []:
            caps.append(RemoteCapability(
                operator=c.get("operator") or op, name=c.get("name", ""),
                inputs=c.get("inputs") or {}, outputs=c.get("outputs") or {},
                side_effecting=bool(c.get("side_effecting")),
                approval_required=bool(c.get("approval_required")),
                required_authority=tuple(c.get("required_authority") or ())))
            bases[c.get("operator") or op] = url
    return caps, bases


def build_bridge(agent_urls: Dict[str, str], *, transport: Optional[Transport] = None
                 ) -> Tuple[List[RemoteCapability], HTTPOperatorClient]:
    """Discover the deployed agents' capabilities and return (capabilities, HTTPOperatorClient) so the
    unified surface can both LIST what every app offers and INVOKE any of it — one control surface, all
    apps. ``agent_urls`` is the same map the console uses (REVENUE_URL/FINANCE_URL/…)."""
    caps, bases = discover_capabilities(agent_urls, transport=transport)
    return caps, HTTPOperatorClient(bases, transport=transport)
