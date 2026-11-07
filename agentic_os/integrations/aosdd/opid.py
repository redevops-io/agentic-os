"""Deterministic operation identifiers and the application-visible markers derived from them.

``op_id`` depends only on (mission id, step, canonical target, attempt): the same mission, step and target always name the same
operation, in any process, before any request is made. The marker is what is written INTO the application so that, after a
crash or a lost reply, the adapter can find its own record again instead of writing a second one."""
from __future__ import annotations

import hashlib
import json
import uuid

_NS = uuid.UUID("5d0f2a8e-2d1b-5f55-9d3e-0a05dd000001")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(obj) -> str:
    return hashlib.sha256((obj if isinstance(obj, (bytes, bytearray)) else canonical(obj).encode())).hexdigest()


def target_digest(target: dict) -> str:
    return sha256_hex(target)


def op_id(mission_id: str, step: str, target: dict, attempt: int = 1) -> str:
    """``aosdd.<mission>.<step>.a<attempt>.<10 hex of sha256(mission|step|target|attempt)>``"""
    h = hashlib.sha256(f"{mission_id}|{step}|{target_digest(target)}|{int(attempt)}".encode()).hexdigest()[:10]
    return f"aosdd.{mission_id}.{step}.a{int(attempt)}.{h}"


def valid(op: str, mission_id: str, step: str, target: dict, attempt: int = 1) -> bool:
    return isinstance(op, str) and op == op_id(mission_id, step, target, attempt)


def twenty_uuid(op: str, what: str) -> str:
    """The client-supplied Twenty record id (the app honours it): a pure function of the operation id."""
    return str(uuid.uuid5(_NS, f"{op}|{what}"))


def erp_title(op: str) -> str:
    return "AOSDD-" + op.replace("aosdd.", "", 1)


def chatwoot_marker(op: str) -> str:
    return f"[{op}]"
