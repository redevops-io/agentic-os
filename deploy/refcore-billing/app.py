"""Reference billing core — a real, running, persistent HTTP service that speaks the Lago customer API
contract (POST/GET /api/v1/customers + Bearer auth). It exists to prove the World Adapter seam end-to-end
against a LIVE networked core: a governed world's customer entity is projected here over real HTTP and
persists. Production Lago speaks the same contract, so pointing LagoBillingAdapter at a real Lago is the
identical env swap (LAGO_API_URL/LAGO_API_KEY) — this just lets the seam be proven without Lago's full stack.
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

_TOKEN = os.environ.get("REFCORE_API_KEY", "refcore-dev-key")
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
    if authorization != f"Bearer {_TOKEN}":
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
