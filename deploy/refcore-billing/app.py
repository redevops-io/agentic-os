"""Reference billing core — a real, running, persistent HTTP service that speaks the Lago customer API
contract (POST/GET /api/v1/customers + Bearer auth). It exists to prove the World Adapter seam end-to-end
against a LIVE networked core: a governed world's customer entity is projected here over real HTTP and
persists. Production Lago speaks the same contract, so pointing LagoBillingAdapter at a real Lago is the
identical env swap (LAGO_API_URL/LAGO_API_KEY) — this just lets the seam be proven without Lago's full stack.

The service refuses to start unless REFCORE_API_KEY is set to a non-empty key of your own choosing. There is no
default key. A key that is empty, only whitespace, or the retired development key `refcore-dev-key` is refused.
"""
from __future__ import annotations

import hmac
import json
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

_RETIRED_DEFAULT_KEYS = frozenset({"refcore-dev-key"})


def _require_key(env=None) -> str:
    """The API key from REFCORE_API_KEY. Raises RuntimeError (the process does not start) when it is absent, empty,
    whitespace-only or the retired default; the value is never included in the message."""
    key = (os.environ if env is None else env).get("REFCORE_API_KEY", "")
    if not key.strip():
        raise RuntimeError("REFCORE_API_KEY is not set: set it to a secret of your own choosing (there is no default key)")
    if key.strip() in _RETIRED_DEFAULT_KEYS:
        raise RuntimeError("REFCORE_API_KEY is the retired development default: set a secret of your own choosing")
    return key


_TOKEN = _require_key()
_DB = os.environ.get("REFCORE_DB", "/data/customers.json")

app = FastAPI(title="Reference Billing Core (Lago-contract)")


def _load() -> dict:
    try:
        with open(_DB) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save(store: dict) -> None:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    with open(_DB, "w") as f:
        json.dump(store, f)


def _auth(authorization: str) -> None:
    if not hmac.compare_digest(authorization.encode("utf-8"), f"Bearer {_TOKEN}".encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/healthz")
def healthz():
    return {"ok": True, "customers": len(_load())}


@app.get("/api/v1/customers")
def list_customers(authorization: str = Header(default="")):
    _auth(authorization)
    return {"customers": list(_load().values())}


@app.post("/api/v1/customers")
def upsert_customer(body: dict, authorization: str = Header(default="")):
    _auth(authorization)
    cust = (body or {}).get("customer") or {}
    ext = cust.get("external_id")
    if not ext:
        return JSONResponse({"error": "external_id required"}, status_code=422)
    store = _load()
    store[ext] = {"external_id": ext, "name": cust.get("name", ""), "lago_id": f"lago-{ext}"}
    _save(store)
    return {"customer": store[ext]}
