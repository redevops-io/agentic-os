"""ProductHub: the registry plus one ProductClient per product, health/info state, and a bounded read cache.

The cache is NEVER authoritative and is never used for a decision: approvals always re-read the action from the
product (``approvals.py``). Reads are cached for ``READ_TTL_S`` so that one Projects page load (missions, attention,
today, activity) costs one product call, not four. Health is probed at most every ``health_interval_s`` per product
and the authenticated ``info`` call (version, features, ``unavailable[]``) rides on the same refresh.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import gates
from .client import ProductClient, ProductError
from .qa import QaAdapter
from .registry import ProductRegistry, ProductSpec
from .sentinel import SentinelAdapter

READ_TTL_S = 4.0


class ProductHub:
    def __init__(self, registry: ProductRegistry, *, transports: Optional[Dict[str, Any]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.registry = registry
        self._clock = clock
        self._lock = threading.RLock()
        self._clients: Dict[str, ProductClient] = {
            s.id: ProductClient(s, transport=(transports or {}).get(s.id)) for s in registry.all()}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._status_at: Dict[str, float] = {}
        self._cache: Dict[str, "tuple[float, Any]"] = {}

    # ── lookup ────────────────────────────────────────────────────────────────────────────────────
    def specs(self) -> List[ProductSpec]:
        return self.registry.all()

    def spec(self, product_id: str) -> Optional[ProductSpec]:
        return self.registry.get(product_id)

    def client(self, product_id: str) -> Optional[ProductClient]:
        return self._clients.get(product_id)

    def sentinel(self, product_id: str) -> SentinelAdapter:
        return SentinelAdapter(self._clients[product_id])

    def qa(self, product_id: str) -> QaAdapter:
        return QaAdapter(self._clients[product_id])

    # ── cache ─────────────────────────────────────────────────────────────────────────────────────
    def cached(self, key: str, fn: Callable[[], Any], ttl: float = READ_TTL_S) -> Any:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self._lock:
            self._cache[key] = (now, val)
            if len(self._cache) > 256:
                for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:64]:
                    self._cache.pop(k, None)
        return val

    def invalidate(self, product_id: str = "") -> None:
        with self._lock:
            for k in [k for k in self._cache if not product_id or k.startswith(product_id + ":")]:
                self._cache.pop(k, None)
            if product_id:
                self._status_at.pop(product_id, None)

    # ── status ────────────────────────────────────────────────────────────────────────────────────
    def status(self, product_id: str, *, force: bool = False) -> Dict[str, Any]:
        spec = self._clients[product_id].spec if product_id in self._clients else None
        if spec is None:
            raise KeyError(product_id)
        now = self._clock()
        with self._lock:
            st = self._status.get(product_id)
            if st is not None and not force and now - self._status_at.get(product_id, -1e9) < spec.health_interval_s:
                return st
        st = self._compute(spec)
        with self._lock:
            self._status[product_id] = st
            self._status_at[product_id] = now
        return st

    def statuses(self, *, force: bool = False) -> List[Dict[str, Any]]:
        return [self.status(s.id, force=force) for s in self.specs()]

    def _compute(self, spec: ProductSpec) -> Dict[str, Any]:
        cl = self._clients[spec.id]
        unavailable: List[Dict[str, str]] = []
        gate = gates.gate_for(spec.kind)
        base: Dict[str, Any] = {
            "id": spec.id, "display_name": spec.display_name, "kind": spec.kind, "ui_url": spec.ui_url,
            "version": "", "features": {}, "unavailable": unavailable, "gate": gate,
            "health": {"state": "unknown", "checked_at": time.time(), "detail": ""}, "configured": cl.configured,
            "ready": False, "tools_available": False, "approval_secret": bool(cl.approval_secret())}
        if not gate["open"]:
            unavailable.append({"feature": "*", "code": gate["code"], "reason": gate["reason"], "remedy": gate["remedy"]})
            # the gate decides before anything is probed: a gated product is never called by Projects
            base["health"] = {"state": "unknown", "checked_at": time.time(), "detail": gate["reason"]}
            return base
        if not cl.configured:
            unavailable.append({"feature": "*", "code": "not_configured",
                                "reason": "no service token is configured for this product",
                                "remedy": f"set auth.service_token_file for {spec.id} in the products registry"})
            base["health"] = {"state": "unknown", "checked_at": time.time(), "detail": "not configured"}
            return base
        probe = cl.probe()
        if not probe["ok"]:
            unavailable.append({"feature": "*", "code": "unreachable",
                                "reason": f"{spec.display_name} did not answer its readiness probe ({probe['detail']})",
                                "remedy": f"start {spec.display_name} or check api_base for {spec.id} in the products registry"})
            base["health"] = {"state": "down", "checked_at": time.time(), "detail": probe["detail"]}
            return base
        try:
            info = cl.get(spec.info_path)
        except ProductError as e:
            unavailable.append({"feature": "*", "code": e.code,
                                "reason": e.reason or f"{spec.display_name} refused the service token ({e.code})"
                                if e.code in ("unauthenticated", "forbidden") else (e.reason or e.detail or e.code),
                                "remedy": e.remedy or ("check the service token pair of this product" if e.code in
                                                       ("unauthenticated", "forbidden") else "retry later")})
            base["health"] = {"state": "degraded", "checked_at": time.time(), "detail": e.code}
            return base
        info = info if isinstance(info, dict) else {}
        base["version"] = str(info.get("version") or "")
        feats = info.get("features")
        base["features"] = dict(feats) if isinstance(feats, dict) else {}
        for u in info.get("unavailable") or []:
            if isinstance(u, dict):
                unavailable.append({k: str(u.get(k, "")) for k in ("feature", "code", "reason", "remedy")})
        base["info"] = {k: info[k] for k in ("mode", "tenant_label", "core", "levels", "platform") if k in info}
        if not base["approval_secret"]:
            unavailable.append({"feature": "approvals", "code": "no_approval_secret",
                                "reason": "no approval secret is configured, so a decision cannot be made from Projects",
                                "remedy": f"set auth.approval_secret_file for {spec.id}; decide in {spec.display_name} meanwhile"})
        base["health"] = {"state": "ok", "checked_at": time.time(), "detail": "ready"}
        base["ready"] = True
        base["tools_available"] = True
        return base

    def with_mode(self, product_id: str, rec: Any) -> Any:
        """An action record carrying ``mode`` (the product's info.mode) when the product's list rows do not."""
        if not isinstance(rec, dict) or "mode" in rec:
            return rec
        mode = (self._status.get(product_id) or {}).get("info", {}).get("mode")
        return {**rec, "mode": mode} if isinstance(mode, dict) else rec

    def ready(self, product_id: str) -> bool:
        try:
            return bool(self.status(product_id)["ready"])
        except KeyError:
            return False

    def unavailable_reason(self, product_id: str) -> Dict[str, str]:
        st = self.status(product_id)
        for u in st["unavailable"]:
            if u.get("feature") == "*":
                return u
        return {"feature": "*", "code": "unavailable", "reason": "product unavailable", "remedy": ""}

    def close(self) -> None:
        for c in self._clients.values():
            c.close()


__all__ = ["ProductHub", "READ_TTL_S"]
