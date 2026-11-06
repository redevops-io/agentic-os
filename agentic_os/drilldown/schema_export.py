"""JSON Schemas (draft 2020-12) of the drill-down responses. Strict on purpose: ``additionalProperties: false`` everywhere, so
a field the contract does not name (an "explanation", a guessed actor) fails validation instead of reaching a screen.

The checked-in files under ``schema/`` are generated from this module; a test fails on drift.
Regenerate:  python -m agentic_os.drilldown.schema_export
"""
from __future__ import annotations

import json
import os
from typing import Dict

from .models import (CATEGORIES, DURABILITY, EVIDENCE_INSPECT_SCHEMA_ID, EVIDENCE_KINDS, EVIDENCE_LIST_SCHEMA_ID, INTEGRITY_STATUSES, OUTCOMES,
                     TIMELINE_SCHEMA_ID, UNAVAILABLE_REASONS)

DRAFT = "https://json-schema.org/draft/2020-12/schema"
SHA = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_STR_OR_NULL = {"type": ["string", "null"]}


def _obj(props: dict, required=None) -> dict:
    return {"type": "object", "properties": props, "required": list(props if required is None else required), "additionalProperties": False}


def _defs() -> dict:
    return {
        "unavailable": _obj({"available": {"const": False}, "reason": {"enum": list(UNAVAILABLE_REASONS)}, "detail": {"type": "string"}}),
        "cite": _obj({"event_seq": {"type": "integer", "minimum": 0}, "event_sha256": SHA}),
        "actor": {"anyOf": [{"$ref": "#/$defs/unavailable"}, _obj({"available": {"const": True}, "value": {"type": "string"}})]},
        "provenance": _obj({"origin": {"const": "event_log"}, "sample_data": {"const": False}, "read_only": {"const": True},
                            "store": _obj({"backend": {"type": "string"}, "durability": {"enum": list(DURABILITY)}})}),
        "produced_by": {"anyOf": [{"$ref": "#/$defs/unavailable"}, _obj({"node_id": {"type": "string"}, "capability": _STR_OR_NULL})]},
    }


def timeline_schema() -> dict:
    defs = _defs()
    defs["entry"] = _obj({
        "seq": {"type": "integer", "minimum": 0}, "ts": {"type": "number"}, "type": {"type": "string"},
        "category": {"enum": list(CATEGORIES)}, "outcome": {"enum": list(OUTCOMES)},
        "node_id": _STR_OR_NULL, "capability": _STR_OR_NULL, "summary": {"type": "string"},
        "actor": {"$ref": "#/$defs/actor"},
        "payload": {"anyOf": [{"$ref": "#/$defs/unavailable"}, {"type": "object"}]},
        "cite": {"$ref": "#/$defs/cite"}})
    top = _obj({
        "schema": {"const": TIMELINE_SCHEMA_ID}, "mission_id": {"type": "string"}, "provenance": {"$ref": "#/$defs/provenance"},
        "head": _obj({"seq": {"type": "integer", "minimum": 0}, "event_count": {"type": "integer", "minimum": 0}, "lineage_sha256": SHA,
                      "mission_state": _STR_OR_NULL}),
        "page": _obj({"after_seq": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1}, "returned": {"type": "integer", "minimum": 0},
                      "next_after_seq": {"type": ["integer", "null"]}}),
        "entries": {"type": "array", "items": {"$ref": "#/$defs/entry"}},
        "lineage_check": {"anyOf": [_obj({"status": {"const": "not_requested"}}),
                                    _obj({"status": {"enum": ["match", "mismatch"]}, "upto_seq": {"type": "integer", "minimum": 0}})]},
    })
    return {"$schema": DRAFT, "$id": "urn:redevops:schema:" + TIMELINE_SCHEMA_ID, "title": "Mission timeline page", **top, "$defs": defs}


def evidence_list_schema() -> dict:
    defs = _defs()
    defs["item"] = _obj({"id": {"type": "string"}, "kind": {"enum": list(EVIDENCE_KINDS)}, "capability": _STR_OR_NULL, "node_id": _STR_OR_NULL,
                         "summary": {"type": "string"}, "cite": {"$ref": "#/$defs/cite"}})
    top = _obj({
        "schema": {"const": EVIDENCE_LIST_SCHEMA_ID}, "mission_id": {"type": "string"}, "provenance": {"$ref": "#/$defs/provenance"},
        "items": {"type": "array", "items": {"$ref": "#/$defs/item"}},
        "coverage": _obj({"claims": {"type": "integer", "minimum": 0}, "evidence_records": {"type": "integer", "minimum": 0},
                          "verifications": {"type": "integer", "minimum": 0}, "node_results": {"type": "integer", "minimum": 0},
                          "mission_evidence_refs": {"type": "integer", "minimum": 0}, "lineage_complete": {"type": "boolean"},
                          "unavailable": {"type": "array", "items": _obj({"what": {"type": "string"}, "reason": {"enum": list(UNAVAILABLE_REASONS)},
                                                                         "detail": {"type": "string"}})}}),
    })
    return {"$schema": DRAFT, "$id": "urn:redevops:schema:" + EVIDENCE_LIST_SCHEMA_ID, "title": "Mission evidence list", **top, "$defs": defs}


def evidence_inspect_schema() -> dict:
    defs = _defs()
    top = _obj({
        "schema": {"const": EVIDENCE_INSPECT_SCHEMA_ID}, "mission_id": {"type": "string"}, "id": {"type": "string"},
        "kind": {"enum": list(EVIDENCE_KINDS)}, "reference": _STR_OR_NULL,
        "content": {"description": "any JSON value, or an unavailable object"},
        "content_sha256": {"anyOf": [SHA, {"type": "null"}]},
        "content_sha256_basis": {"enum": ["computed_at_read_time", None]},
        "integrity": _obj({"status": {"enum": list(INTEGRITY_STATUSES)}, "recorded_sha256": {"anyOf": [SHA, {"type": "null"}]},
                           "basis": {"type": "string"}, "computed_sha256": SHA}, required=["status", "recorded_sha256", "basis"]),
        "produced_by": {"$ref": "#/$defs/produced_by"}, "cite": {"$ref": "#/$defs/cite"}, "provenance": {"$ref": "#/$defs/provenance"},
    })
    return {"$schema": DRAFT, "$id": "urn:redevops:schema:" + EVIDENCE_INSPECT_SCHEMA_ID, "title": "Evidence item", **top, "$defs": defs}


def generated() -> Dict[str, dict]:
    return {"timeline.v1.schema.json": timeline_schema(), "evidence-list.v1.schema.json": evidence_list_schema(),
            "evidence-inspect.v1.schema.json": evidence_inspect_schema()}


def write(directory: str | None = None) -> None:
    d = directory or os.path.join(os.path.dirname(__file__), "schema")
    os.makedirs(d, exist_ok=True)
    for name, sch in generated().items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            json.dump(sch, fh, indent=2, sort_keys=True)
            fh.write("\n")


if __name__ == "__main__":
    write()
