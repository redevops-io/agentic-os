"""Reusable HTTP IntegrationProvider base (§4).

Validates that the IntegrationProvider contract works against a REAL REST API shape, not just the in-memory fake.
A subclass declares, per object type, how to build the path, normalize a response into canonical fields, and
denormalize fields into a request body; the base handles the contract methods, normalized-error mapping, and
read-after-write verification. Credential-gated and offline-testable via an injected transport (same seam as the
intelligence/collaboration adapters).
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from ..contracts import Observation, _now
from ..provider import (
    ActionResult, IntegrationError, IntegrationErrorCode, ProviderHealth,
)

# transport(method, url, headers, body) -> (status, parsed_json_or_text)
Transport = Callable[[str, str, Optional[dict], Optional[dict]], "tuple[int, Any]"]


def http_json(method: str, url: str, headers: Optional[dict] = None, body: Optional[dict] = None,
              timeout: float = 20.0) -> "tuple[int, Any]":
    data = _json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed provider hosts
            raw = r.read().decode("utf-8")
            return r.status, (_json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}


def status_to_error(status: int) -> Optional[IntegrationErrorCode]:
    return {
        401: IntegrationErrorCode.AUTH_EXPIRED, 403: IntegrationErrorCode.PERMISSION_DENIED,
        404: IntegrationErrorCode.OBJECT_NOT_FOUND, 409: IntegrationErrorCode.CONFLICT,
        422: IntegrationErrorCode.VALIDATION_FAILED, 429: IntegrationErrorCode.RATE_LIMITED,
    }.get(status) or (IntegrationErrorCode.PROVIDER_UNAVAILABLE if status >= 500 else None)


class HttpIntegrationProvider:
    """Base for REST-backed providers. Subclasses set ``provider``/``_caps`` and implement ``_path``,
    ``_normalize`` and ``_denormalize`` (+ optionally ``_extract`` / ``_list``)."""
    provider: str = "?"
    _caps: tuple[str, ...] = ()

    def __init__(self, base_url: str, *, token: str = "", transport: Transport = http_json):
        self._base = base_url.rstrip("/")
        self._token = token
        self._t = transport

    # ── subclass hooks ──
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _path(self, object_type: str, external_id: str = "") -> str:
        raise NotImplementedError

    def _normalize(self, object_type: str, raw: dict) -> dict:
        """Provider response body → canonical field dict (must include an 'id')."""
        raise NotImplementedError

    def _denormalize(self, object_type: str, fields: dict) -> dict:
        """Canonical fields → request body."""
        return dict(fields)

    def _unwrap(self, resp: Any) -> dict:
        """Pull the object out of a provider envelope (overridable; default assumes the object is top-level)."""
        return resp if isinstance(resp, dict) else {}

    # ── contract ──
    def describe_capabilities(self) -> tuple[str, ...]:
        return self._caps

    def health(self) -> ProviderHealth:
        try:
            status, _ = self._t("GET", f"{self._base}/", self._headers(), None)
            return ProviderHealth(self.provider, healthy=status < 500, auth_ok=status not in (401, 403))
        except Exception as e:  # noqa: BLE001
            return ProviderHealth(self.provider, healthy=False, auth_ok=False, detail=str(e))

    def _obs(self, object_type: str, fields: dict) -> Observation:
        return Observation(resource_id=self.provider, object_type=object_type,
                           external_id=str(fields.get("id", "")), normalized_fields=fields, known_at=_now())

    def read_object(self, object_type: str, external_id: str) -> Optional[Observation]:
        status, resp = self._t("GET", f"{self._base}{self._path(object_type, external_id)}", self._headers(), None)
        if status == 404:
            return None
        err = status_to_error(status)
        if err:
            raise IntegrationError(err, f"{self.provider} read {object_type}/{external_id}")
        return self._obs(object_type, self._normalize(object_type, self._unwrap(resp)))

    def search_objects(self, object_type: str, query: dict) -> list[Observation]:
        status, resp = self._t("GET", f"{self._base}{self._path(object_type)}", self._headers(), None)
        err = status_to_error(status)
        if err:
            raise IntegrationError(err, f"{self.provider} search {object_type}")
        rows = resp.get("data", resp) if isinstance(resp, dict) else resp
        out = []
        for raw in (rows or []):
            f = self._normalize(object_type, raw)
            if all(f.get(k) == v for k, v in query.items()):
                out.append(self._obs(object_type, f))
        return out

    def _write(self, method: str, object_type: str, external_id: str, fields: dict, idempotency_key: str) -> ActionResult:
        headers = self._headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        path = self._path(object_type, external_id) if external_id else self._path(object_type)
        status, resp = self._t(method, f"{self._base}{path}", headers, self._denormalize(object_type, fields))
        err = status_to_error(status)
        if err:
            return ActionResult(ok=False, error=err, detail=f"http {status}")
        f = self._normalize(object_type, self._unwrap(resp))
        return ActionResult(ok=True, external_id=str(f.get("id", external_id)), fields=f, idempotency_key=idempotency_key)

    def create_object(self, object_type: str, fields: dict, *, idempotency_key: str = "") -> ActionResult:
        return self._write("POST", object_type, "", fields, idempotency_key)

    def update_object(self, object_type: str, external_id: str, fields: dict, *, idempotency_key: str = "") -> ActionResult:
        return self._write("PUT", object_type, external_id, fields, idempotency_key)

    def execute_action(self, capability: str, inputs: dict, *, idempotency_key: str = "") -> ActionResult:
        object_type = inputs.get("object_type", capability.split(".")[1] if "." in capability else "object")
        ext = inputs.get("external_id", "")
        fields = inputs.get("fields", {})
        return (self.update_object(object_type, ext, fields, idempotency_key=idempotency_key) if ext
                else self.create_object(object_type, fields, idempotency_key=idempotency_key))

    def verify_action(self, object_type: str, external_id: str) -> Optional[Observation]:
        return self.read_object(object_type, external_id)
