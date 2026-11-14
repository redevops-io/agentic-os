"""ProductClient: Projects' server-to-server HTTP hop to ONE product's own API.

Rules enforced here, not left to callers:

* the base URL is operator configuration (``registry.validate_url``), never a request value; no redirect is ever
  followed; a response is read at most ``MAX_BYTES`` (2 MiB) and must be JSON;
* the credential is a dedicated per-product service token (``X-Control-Token``), read from a file at call time and
  never logged, never put in a URL, never forwarded from the browser; a decision/execute call may use the second
  token of the pair (``decision_token_file``) so a token that can only read and stage is distinct from one that can
  carry a human decision;
* the end user's own Projects credential is never sent to a product (``Authorization`` carries only the product's own
  service token, and only for a product registered with ``auth.scheme: bearer``); who is acting
  travels as the informational ``X-Acting-User`` / ``X-Acting-Source`` headers, and a human decision additionally
  carries a signed, single-use, action-digest-bound ``X-Approval-Token`` minted by ``approvals.py``;
* every failure is a typed :class:`ProductError` carrying the product's own error ``code``, ``reason`` and ``remedy``
  when it gave them, and ``unavailable`` for transport problems and 5xx, so callers can render an honest state.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, Mapping, Optional

import httpx

from .registry import ProductSpec

MAX_BYTES = 2 * 1024 * 1024
CONTROL_HEADER = "X-Control-Token"
APPROVAL_HEADER = "X-Approval-Token"
ACTOR_HEADER = "X-Acting-User"
SOURCE_HEADER = "X-Acting-Source"
_SAFE_ACTOR = re.compile(r"[^A-Za-z0-9._:@-]")


class ProductError(Exception):
    """A product call that did not produce a usable answer."""

    def __init__(self, code: str, detail: str = "", *, status: int = 0, reason: str = "", remedy: str = "",
                 body: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.status = code, detail, status
        self.reason, self.remedy = reason, remedy
        self.body = dict(body or {})

    @property
    def unavailable(self) -> bool:
        return self.code in ("unavailable", "timeout", "unreachable", "bad_response", "no_token")

    def as_dict(self) -> Dict[str, Any]:
        out = {"error": self.code, "detail": self.detail}
        if self.reason:
            out["reason"] = self.reason
        if self.remedy:
            out["remedy"] = self.remedy
        return out


def _read_secret(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _clean_actor(actor: str) -> str:
    return _SAFE_ACTOR.sub("_", actor or "")[:80]


class ProductClient:
    def __init__(self, spec: ProductSpec, *, transport: Optional[httpx.BaseTransport] = None) -> None:
        self.spec = spec
        self._lock = threading.Lock()
        self._http = httpx.Client(base_url=spec.api_base, follow_redirects=False, trust_env=False,
                                  timeout=httpx.Timeout(max(spec.health_timeout_s, 5.0), connect=spec.health_timeout_s + 2),
                                  transport=transport, limits=httpx.Limits(max_connections=8, max_keepalive_connections=2))

    # ── credentials ──────────────────────────────────────────────────────────────────────────────
    def service_token(self) -> str:
        return _read_secret(self.spec.service_token_file)

    def decision_token(self) -> str:
        return _read_secret(self.spec.decision_token_file) or self.service_token()

    def approval_secret(self) -> str:
        return _read_secret(self.spec.approval_secret_file)

    @property
    def configured(self) -> bool:
        return bool(self.service_token())

    # ── transport ────────────────────────────────────────────────────────────────────────────────
    def _send(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None,
              body: Optional[Mapping[str, Any]] = None, auth: Optional[str] = "service",
              headers: Optional[Mapping[str, str]] = None, actor: str = "", source: str = "projects",
              timeout: Optional[float] = None, json_required: bool = True) -> Any:
        if not path.startswith("/") or "://" in path or ".." in path.split("?")[0].split("/"):
            raise ProductError("invalid", "path must be a product-relative absolute path")
        hdrs: Dict[str, str] = {"Accept": "application/json", "User-Agent": "agentic-os-products/1"}
        if auth is not None:
            tok = self.decision_token() if auth == "decision" else self.service_token()
            if not tok:
                raise ProductError("no_token", f"no {auth} token configured for {self.spec.id}",
                                   reason="the product service token file is empty or unreadable",
                                   remedy="set auth.service_token_file in the products registry")
            if self.spec.auth_scheme == "bearer":
                hdrs["Authorization"] = f"Bearer {tok}"          # the PRODUCT's service token, never the Projects user's credential
            else:
                hdrs[CONTROL_HEADER] = tok
        if actor:
            hdrs[ACTOR_HEADER] = _clean_actor(actor)
            hdrs[SOURCE_HEADER] = _clean_actor(source)
        if headers:
            hdrs.update(headers)
        content = None
        if body is not None:
            content = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
            hdrs["Content-Type"] = "application/json"
        try:
            with self._http.stream(method, path, params=dict(params or {}), content=content, headers=hdrs,
                                   timeout=timeout) as r:
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ProductError("bad_response", "response larger than 2 MiB", status=r.status_code)
                    chunks.append(chunk)
                raw = b"".join(chunks)
                status = r.status_code
                ctype = r.headers.get("content-type", "")
        except ProductError:
            raise
        except httpx.TimeoutException:
            raise ProductError("timeout", f"{self.spec.id} did not answer in time") from None
        except httpx.HTTPError as e:
            raise ProductError("unreachable", f"{self.spec.id} is not reachable ({type(e).__name__})") from None
        if 300 <= status < 400:
            raise ProductError("bad_response", "redirects are not followed", status=status)
        data: Any = None
        if raw and not json_required and status < 400:
            return None
        if raw:
            if "json" not in ctype.lower():
                if status >= 400:
                    raise ProductError("unavailable" if status >= 500 else "error", f"HTTP {status}", status=status)
                raise ProductError("bad_response", "response is not JSON", status=status)
            try:
                data = json.loads(raw)
            except ValueError:
                raise ProductError("bad_response", "response is not valid JSON", status=status) from None
        if status >= 400:
            d = data if isinstance(data, dict) else {}
            code = d.get("error") if isinstance(d.get("error"), str) else None
            detail = d.get("detail") if isinstance(d.get("detail"), str) else ""
            if code is None:
                code = {401: "unauthenticated", 403: "forbidden", 404: "not_found", 409: "conflict", 422: "invalid",
                        429: "rate_limited", 503: "unavailable"}.get(status, "unavailable" if status >= 500 else "error")
            if status >= 500 and code not in ("unavailable",):
                code = "unavailable"
            raise ProductError(code, detail, status=status, reason=str(d.get("reason") or ""),
                               remedy=str(d.get("remedy") or ""), body=d)
        return data

    def get(self, path: str, *, params: Optional[Mapping[str, Any]] = None, auth: Optional[str] = "service",
            actor: str = "", source: str = "projects") -> Any:
        return self._send("GET", path, params=params, auth=auth, actor=actor, source=source)

    def post(self, path: str, body: Mapping[str, Any], *, auth: str = "service", actor: str = "",
             source: str = "projects", headers: Optional[Mapping[str, str]] = None) -> Any:
        return self._send("POST", path, body=body, auth=auth, actor=actor, source=source, headers=headers)

    def probe(self) -> Dict[str, Any]:
        """The public health probe (no credential). Returns ``{"ok": bool, "detail": str}``."""
        try:
            self._send("GET", self.spec.health_path, auth=None, timeout=self.spec.health_timeout_s,
                       json_required=False)
            return {"ok": True, "detail": "ready"}
        except ProductError as e:
            return {"ok": False, "detail": e.detail or e.code}

    def close(self) -> None:
        self._http.close()


def new_client(spec: ProductSpec, *, transport: Optional[httpx.BaseTransport] = None) -> ProductClient:
    return ProductClient(spec, transport=transport)


__all__ = ["ProductClient", "ProductError", "new_client", "MAX_BYTES", "CONTROL_HEADER", "APPROVAL_HEADER",
           "ACTOR_HEADER", "SOURCE_HEADER"]
