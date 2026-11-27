"""Portable application definitions (``aos.app-definition/v1``): an application as DATA that any agent, model or client
can read and write, run by this runtime.

A definition names the gateway capabilities the application uses (each with why), the state it keeps (typed collections),
and the views a person sees (text, a table of records, a form that adds one, the changes waiting for approval). It holds
no code, no HTML, no credential and no figures. What follows is the runtime's:

* **Installing is a person's decision.** An agent can only propose a definition (``apps.request_install``); the card shows
  the capabilities, which of them propose changes, the permissions an agent needs to use it, and its state. The exact
  definition, by digest, is what is approved. A newer version is installed the same way; an older or equal one never.
* **Permissions are derived, never declared.** The grants the app needs are those of its capabilities. Installing grants
  nothing: every call an agent or person makes is authorized as it always is.
* **Typed capabilities are pinned.** The input schema of each capability is hashed at install. If a capability changes or
  disappears (a provider change, an update), the app says which.
* **State lives here**, per tenant, validated against the definition, with a revision per record.
* **Portable.** The definition is plain JSON and nothing in it depends on a model, a vendor or an artifact format.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

SCHEMA = "aos.app-definition/v1"
MAX_BYTES = 32_768
FIELD_TYPES = ("text", "long_text", "number", "boolean", "date", "choice")
BLOCKS = {"text": ("text",), "records": ("collection", "columns"), "form": ("collection", "submit"), "approvals": ()}
_ID = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DefinitionError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest_of(definition: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(definition)).hexdigest()


def _keys(value: Any, where: str, required: tuple, optional: tuple = ()) -> dict:
    if not isinstance(value, dict):
        raise DefinitionError(f"{where} must be an object")
    missing, extra = [k for k in required if k not in value], sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        raise DefinitionError(f"{where}: " + "; ".join(x for x in (f"missing {', '.join(missing)}" if missing else "",
                                                                   f"unknown {', '.join(extra)}" if extra else "") if x))
    return value


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _CONTROL.search(value):
        raise DefinitionError(f"{where} must be 1 to {limit} characters of plain text")
    return value


def _list(value: Any, where: str, low: int, high: int) -> list:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise DefinitionError(f"{where} must be a list of {low} to {high}")
    return value


def _unique(names: List[str], where: str) -> None:
    if len(set(names)) != len(names):
        raise DefinitionError(f"{where} must not repeat a name")


def validate(definition: Any, catalog: Mapping[str, Any]) -> Dict[str, Any]:
    """The definition, unchanged, when every part of it is understood; ``DefinitionError`` otherwise. ``catalog`` maps a
    capability name to its gateway manifest. Nothing unknown is ignored."""
    d = _keys(definition, "definition", ("schema", "id", "name", "version", "summary", "capabilities", "state", "views"))
    if len(canonical(d)) > MAX_BYTES:
        raise DefinitionError(f"a definition is at most {MAX_BYTES} bytes")
    if d["schema"] != SCHEMA:
        raise DefinitionError(f"schema must be {SCHEMA}")
    if not isinstance(d["id"], str) or not _ID.match(d["id"]):
        raise DefinitionError("id must be lower-case letters, digits and hyphens (2 to 48)")
    if not isinstance(d["version"], int) or isinstance(d["version"], bool) or not 1 <= d["version"] <= 100_000:
        raise DefinitionError("version must be a whole number from 1")
    _text(d["name"], "name", 80)
    _text(d["summary"], "summary", 500)
    used = []
    for c in _list(d["capabilities"], "capabilities", 0, 30):
        _keys(c, "a capability", ("name", "why"))
        _text(c["why"], "why", 200)
        if c["name"] not in catalog or str(c["name"]).startswith("apps."):
            raise DefinitionError(f"capability {c['name']!r} is not one this runtime offers to applications")
        used.append(c["name"])
    _unique(used, "capabilities")
    collections: Dict[str, Dict[str, dict]] = {}
    for col in _list(_keys(d["state"], "state", ("collections",))["collections"], "state.collections", 0, 10):
        _keys(col, "a collection", ("name", "fields", "max_records"))
        if not isinstance(col["name"], str) or not _NAME.match(col["name"]) or col["name"] in collections:
            raise DefinitionError("each collection needs its own lower-case name")
        if not isinstance(col["max_records"], int) or isinstance(col["max_records"], bool) or not 1 <= col["max_records"] <= 1000:
            raise DefinitionError("max_records must be 1 to 1000")
        fields: Dict[str, dict] = {}
        for f in _list(col["fields"], "fields", 1, 20):
            _keys(f, "a field", ("name", "type"), ("required", "choices"))
            if not isinstance(f["name"], str) or not _NAME.match(f["name"]) or f["name"] in fields:
                raise DefinitionError("each field needs its own lower-case name")
            if f["type"] not in FIELD_TYPES:
                raise DefinitionError(f"a field's type is one of {', '.join(FIELD_TYPES)}")
            if "required" in f and not isinstance(f["required"], bool):
                raise DefinitionError("required is true or false")
            if (f["type"] == "choice") != ("choices" in f):
                raise DefinitionError("choices are for, and required by, a choice field")
            if f["type"] == "choice":
                _unique([_text(x, "a choice", 60) for x in _list(f["choices"], "choices", 1, 20)], "choices")
            fields[f["name"]] = f
        collections[col["name"]] = fields
    view_ids = []
    for view in _list(d["views"], "views", 1, 10):
        _keys(view, "a view", ("id", "title", "blocks"))
        if not isinstance(view["id"], str) or not _NAME.match(view["id"]):
            raise DefinitionError("a view id is a lower-case name")
        view_ids.append(view["id"])
        _text(view["title"], "a view title", 80)
        for block in _list(view["blocks"], "blocks", 1, 12):
            kind = block.get("type") if isinstance(block, dict) else None
            if kind not in BLOCKS:
                raise DefinitionError(f"a block's type is one of {', '.join(BLOCKS)}")
            _keys(block, f"a {kind} block", ("type", *BLOCKS[kind]))
            if kind == "text":
                _text(block["text"], "text", 2000)
            if kind in ("records", "form"):
                if block["collection"] not in collections:
                    raise DefinitionError(f"a {kind} block names a collection the state does not have")
            if kind == "records":
                columns = _list(block["columns"], "columns", 1, 12)
                if not set(columns) <= set(collections[block["collection"]]):
                    raise DefinitionError("columns must be fields of the collection")
                _unique(columns, "columns")
            if kind == "form":
                _text(block["submit"], "submit", 40)
    _unique(view_ids, "views")
    return d


def grants_needed(definition: Mapping[str, Any], catalog: Mapping[str, Any]) -> List[str]:
    out = {"apps.use"}
    for c in definition["capabilities"]:
        manifest = catalog.get(c["name"])
        out.update(manifest.permissions if manifest is not None else ())
    return sorted(out)


def contract_of(manifest: Any) -> str:
    return hashlib.sha256(canonical({"input": manifest.input_schema, "side_effecting": bool(manifest.side_effecting)})).hexdigest()


def contracts_of(definition: Mapping[str, Any], catalog: Mapping[str, Any]) -> Dict[str, str]:
    return {c["name"]: contract_of(catalog[c["name"]]) for c in definition["capabilities"]}


def contract_changes(pinned: Mapping[str, str], catalog: Mapping[str, Any]) -> List[Dict[str, str]]:
    out = []
    for name, was in sorted(pinned.items()):
        if name not in catalog:
            out.append({"capability": name, "change": "no longer offered here"})
        elif contract_of(catalog[name]) != was:
            out.append({"capability": name, "change": "its inputs or effect changed since the app was approved"})
    return out


def check_record(fields: List[dict], values: Any) -> Dict[str, Any]:
    if not isinstance(values, dict):
        raise DefinitionError("a record is an object of field values")
    by = {f["name"]: f for f in fields}
    unknown = sorted(set(values) - set(by))
    if unknown:
        raise DefinitionError(f"unknown field(s): {', '.join(unknown)}")
    out: Dict[str, Any] = {}
    for name, f in by.items():
        v = values.get(name)
        if v is None or v == "":
            if f.get("required"):
                raise DefinitionError(f"{name} is required")
            continue
        kind = f["type"]
        if kind in ("text", "long_text"):
            if not isinstance(v, str) or len(v) > (300 if kind == "text" else 4000) or _CONTROL.search(v):
                raise DefinitionError(f"{name} must be plain text of at most {300 if kind == 'text' else 4000} characters")
        elif kind == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e15:
                raise DefinitionError(f"{name} must be a number")
        elif kind == "boolean":
            if not isinstance(v, bool):
                raise DefinitionError(f"{name} must be true or false")
        elif kind == "date":
            try:
                _dt.date.fromisoformat(v if isinstance(v, str) else "")
            except ValueError:
                raise DefinitionError(f"{name} must be a date (YYYY-MM-DD)") from None
        elif v not in f["choices"]:
            raise DefinitionError(f"{name} must be one of: {', '.join(f['choices'])}")
        out[name] = v
    return out


class AppStore:
    """The installed applications and their state, per tenant. ``catalog`` returns the capabilities offered now."""

    def __init__(self, state_dir: "str | Path", catalog: Callable[[], Mapping[str, Any]] = dict, clock: Callable[[], float] = time.time) -> None:
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = root / "apps.sqlite"
        if not db.exists():
            os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
        self.catalog, self.clock, self._lock = catalog, clock, threading.RLock()
        self._db = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("CREATE TABLE IF NOT EXISTS apps(tenant TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, definition TEXT NOT NULL,"
                         " digest TEXT NOT NULL, contracts TEXT NOT NULL, installed_by TEXT NOT NULL, installed REAL NOT NULL,"
                         " PRIMARY KEY(tenant, id, version))")
        self._db.execute("CREATE TABLE IF NOT EXISTS records(tenant TEXT NOT NULL, app TEXT NOT NULL, collection TEXT NOT NULL, id TEXT NOT NULL,"
                         " data TEXT NOT NULL, revision INTEGER NOT NULL, updated_by TEXT NOT NULL, updated REAL NOT NULL,"
                         " PRIMARY KEY(tenant, app, collection, id))")

    # ── applications ──
    def _row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {"id": row["id"], "version": row["version"], "definition": json.loads(row["definition"]), "digest": row["digest"],
                "contracts": json.loads(row["contracts"]), "installed_by": row["installed_by"], "installed_at": row["installed"]}

    def current(self, tenant: str, app_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM apps WHERE tenant=? AND id=? ORDER BY version DESC LIMIT 1", (tenant, app_id)).fetchone()
        return None if row is None else self._row(row)

    def installed(self, tenant: str) -> List[Dict[str, Any]]:
        with self._lock:
            ids = [r["id"] for r in self._db.execute("SELECT DISTINCT id FROM apps WHERE tenant=? ORDER BY id", (tenant,)).fetchall()]
            return [self.current(tenant, i) for i in ids]

    def install(self, tenant: str, definition: Mapping[str, Any], *, by: str) -> Dict[str, Any]:
        catalog = self.catalog()
        validate(dict(definition), catalog)
        with self._lock:
            old = self.current(tenant, definition["id"])
            if old is not None and definition["version"] <= old["version"]:
                raise DefinitionError(f"version {old['version']} of {definition['id']} is installed: a new definition needs a higher version")
            self._db.execute("INSERT INTO apps VALUES(?,?,?,?,?,?,?,?)",
                             (tenant, definition["id"], definition["version"], json.dumps(definition), digest_of(definition),
                              json.dumps(contracts_of(definition, catalog)), by, self.clock()))
            return self.current(tenant, definition["id"])

    # ── state ──
    def collection(self, tenant: str, app_id: str, name: str) -> Dict[str, Any]:
        app = self.current(tenant, app_id)
        if app is None:
            raise DefinitionError("there is no such application")
        found = next((c for c in app["definition"]["state"]["collections"] if c["name"] == name), None)
        if found is None:
            raise DefinitionError("the application has no such collection")
        return found

    def records(self, tenant: str, app_id: str, name: str, limit: int = 100) -> List[Dict[str, Any]]:
        self.collection(tenant, app_id, name)
        with self._lock:
            rows = self._db.execute("SELECT * FROM records WHERE tenant=? AND app=? AND collection=? ORDER BY updated DESC, id LIMIT ?",
                                    (tenant, app_id, name, limit)).fetchall()
        return [{"id": r["id"], "values": json.loads(r["data"]), "revision": r["revision"], "updated_by": r["updated_by"],
                 "updated_at": r["updated"]} for r in rows]

    def add(self, tenant: str, app_id: str, name: str, values: Any, *, by: str, record_id: str = "") -> Dict[str, Any]:
        """``record_id`` makes a retry return the record already added."""
        col = self.collection(tenant, app_id, name)
        clean = check_record(col["fields"], values)
        ident = record_id or "rec_" + secrets.token_hex(8)
        with self._lock:
            old = self._db.execute("SELECT * FROM records WHERE tenant=? AND app=? AND collection=? AND id=?", (tenant, app_id, name, ident)).fetchone()
            if old is not None:
                return {"id": ident, "values": json.loads(old["data"]), "revision": old["revision"], "replayed": True}
            count = self._db.execute("SELECT count(*) FROM records WHERE tenant=? AND app=? AND collection=?", (tenant, app_id, name)).fetchone()[0]
            if count >= col["max_records"]:
                raise DefinitionError(f"the collection is full ({col['max_records']} records)")
            self._db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?)", (tenant, app_id, name, ident, json.dumps(clean), 1, by, self.clock()))
        return {"id": ident, "values": clean, "revision": 1}

    def update(self, tenant: str, app_id: str, name: str, record_id: str, values: Any, *, revision: int, by: str) -> Dict[str, Any]:
        col = self.collection(tenant, app_id, name)
        clean = check_record(col["fields"], values)
        with self._lock:
            changed = self._db.execute("UPDATE records SET data=?, revision=revision+1, updated_by=?, updated=? WHERE tenant=? AND app=?"
                                       " AND collection=? AND id=? AND revision=?",
                                       (json.dumps(clean), by, self.clock(), tenant, app_id, name, record_id, revision)).rowcount
            if changed != 1:
                raise DefinitionError("no record of that id and revision: read it again (someone may have changed it)")
        return {"id": record_id, "values": clean, "revision": revision + 1}


# ── installing, as a governed write ──────────────────────────────────────────────
def prepare_install(ctx: Any, store: AppStore, inputs: Mapping[str, Any]) -> Dict[str, Any]:
    from agentic_os.governed_writes.apps import NotApplied  # noqa: PLC0415
    definition = inputs["definition"]
    catalog = store.catalog()
    try:
        validate(definition, catalog)
        old = store.current(ctx.tenant, definition["id"])
        if old is not None and definition["version"] <= old["version"]:
            raise DefinitionError(f"version {old['version']} is installed: propose a higher version")
    except DefinitionError as e:
        raise NotApplied(f"the definition is not acceptable: {e}") from None
    proposes = [c["name"] for c in definition["capabilities"] if catalog[c["name"]].side_effecting]
    show = [["Application", f"{definition['name']} ({definition['id']}), version {definition['version']}"
                            + (f", replacing version {old['version']}" if old else "")], ["What it is for", definition["summary"]]]
    show += [[f"Uses {c['name']}" + (" (proposes or makes changes)" if c["name"] in proposes else " (reads)"), c["why"]]
             for c in definition["capabilities"]]
    show.append(["An agent needs these permissions to use it (installing grants none)", ", ".join(grants_needed(definition, catalog))])
    show.append(["It keeps", "; ".join(f"{c['name']} ({', '.join(f['name'] for f in c['fields'])}; up to {c['max_records']})"
                                       for c in definition["state"]["collections"]) or "no state"])
    show.append(["A person sees", ", ".join(v["title"] for v in definition["views"])])
    # every word the author wrote for people to read: it can claim anything, so the approver reads it before it is shown to anyone
    for view in definition["views"]:
        for block in view["blocks"]:
            if block["type"] == "text":
                show.append([f"Text in “{view['title']}” (written by the author, not checked)", block["text"]])
            if block["type"] == "form":
                show.append([f"Button in “{view['title']}” (adds to {block['collection']} at once, without an approval)", block["submit"]])
    return {"definition_sha256": digest_of(definition), "contracts": contracts_of(definition, catalog), "show": show,
            "summary": f"Install the application {definition['name']} version {definition['version']}"}


def perform_install(store: AppStore, inputs: Mapping[str, Any], footer: str, rid: str, ctx: Any):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    from agentic_os.governed_writes.apps import NotApplied  # noqa: PLC0415
    definition = inputs["definition"]
    approved = ctx.evidence or {}
    if digest_of(definition) != approved.get("definition_sha256"):
        raise NotApplied("the definition is not the one that was approved")
    try:
        if contracts_of(definition, store.catalog()) != approved.get("contracts"):
            raise NotApplied("a capability changed since the approval: propose the application again")
        store.install(ctx.tenant, definition, by=ctx.approver)
    except (DefinitionError, KeyError) as e:
        raise NotApplied(f"it was not installed: {e}") from None
    now = store.current(ctx.tenant, definition["id"])
    ok = bool(now and now["digest"] == approved["definition_sha256"] and now["version"] == definition["version"])
    return Outcome(True, ok, f"{definition['id']}@{definition['version']}", f"installed; digest {approved['definition_sha256'][:12]}")


__all__ = ["SCHEMA", "FIELD_TYPES", "DefinitionError", "AppStore", "validate", "digest_of", "grants_needed", "contracts_of",
           "contract_changes", "check_record", "prepare_install", "perform_install", "canonical"]
