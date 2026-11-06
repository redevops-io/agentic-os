"""Drill-down contract (Timeline + Evidence Inspect) over the REAL event log of a mission run by the real runtime.

What these tests pin down:
  * every shown item cites the persisted event it came from (seq + sha256), and the cite is reproducible by an oracle
    written independently of the code under test;
  * nothing is fabricated: a field the log does not hold is an explicit ``unavailable`` object with a reason, never a
    guess, and the evidence coverage says out loud what the runtime does not record;
  * the read path performs NO side effect: it takes a store and nothing else (no executor, no runtime), cannot append,
    and leaves the log byte-identical;
  * the output validates against the published JSON Schemas.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import pytest

from agentic_os.drilldown import (
    EVIDENCE_INSPECT_SCHEMA_ID, EVIDENCE_LIST_SCHEMA_ID, TIMELINE_SCHEMA_ID, UNAVAILABLE_REASONS, DrilldownRefused,
    evidence_inspect_schema, evidence_list_schema, timeline_schema)
from agentic_os.mission.store import Event, EventStore
from tests.drilldown_support import (
    CHATWOOT_SENTINEL, SCOPE, TWENTY_SENTINEL, completed_mission, failed_mission, gated_mission, make_service, oracle_event_sha256,
    owner, rewrite_jsonl_line, set_env)

jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture()
def env(monkeypatch):
    set_env(monkeypatch)


def _timeline_all(svc, mid, limit=500):
    entries, after = [], 0
    while True:
        page = svc.timeline(owner(), mid, after_seq=after, limit=limit)
        entries += page["entries"]
        if page["page"]["next_after_seq"] is None:
            return entries, page
        after = page["page"]["next_after_seq"]


# ───────────────────────── cites are real and reproducible ─────────────────────────
def test_every_timeline_entry_cites_a_persisted_event_with_a_reproducible_hash(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    rt, mid = completed_mission(path)
    store = EventStore(path=path)                              # a fresh store: the data comes from disk, not from the runtime
    svc, _, _ = make_service(store, tmp_path)
    entries, _ = _timeline_all(svc, mid)
    log = store.for_mission(mid)
    assert [e["seq"] for e in entries] == [e.seq for e in log] and len(entries) > 8
    by_seq = {e.seq: e for e in log}
    for ent in entries:
        raw = by_seq[ent["seq"]]
        assert ent["type"] == raw.type
        assert ent["cite"] == {"event_seq": raw.seq, "event_sha256": oracle_event_sha256(raw)}


def test_node_and_capability_fields_come_only_from_the_event_payload(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    svc, _, _ = make_service(store, tmp_path)
    entries, _ = _timeline_all(svc, mid)
    by_seq = {e.seq: e for e in store.for_mission(mid)}
    shown = [e for e in entries if e["capability"]]
    assert {e["capability"] for e in shown} >= {"crm.read_account", "support.contact.upsert"}
    for ent in entries:
        p = by_seq[ent["seq"]].payload
        assert ent["capability"] == p.get("capability")
        assert ent["node_id"] == p.get("node_id")


def test_summary_is_a_fixed_template_over_recorded_fields_and_never_names_what_the_log_lacks(tmp_path):
    store = EventStore()
    store.append("MissionCreated", "m1", {"goal": "g"})
    store.append("SomethingFromTheFuture", "m1", {"x": 1})        # an event type this build has never heard of
    store.append("NodeSucceeded", "m1", {"node_id": "n1"})        # a success with no capability recorded
    svc, _, _ = make_service(store, __import__("pathlib").Path(__import__("tempfile").mkdtemp()),
                             scope_of=lambda m: SCOPE)
    ents = svc.timeline(owner(), "m1", limit=10)["entries"]
    assert ents[1]["category"] == "other" and ents[1]["summary"] == "SomethingFromTheFuture"
    assert "None" not in ents[2]["summary"] and ents[2]["capability"] is None


def test_actor_is_unavailable_not_recorded_unless_the_payload_names_one(tmp_path):
    store = EventStore()
    store.append("MissionCreated", "m1", {"goal": "g"})
    store.append("ReplayAuthorized", "m1", {"actor": "alice", "scope": "customer-ops", "action": "resume"})
    store.append("ApprovalGranted", "m1", {"node_id": "n", "capability": "c", "approver": "bob"})
    store.append("NodeSucceeded", "m1", {"node_id": "n", "capability": "c", "result": {}})
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    a = [e["actor"] for e in svc.timeline(owner(), "m1")["entries"]]
    assert a[0] == {"available": False, "reason": "not_recorded", "detail": "the event records no actor, approver or principal"}
    assert a[1] == {"available": True, "value": "alice"}
    assert a[2] == {"available": True, "value": "bob"}
    assert a[3]["available"] is False and a[3]["reason"] == "not_recorded"


# ───────────────────────── lineage, tampering ─────────────────────────
def test_head_lineage_matches_the_d1_lineage_digest_and_changes_when_a_payload_is_edited(tmp_path, env):
    from agentic_os.evidence_query.replay import lineage_digest
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    svc, _, _ = make_service(store, tmp_path)
    before = svc.timeline(owner(), mid, limit=500)
    assert before["head"]["lineage_sha256"] == lineage_digest(store.for_mission(mid))
    target = next(e for e in before["entries"] if e["type"] == "NodeSucceeded")
    rewrite_jsonl_line(path, target["seq"], lambda d: d["payload"].__setitem__("result", {"native_id": "forged"}))
    after = make_service(EventStore(path=path), tmp_path)[0].timeline(owner(), mid, limit=500)
    assert after["head"]["lineage_sha256"] != before["head"]["lineage_sha256"]
    changed = [(a, b) for a, b in zip(before["entries"], after["entries"]) if a["cite"] != b["cite"]]
    assert [a["seq"] for a, _ in changed] == [target["seq"]]    # exactly the edited event stops matching its earlier cite


def test_expect_lineage_reports_match_and_mismatch_and_never_hides_a_mismatch(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    svc, _, _ = make_service(store, tmp_path)
    head = svc.timeline(owner(), mid, limit=500)["head"]
    ok = svc.timeline(owner(), mid, limit=1, expect_lineage=head["lineage_sha256"], upto=head["seq"])
    assert ok["lineage_check"] == {"status": "match", "upto_seq": head["seq"]}
    bad = svc.timeline(owner(), mid, limit=1, expect_lineage="sha256:" + "0" * 64, upto=head["seq"])
    assert bad["lineage_check"]["status"] == "mismatch"
    none = svc.timeline(owner(), mid, limit=1)
    assert none["lineage_check"] == {"status": "not_requested"}


# ───────────────────────── explicit unavailability, redaction ─────────────────────────
def test_unavailable_has_a_closed_set_of_reasons():
    assert set(UNAVAILABLE_REASONS) == {"not_recorded", "not_authorized", "redacted", "expired", "source_offline",
                                        "too_large", "integrity_failed"}


def test_secret_looking_keys_are_redacted_with_an_explicit_marker(tmp_path):
    store = EventStore()
    store.append("MissionCreated", "m1", {"goal": "g"})
    store.append("NodeSucceeded", "m1", {"node_id": "n", "capability": "c",
                                         "result": {"ok": 1, "api_key": "SK-SENTINEL-1", "nested": {"Authorization": "Bearer SK-SENTINEL-2"}}})
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    out = svc.timeline(owner(), "m1")
    blob = json.dumps(out)
    assert "SK-SENTINEL" not in blob
    res = out["entries"][1]["payload"]["result"]
    assert res["ok"] == 1 and res["api_key"]["reason"] == "redacted" and res["nested"]["Authorization"]["reason"] == "redacted"


def test_an_oversize_payload_is_withheld_with_a_reason_but_its_cite_is_still_shown(tmp_path):
    store = EventStore()
    store.append("MissionCreated", "m1", {"goal": "g"})
    ev = store.append("NodeSucceeded", "m1", {"node_id": "n", "capability": "c", "result": {"blob": "x" * 20000}})
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    ent = svc.timeline(owner(), "m1")["entries"][1]
    assert ent["payload"]["available"] is False and ent["payload"]["reason"] == "too_large"
    assert ent["cite"]["event_sha256"] == oracle_event_sha256(ev)


def test_no_secret_value_reaches_the_timeline_or_the_evidence_surface(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    surface = json.dumps([svc.timeline(owner(), mid, limit=500), svc.evidence_list(owner(), mid)], default=str)
    assert TWENTY_SENTINEL not in surface and CHATWOOT_SENTINEL not in surface


# ───────────────────────── pagination ─────────────────────────
def test_pages_concatenate_to_the_full_log_and_next_cursor_ends(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    full = svc.timeline(owner(), mid, limit=500)
    small, _ = _timeline_all(svc, mid, limit=3)
    assert [e["seq"] for e in small] == [e["seq"] for e in full["entries"]]
    assert full["page"]["next_after_seq"] is None and full["page"]["returned"] == len(full["entries"])


@pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 501}, {"limit": -1}, {"after_seq": -1}, {"limit": "x"}, {"after_seq": 1.5}])
def test_bad_pagination_parameters_are_a_bad_request(tmp_path, env, kw):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    with pytest.raises(DrilldownRefused) as ei:
        svc.timeline(owner(), mid, **kw)
    assert ei.value.code == "bad_request" and ei.value.status == 400


def test_a_large_mission_is_paged_not_dumped(tmp_path):
    store = EventStore()
    store.append("MissionCreated", "big", {"goal": "g"})
    for i in range(2500):
        store.append("NodeSucceeded", "big", {"node_id": f"n{i}", "capability": "c", "result": {"i": i}})
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    first = svc.timeline(owner(), "big", limit=500)
    assert first["page"]["returned"] == 500 and first["page"]["next_after_seq"] == 500 and first["head"]["event_count"] == 2501
    entries, _ = _timeline_all(svc, "big", limit=500)
    assert len(entries) == 2501 and len({e["seq"] for e in entries}) == 2501


# ───────────────────────── evidence: what exists, what does not ─────────────────────────
def test_evidence_list_cites_verifications_and_node_results_and_says_what_is_not_recorded(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    svc, _, _ = make_service(store, tmp_path)
    out = svc.evidence_list(owner(), mid)
    kinds = {i["kind"] for i in out["items"]}
    assert {"verification", "node_result"} <= kinds
    cov = out["coverage"]
    assert cov["claims"] == 0 and cov["evidence_records"] == 0 and cov["verifications"] >= 1
    assert cov["lineage_complete"] is False
    gaps = {g["what"]: g for g in cov["unavailable"]}
    assert gaps["claim_to_evidence_lineage"]["reason"] == "not_recorded"
    assert gaps["mission_evidence_refs"]["reason"] == "not_recorded"       # a goal-created mission carries no sealed-intent evidence refs
    by_seq = {e.seq: e for e in store.for_mission(mid)}
    for it in out["items"]:
        assert it["cite"]["event_sha256"] == oracle_event_sha256(by_seq[it["cite"]["event_seq"]])


def test_inspecting_a_node_result_returns_the_recorded_result_with_realism_visible(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    item = next(i for i in svc.evidence_list(owner(), mid)["items"]
                if i["kind"] == "node_result" and i["capability"] == "support.contact.upsert")
    out = svc.evidence_inspect(owner(), mid, item["id"])
    assert out["content"]["native_id"] and out["content"]["realism"] == "SEEDED-DEMO" and out["content"]["connected"] is False
    assert out["produced_by"]["capability"] == "support.contact.upsert"
    assert out["integrity"]["status"] == "no_recorded_hash"            # the runtime records no hash of a result at write time
    assert out["content_sha256"].startswith("sha256:") and out["content_sha256_basis"] == "computed_at_read_time"


def test_inspecting_a_verification_returns_its_checks(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    item = next(i for i in svc.evidence_list(owner(), mid)["items"] if i["kind"] == "verification")
    out = svc.evidence_inspect(owner(), mid, item["id"])
    assert out["content"]["decision"] == "accept" and out["content"]["checks"]


def test_an_unknown_evidence_id_is_not_found(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    with pytest.raises(DrilldownRefused) as ei:
        svc.evidence_inspect(owner(), mid, "vr-does-not-exist")
    assert ei.value.status == 404


def _store_with_refs(refs):
    store = EventStore()
    store.append("MissionCreated", "m1", {"goal": "g", "evidence_refs": refs})
    return store


def test_mission_evidence_refs_without_a_resolver_are_source_offline_not_invented(tmp_path):
    store = _store_with_refs(["discovery:finding-918@v3"])
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    items = svc.evidence_list(owner(), "m1")["items"]
    ref = next(i for i in items if i["kind"] == "mission_evidence_ref")
    out = svc.evidence_inspect(owner(), "m1", ref["id"])
    assert out["content"] == {"available": False, "reason": "source_offline",
                              "detail": "no resolver is configured for this reference"}
    assert out["reference"] == "discovery:finding-918@v3"


def test_a_pinned_ref_is_verified_when_the_resolved_bytes_match_and_flagged_when_they_do_not(tmp_path):
    body = {"finding": 918, "text": "refund policy changed"}
    good = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ref = f"discovery:finding-918#{good}"
    store = _store_with_refs([ref])
    ok_svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE, resolver=lambda r: body)
    item = next(i for i in ok_svc.evidence_list(owner(), "m1")["items"] if i["kind"] == "mission_evidence_ref")
    ok = ok_svc.evidence_inspect(owner(), "m1", item["id"])
    assert ok["integrity"] == {"status": "recorded_hash_verified", "recorded_sha256": good, "basis": "pin in the recorded reference"}
    assert ok["content"] == body

    tampered = dict(body, text="refund policy UNCHANGED")
    bad_svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE, resolver=lambda r: tampered)
    bad = bad_svc.evidence_inspect(owner(), "m1", item["id"])
    assert bad["integrity"]["status"] == "recorded_hash_mismatch" and bad["integrity"]["recorded_sha256"] == good
    assert bad["content"]["available"] is False and bad["content"]["reason"] == "integrity_failed"
    assert "UNCHANGED" not in json.dumps(bad)


def test_a_resolver_that_raises_is_source_offline(tmp_path):
    def boom(_ref):
        raise ConnectionError("down")
    store = _store_with_refs(["discovery:finding-1"])
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE, resolver=boom)
    item = next(i for i in svc.evidence_list(owner(), "m1")["items"] if i["kind"] == "mission_evidence_ref")
    out = svc.evidence_inspect(owner(), "m1", item["id"])
    assert out["content"]["reason"] == "source_offline" and "down" not in json.dumps(out)


# ───────────────────────── partial failure and gates are shown, not smoothed over ─────────────────────────
def test_a_failed_step_is_shown_as_failed_with_the_recorded_error(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = failed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    ents, _ = _timeline_all(svc, mid)
    failed = [e for e in ents if e["type"] == "NodeFailed"]
    assert failed and failed[0]["outcome"] == "failed" and failed[0]["capability"] == "support.contact.upsert"
    assert any(e["type"] == "NodeSucceeded" and e["capability"] == "crm.read_account" for e in ents)   # the part that did happen
    assert not any(e["type"] == "NodeSucceeded" and e["capability"] == "support.contact.upsert" for e in ents)
    assert svc.timeline(owner(), mid, limit=500)["head"]["mission_state"] == "failed"


def test_a_mission_parked_at_an_approval_gate_is_shown_pending(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = gated_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    out = svc.timeline(owner(), mid, limit=500)
    parked = [e for e in out["entries"] if e["type"] == "NodeParked"]
    assert parked and parked[-1]["outcome"] == "pending" and parked[-1]["category"] == "approval"
    assert out["head"]["mission_state"] == "waiting_human"


# ───────────────────────── provenance, restart, durability ─────────────────────────
def test_provenance_marks_the_origin_as_the_event_log_and_never_sample(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    prov = svc.timeline(owner(), mid)["provenance"]
    assert prov == {"origin": "event_log", "sample_data": False, "read_only": True,
                    "store": {"backend": "EventStore", "durability": "durable"}}


def test_a_memory_only_store_is_labelled_volatile(tmp_path, env):
    store = EventStore()                                           # the default of the desktop sidecar and the OCI runtime
    store.append("MissionCreated", "m1", {"goal": "g"})
    svc, _, _ = make_service(store, tmp_path, scope_of=lambda m: SCOPE)
    assert svc.timeline(owner(), "m1")["provenance"]["store"] == {"backend": "EventStore", "durability": "volatile"}


def test_the_answer_after_a_restart_is_byte_identical(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    first = make_service(EventStore(path=path), tmp_path)[0]
    a = json.dumps([first.timeline(owner(), mid, limit=500), first.evidence_list(owner(), mid)], sort_keys=True)
    second = make_service(EventStore(path=path), tmp_path)[0]          # a fresh process-equivalent: nothing in memory
    b = json.dumps([second.timeline(owner(), mid, limit=500), second.evidence_list(owner(), mid)], sort_keys=True)
    assert a == b


def test_a_duckdb_ledger_gives_the_same_cites_as_the_jsonl_ledger(tmp_path, env):
    duckdb = pytest.importorskip("duckdb")
    from agentic_os.mission.event_backends import DuckDBEventStore
    db = DuckDBEventStore(str(tmp_path / "ledger.duckdb"))
    for i in range(3):
        db.append("NodeSucceeded", "m1", {"node_id": f"n{i}", "capability": "c", "result": {"i": i, "f": 1.5}})
    db.close()
    db2 = DuckDBEventStore(str(tmp_path / "ledger.duckdb"))
    svc, _, _ = make_service(db2, tmp_path, scope_of=lambda m: SCOPE)
    out = svc.timeline(owner(), "m1")
    by_seq = {e.seq: e for e in db2.for_mission("m1")}
    assert out["provenance"]["store"] == {"backend": "DuckDBEventStore", "durability": "durable"}
    for ent in out["entries"]:
        assert ent["cite"]["event_sha256"] == oracle_event_sha256(by_seq[ent["seq"]])
    db2.close()


# ───────────────────────── no side effects ─────────────────────────
def test_reading_appends_nothing_and_leaves_the_log_file_byte_identical(tmp_path, env, monkeypatch):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    store = EventStore(path=path)
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    n = len(store.all())

    def no_append(*a, **k):
        raise AssertionError("the read path appended to the event log")
    monkeypatch.setattr(store, "append", no_append)
    svc, _, _ = make_service(store, tmp_path)
    svc.timeline(owner(), mid, limit=500)
    svc.evidence_list(owner(), mid)
    for it in svc.evidence_list(owner(), mid)["items"]:
        svc.evidence_inspect(owner(), mid, it["id"])
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before and len(store.all()) == n


def test_the_service_cannot_reach_an_executor_or_a_runtime():
    import inspect
    import agentic_os.drilldown as pkg
    root = os.path.dirname(pkg.__file__)
    src = "\n".join(open(os.path.join(root, f)).read() for f in sorted(os.listdir(root)) if f.endswith(".py"))
    for forbidden in ("MissionRuntime", "rehydrate", "Executor", "OperatorClient", "grant_resume", ".run(", "re_evaluate", "subprocess", "urllib", "requests", "httpx"):
        assert forbidden not in src, f"drilldown must not reference {forbidden}"
    # the only .append( allowed is the audit sink's
    appends = re.findall(r"[\w.]*\.append\(", src)
    assert all(a.startswith("self.audit") or a in {"out.append(", "items.append(", "entries.append(", "unavailable.append(", "parts.append(", "keys.append(", "rows.append("} for a in appends), appends
    sig = inspect.signature(__import__("agentic_os.drilldown", fromlist=["DrilldownService"]).DrilldownService.__init__)
    assert "executor" not in sig.parameters and "runtime" not in sig.parameters


# ───────────────────────── schemas ─────────────────────────
def test_real_output_validates_against_both_schemas(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    jsonschema.validate(svc.timeline(owner(), mid, limit=500), timeline_schema())
    lst = svc.evidence_list(owner(), mid)
    jsonschema.validate(lst, evidence_list_schema())
    for it in lst["items"]:
        jsonschema.validate(svc.evidence_inspect(owner(), mid, it["id"]), evidence_inspect_schema())


def test_schemas_are_draft_2020_12_and_strict(tmp_path):
    for sch, sid in ((timeline_schema(), TIMELINE_SCHEMA_ID), (evidence_list_schema(), EVIDENCE_LIST_SCHEMA_ID),
                     (evidence_inspect_schema(), EVIDENCE_INSPECT_SCHEMA_ID)):
        assert sch["$schema"] == "https://json-schema.org/draft/2020-12/schema" and sch["$id"].endswith(sid)
        jsonschema.Draft202012Validator.check_schema(sch)
    t = timeline_schema()
    assert t["additionalProperties"] is False and t["$defs"]["entry"]["additionalProperties"] is False


def test_an_invented_field_or_reason_is_rejected_by_the_schema(tmp_path, env):
    path = str(tmp_path / "ev.jsonl")
    _, mid = completed_mission(path)
    svc, _, _ = make_service(EventStore(path=path), tmp_path)
    out = svc.timeline(owner(), mid, limit=500)
    bad = json.loads(json.dumps(out))
    bad["entries"][0]["explanation"] = "because the model said so"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, timeline_schema())
    bad = json.loads(json.dumps(out))
    bad["entries"][0]["actor"] = {"available": False, "reason": "unknown_but_probably_alice"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, timeline_schema())
    bad = json.loads(json.dumps(out))
    del bad["entries"][0]["cite"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, timeline_schema())


def test_checked_in_schema_files_equal_the_generated_ones():
    from agentic_os.drilldown import schema_export
    for name, gen in schema_export.generated().items():
        on_disk = json.loads(open(os.path.join(os.path.dirname(schema_export.__file__), "schema", name)).read())
        assert on_disk == gen, f"{name} drifted from the code: regenerate with python -m agentic_os.drilldown.schema_export"
