"""Governed write path: propose (no mutation) -> approved apply (NEW version, read-back) -> publish."""
from __future__ import annotations

import hashlib
import os

import pytest

APPROVAL = {"approved": True, "by": "alice"}
OPS = [{"type": "replace_text", "search": "100 EUR", "replace": "120 EUR"}]


def tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".docedit" not in p.parts:
            h.update(str(p.relative_to(root)).replace(os.sep, "/").encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture()
def doc(env, kit):
    env.put("acme", "contracts/c.docx", kit.make_docx(["Agreement", "Price is 100 EUR.", "End."]), label="INTERNAL")
    return "contracts/c.docx"


def propose(env, doc_id, ops=OPS, **kw):
    return env.svc.propose_patch("acme", doc_id, ops, actor="agent", **kw)


def apply(env, p, idem="idem-key-0001", approval=APPROVAL, **over):
    args = dict(proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                patch_digest=p["patch_digest"], expected_version=p["base_version"], idempotency_key=idem,
                approval=approval, actor="alice")
    args.update(over)
    return env.svc.apply_patch("acme", **args)


# ── propose: no mutation ───────────────────────────────────────────────────────────────────────────────────
def test_propose_mutates_nothing_and_calls_no_backend(env, doc):
    before = tree_hash(env.root)
    env.store.head("acme", doc)
    vers = env.store.versions("acme", doc)
    p = propose(env, doc)
    assert p["status"] == "ok" and p["valid"] and p["executed"] is False
    assert tree_hash(env.root) == before and env.store.versions("acme", doc) == vers
    assert env.backend.calls == []
    assert "-Price is 100 EUR." in p["preview"] and "+Price is 120 EUR." in p["preview"]
    assert p["base_version"] == 1 and p["base_hash"] == vers[0].sha256 and p["backend"] == "onlyoffice"
    assert len(p["patch_digest"]) == 64 and len(p["proposal_id"]) == 32


def test_proposal_is_deterministic_and_bound(env, doc):
    a, b = propose(env, doc), propose(env, doc)
    assert a["proposal_id"] == b["proposal_id"]
    c = propose(env, doc, [{"type": "replace_text", "search": "100 EUR", "replace": "130 EUR"}])
    assert c["proposal_id"] != a["proposal_id"] and c["patch_digest"] != a["patch_digest"]


def test_propose_validation_flags_a_noop_patch(env, doc):
    p = propose(env, doc, [{"type": "replace_text", "search": "not in the document", "replace": "x"}])
    assert p["status"] == "invalid" and p["valid"] is False and p["validation"][0]["problem"]
    with pytest.raises(env.mods.errors.InvalidInput, match="validation"):
        apply(env, p)


@pytest.mark.parametrize("ops", [
    [], None, "x", [{"type": "exec", "code": "1"}], [{"type": "replace_text", "search": "", "replace": "x"}],
    [{"type": "replace_text", "search": "a", "replace": "b", "script": "x"}], [{"type": "set_cell", "cell": "A1", "value": 1}],
    [{"type": "append_paragraph", "text": "a\x00b"}], [{"type": "replace_text", "search": "a" * 501, "replace": "b"}],
    [{"type": "append_paragraph"}], ["replace_text"], [{"type": "replace_text", "search": "a", "replace": 5}],
])
def test_malformed_or_hostile_ops_are_rejected(env, doc, ops):
    with pytest.raises(env.mods.errors.InvalidInput):
        propose(env, doc, ops)


def test_formula_injection_in_cells_is_refused(env, kit):
    env.put("acme", "s.xlsx", kit.make_xlsx({"A1": 1}))
    for v in ("=HYPERLINK(\"http://evil\")", "+cmd", "-1+1", "@SUM(A1)"):
        with pytest.raises(env.mods.errors.InvalidInput):
            env.svc.propose_patch("acme", "s.xlsx", [{"type": "set_cell", "cell": "A1", "value": v}])


def test_propose_fails_closed_when_the_backend_cannot_edit(env, doc, fake_backend_cls, mods):
    env.svc.backend = fake_backend_cls(mods.backend.BackendDecl.parse(
        {"provider": "onlyoffice", "capabilities": {"convert": True}}))
    with pytest.raises(env.mods.errors.UnsupportedCapability):
        propose(env, doc)
    p_tracked = None
    env.svc.backend = fake_backend_cls(mods.backend.BackendDecl.parse(
        {"provider": "onlyoffice", "capabilities": {"automated_edit": True}}))
    with pytest.raises(env.mods.errors.UnsupportedCapability, match="tracked_changes"):
        propose(env, doc, track_changes=True)
    assert p_tracked is None


def test_propose_stale_base_version_is_rejected(env, doc):
    with pytest.raises(env.mods.errors.StaleVersion):
        propose(env, doc, base_version=7)


# ── apply ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_apply_writes_a_new_version_and_verifies_by_read_back(env, doc):
    p = propose(env, doc)
    original = (env.root / "acme" / doc).read_bytes()
    r = apply(env, p)
    assert r["status"] == "ok" and r["executed"] and r["verified"] and not r["replayed"]
    assert r["source_version"] == 1 and r["new_version"] == 2 and r["base_hash"] == p["base_hash"]
    data, info = env.store.read("acme", doc, 2)
    assert hashlib.sha256(data).hexdigest() == r["result_sha256"] == info.sha256
    assert (env.root / "acme" / doc).read_bytes() == original            # NEVER overwritten in place
    assert env.store.read("acme", doc, 1)[0] == original
    assert env.mods.ooxml.docx_paragraphs(data) == ["Agreement", "Price is 120 EUR.", "End."]
    assert env.backend.calls.count("apply_edit") == 1


def test_receipt_carries_source_version_approval_hash_backend_actor_tenant(env, doc):
    p = propose(env, doc)
    r = apply(env, p, approval={"approved": True, "by": "alice"})
    log = env.store.receipt_log("acme")
    assert log.verify_chain()
    rec = [x for x in log.records() if x["kind"] == "apply_committed"][0]
    assert rec["source_version"] == 1 and rec["base_hash"] == p["base_hash"] and rec["new_version"] == 2
    assert rec["result_hash"] == r["result_sha256"] and rec["backend"] == "onlyoffice" and rec["tenant"] == "acme"
    assert rec["actor"] == "alice" and rec["approval_ref"]["marker"]["by"] == "alice"
    assert rec["patch_digest"] == p["patch_digest"] and rec["proposal_id"] == p["proposal_id"]
    kinds = [x["kind"] for x in log.records()]
    assert kinds.index("apply_started") < kinds.index("apply_committed") and "transmit_decision" in kinds
    assert env.store.versions("acme", doc)[-1].receipt_id == [x for x in log.records() if x["kind"] == "apply_started"][0]["receipt_id"]


def test_stale_version_fails_closed_before_any_backend_call(env, doc, kit):
    p = propose(env, doc)
    (env.root / "acme" / doc).write_bytes(kit.make_docx(["Someone else edited this", "Price is 100 EUR."]))
    before = tree_hash(env.root)
    with pytest.raises(env.mods.errors.StaleVersion) as ei:
        apply(env, p)
    assert ei.value.extra["current"] == 2 and ei.value.to_result()["status"] == "conflict"
    assert env.backend.calls == [] and tree_hash(env.root) == before
    assert env.store.head("acme", doc).version == 2                       # nothing was written on top


def test_stale_version_detected_at_commit_keeps_the_draft(env, doc, kit):
    p = propose(env, doc)
    real = env.backend._do_apply_edit

    def racing(ref, ops, *, track_changes, idem_key):
        out = real(ref, ops, track_changes=track_changes, idem_key=idem_key)
        (env.root / "acme" / doc).write_bytes(kit.make_docx(["racing writer"]))      # lands while the editor works
        env.store.head("acme", doc)
        return out
    env.backend._do_apply_edit = racing
    with pytest.raises(env.mods.errors.StaleVersion) as ei:
        apply(env, p)
    d = ei.value.extra["draft"]
    assert d["doc_id"].startswith("drafts/") and env.store.read("acme", d["doc_id"])[1].sha256 == d["sha256"]
    assert ei.value.extra["completed_steps"] == ["started", "backend_edit", "verify"]
    assert env.store.head("acme", doc).kind == "external_edit"


@pytest.mark.parametrize("over", [{"patch_digest": "f" * 64}, {"base_hash": "e" * 64}, {"backend": "collabora"},
                                  {"expected_version": 9}, {"proposal_id": "0" * 32}])
def test_approval_binding_must_match_the_stored_proposal(env, doc, over):
    p = propose(env, doc)
    with pytest.raises(env.mods.errors.DocEditorError):
        apply(env, p, **over)
    assert env.backend.calls == []


def test_apply_requires_an_idempotency_key(env, doc):
    p = propose(env, doc)
    for k in ("", "short", "bad key with spaces", None):
        with pytest.raises(env.mods.errors.InvalidInput):
            apply(env, p, idem=k)
    assert env.backend.calls == []


def test_idempotent_replay_returns_the_same_result_without_a_second_write(env, doc):
    p = propose(env, doc)
    a = apply(env, p, idem="idem-replay-01")
    b = apply(env, p, idem="idem-replay-01")
    assert b["replayed"] is True and b["new_version"] == a["new_version"] and b["result_sha256"] == a["result_sha256"]
    assert env.backend.calls.count("apply_edit") == 1 and len(env.store.versions("acme", doc)) == 2


def test_two_different_keys_for_the_same_proposal_second_is_stale(env, doc):
    p = propose(env, doc)
    apply(env, p, idem="idem-first-001")
    with pytest.raises(env.mods.errors.StaleVersion):
        apply(env, p, idem="idem-second-01")
    assert env.backend.calls.count("apply_edit") == 1


def test_network_failure_after_send_is_not_retried_and_is_unverifiable(env, doc):
    p = propose(env, doc)
    env.backend.mode = "unknown"
    with pytest.raises(env.mods.errors.OutcomeUnknown) as ei:
        apply(env, p, idem="idem-netfail-01")
    res = ei.value.to_result()
    assert res["status"] == "unverifiable" and res["retried"] is False and res["executed"] is False
    assert env.backend.calls.count("apply_edit") == 1                    # exactly ONE attempt, no silent retry
    assert env.store.head("acme", doc).version == 1                       # nothing committed
    kinds = [r["kind"] for r in env.store.receipt_log("acme").records()]
    assert "apply_outcome_unknown" in kinds and "apply_committed" not in kinds


def test_retry_after_unknown_outcome_requires_readback_and_a_fresh_call(env, doc):
    p = propose(env, doc)
    env.backend.mode = "unknown"
    with pytest.raises(env.mods.errors.OutcomeUnknown):
        apply(env, p, idem="idem-netfail-02")
    n = env.backend.calls.count("apply_edit")
    # nothing happens on its own; an explicit new call reconciles against the store first, then writes once
    env.backend.mode = None
    r = apply(env, p, idem="idem-netfail-02")
    assert r["status"] == "ok" and env.backend.calls.count("apply_edit") == n + 1
    started = [x for x in env.store.receipt_log("acme").records() if x["kind"] == "apply_started"]
    assert [x["attempt"] for x in started] == [1, 2]


def test_retry_after_unknown_outcome_when_the_document_moved_is_stale(env, doc, kit):
    p = propose(env, doc)
    env.backend.mode = "unknown"
    with pytest.raises(env.mods.errors.OutcomeUnknown):
        apply(env, p, idem="idem-netfail-03")
    (env.root / "acme" / doc).write_bytes(kit.make_docx(["changed meanwhile"]))
    env.backend.mode = None
    with pytest.raises(env.mods.errors.StaleVersion):
        apply(env, p, idem="idem-netfail-03")


def test_crash_between_commit_and_receipt_is_recovered_without_a_second_write(env, doc):
    p = propose(env, doc)
    real = env.svc._finalize
    state = {"n": 0}

    def crash_once(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("process died after put_version")
        return real(*a, **k)
    env.svc._finalize = crash_once
    with pytest.raises(RuntimeError):
        apply(env, p, idem="idem-crash-001")
    assert env.store.head("acme", doc).version == 2 and env.backend.calls.count("apply_edit") == 1
    r = apply(env, p, idem="idem-crash-001")                                # adopts the already-written version
    assert r["status"] == "ok" and r["new_version"] == 2 and env.backend.calls.count("apply_edit") == 1


def test_backend_unavailable_before_send_is_a_clean_typed_failure(env, doc):
    p = propose(env, doc)
    env.backend.mode = "unavailable"
    with pytest.raises(env.mods.errors.BackendUnavailable) as ei:
        apply(env, p, idem="idem-down-0001")
    assert ei.value.to_result()["status"] == "unavailable"
    assert env.store.head("acme", doc).version == 1


def test_editor_output_that_differs_from_the_simulation_is_rejected_and_preserved(env, doc):
    p = propose(env, doc)
    env.backend.mode = "wrong_text"
    with pytest.raises(env.mods.errors.VerificationFailed) as ei:
        apply(env, p, idem="idem-wrong-001")
    x = ei.value
    assert x.extra["completed_steps"] == ["started", "backend_edit"] and "commit" in x.extra["pending_steps"]
    assert env.store.head("acme", doc).version == 1                       # nothing committed
    d = x.extra["draft"]
    assert env.store.read("acme", d["doc_id"])[1].sha256 == d["sha256"]   # PRESERVE_ONLY: the draft is kept
    assert x.to_result()["status"] == "unverifiable" and x.to_result()["executed"] is False


def test_tracked_changes_are_verified_to_contain_revisions(env, doc, kit):
    p = propose(env, doc, track_changes=True)
    r = apply(env, p, idem="idem-track-001")
    data = env.store.read("acme", doc, r["new_version"])[0]
    rev = env.mods.ooxml.docx_revisions(data)
    assert rev["insertions"] >= 1 and rev["deletions"] >= 1


def test_xlsx_cell_edit_roundtrip(env, kit):
    env.put("acme", "s.xlsx", kit.make_xlsx({"A1": 1, "B2": 42}), label="PUBLIC")
    p = env.svc.propose_patch("acme", "s.xlsx", [{"type": "set_cell", "cell": "B2", "value": 99}])
    assert p["valid"] and "B2" in p["preview"]
    ops_out = kit.make_xlsx({"A1": 1, "B2": 99})
    env.backend._do_apply_edit = lambda ref, ops, **k: env.mods.backend.BackendFile(
        content=ops_out, filename="result.xlsx", sha256=hashlib.sha256(ops_out).hexdigest())
    r = apply(env, p, idem="idem-xlsx-0001")
    assert r["status"] == "ok" and env.mods.ooxml.xlsx_cell(env.store.read("acme", "s.xlsx", 2)[0], "B2") == "99"


def test_xlsx_wrong_output_is_rejected(env, kit):
    env.put("acme", "s.xlsx", kit.make_xlsx({"B2": 42}), label="PUBLIC")
    p = env.svc.propose_patch("acme", "s.xlsx", [{"type": "set_cell", "cell": "B2", "value": 99}])
    bad = kit.make_xlsx({"B2": 41})
    env.backend._do_apply_edit = lambda ref, ops, **k: env.mods.backend.BackendFile(
        content=bad, filename="result.xlsx", sha256=hashlib.sha256(bad).hexdigest())
    with pytest.raises(env.mods.errors.VerificationFailed):
        apply(env, p, idem="idem-xlsx-0002")


# ── classification applies to the write path ────────────────────────────────────────────────────────────────
def test_restricted_document_is_never_sent_even_with_approval(env, kit):
    env.put("acme", "r.docx", kit.make_docx(["Price is 100 EUR."]), label="RESTRICTED")
    p = propose(env, "r.docx")
    with pytest.raises(env.mods.errors.PolicyDenied, match="never sent"):
        apply(env, p, idem="idem-restr-001")
    assert env.backend.calls == []


def test_unlabeled_document_is_treated_as_confidential_and_needs_the_bound_approval(env, kit):
    env.put("acme", "u.docx", kit.make_docx(["Price is 100 EUR."]))
    p = propose(env, "u.docx")
    assert p["would_transmit"]["effective_classification"] == "CONFIDENTIAL" and p["would_transmit"]["needs_approval"]
    r = apply(env, p, idem="idem-unlab-001")             # the Mission approval IS the bound per-document approval
    assert r["status"] == "ok"


def test_inspect_unlabeled_without_transmit_approval_is_denied_and_local_only_works(env, kit):
    env.put("acme", "u.docx", kit.make_docx(["x"]))
    with pytest.raises(env.mods.errors.PolicyDenied):
        env.svc.inspect("acme", "u.docx")
    assert env.backend.calls == []
    assert env.svc.inspect("acme", "u.docx", local_only=True)["transmitted"] is False


def test_every_transmit_decision_is_receipted_including_denials(env, kit):
    env.put("acme", "u.docx", kit.make_docx(["x"]))
    with pytest.raises(env.mods.errors.PolicyDenied):
        env.svc.convert("acme", "u.docx", "pdf")
    recs = [r for r in env.store.receipt_log("acme").records() if r["kind"] == "transmit_decision"]
    assert recs and recs[-1]["allowed"] is False and recs[-1]["effective_label"] == "CONFIDENTIAL"


# ── convert / export ────────────────────────────────────────────────────────────────────────────────────────
def test_convert_stores_a_validated_versioned_artifact_with_inherited_label(env, kit):
    env.put("acme", "p.docx", kit.make_docx(["x"]), label="PUBLIC")
    r = env.svc.convert("acme", "p.docx", "pdf")
    assert r["verified"] and r["artifact_sha256"] == hashlib.sha256(kit.PDF).hexdigest() and r["size"] == len(kit.PDF)
    data, info = env.store.read("acme", r["artifact_doc_id"])
    assert data == kit.PDF and env.store.get_classification("acme", r["artifact_doc_id"]) == "PUBLIC"
    assert r["artifact_doc_id"].startswith("converted/")
    assert env.svc.convert("acme", "p.docx", "pdf")["artifact_version"] == r["artifact_version"]    # identical => same version


def test_convert_rejects_invalid_output_before_declaring_success(env, kit):
    env.put("acme", "p.docx", kit.make_docx(["x"]), label="PUBLIC")
    env.backend.mode = "corrupt"
    with pytest.raises(env.mods.errors.InvalidInput):
        env.svc.convert("acme", "p.docx", "pdf")
    assert env.store.receipt_log("acme") and not [
        r for r in env.store.receipt_log("acme").records() if r["kind"] == "convert"]


def test_export_goes_to_its_own_namespace(env, kit):
    env.put("acme", "p.docx", kit.make_docx(["x"]), label="PUBLIC")
    assert env.svc.export("acme", "p.docx", "pdf")["artifact_doc_id"].startswith("exports/")


def test_confidential_convert_with_a_bound_transmit_approval(env, kit):
    env.put("acme", "c.docx", kit.make_docx(["x"]), label="CONFIDENTIAL")
    head = env.store.head("acme", "c.docx")
    tok = kit.mint("document-editor", "document.transmit", {"tenant": "acme", "doc_id": "c.docx",
                                                              "doc_sha256": head.sha256, "backend": "onlyoffice"},
                   mission_id=None, node_id=None)
    assert env.svc.convert("acme", "c.docx", "pdf", transmit_approval=tok)["verified"]
    with pytest.raises(env.mods.errors.PolicyDenied):                    # single use
        env.svc.convert("acme", "c.docx", "pdf", transmit_approval=tok)


# ── publish ────────────────────────────────────────────────────────────────────────────────────────────────
def pub(env, doc, version=1, dest="board-share", approval=APPROVAL, sha=None):
    info = env.store.read("acme", doc, version)[1]
    return env.svc.publish("acme", doc_id=doc, version=version, sha256=sha or info.sha256, destination=dest,
                           approval=approval, actor="alice")


def test_publish_copies_the_exact_version_to_an_allowlisted_destination_with_its_own_receipt(env, doc):
    r = pub(env, doc)
    assert r["published"] and r["verified"] and r["irreversible"] is True
    out = env.pub / "acme" / "c.v1.docx"
    assert out.read_bytes() == (env.root / "acme" / doc).read_bytes()
    rec = [x for x in env.store.receipt_log("acme").records() if x["kind"] == "publish"][0]
    assert rec["destination"] == "board-share" and rec["irreversible"] is True and rec["approval_ref"]["marker"]["by"] == "alice"
    assert pub(env, doc)["receipt_id"] != r["receipt_id"]                # idempotent copy, new receipt


def test_publish_never_overwrites_a_different_file(env, doc):
    pub(env, doc)
    (env.pub / "acme" / "c.v1.docx").write_bytes(b"someone replaced it")
    with pytest.raises(env.mods.errors.Conflict, match="never overwrites"):
        pub(env, doc)


def test_publish_to_a_non_allowlisted_destination_is_denied(env, doc):
    with pytest.raises(env.mods.errors.PolicyDenied, match="allowlist"):
        pub(env, doc, dest="somewhere-else")
    with pytest.raises(env.mods.errors.PolicyDenied):
        pub(env, doc, dest="../../etc")


def test_publish_hash_mismatch_is_refused(env, doc):
    with pytest.raises(env.mods.errors.Conflict):
        pub(env, doc, sha="0" * 64)


def test_publish_respects_the_classification_matrix(env, kit):
    env.put("acme", "i.docx", kit.make_docx(["internal"]), label="INTERNAL")
    env.put("acme", "pb.docx", kit.make_docx(["public"]), label="PUBLIC")
    env.put("acme", "r.docx", kit.make_docx(["restricted"]), label="RESTRICTED")
    env.put("acme", "c.docx", kit.make_docx(["confidential"]), label="CONFIDENTIAL")
    assert pub(env, "pb.docx", dest="public-site")["published"]                    # PUBLIC may go anywhere allowlisted
    with pytest.raises(env.mods.errors.PolicyDenied):
        pub(env, "i.docx", dest="public-site")                                      # INTERNAL not to EXTERNAL
    assert pub(env, "i.docx", dest="board-share")["published"]
    with pytest.raises(env.mods.errors.PolicyDenied):
        pub(env, "r.docx", dest="board-share")                                      # RESTRICTED never
    with pytest.raises(env.mods.errors.PolicyDenied):
        pub(env, "c.docx", dest="public-site")                                      # CONFIDENTIAL not to EXTERNAL
    assert pub(env, "c.docx", dest="board-share")["published"]                      # with the Mission approval


# ── tenant isolation ─────────────────────────────────────────────────────────────────────────────────────
def test_proposals_are_tenant_scoped(env, doc):
    p = propose(env, doc)
    with pytest.raises(env.mods.errors.NotFound):
        env.svc.apply_patch("globex", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1, idempotency_key="idem-xtenant-1",
                            approval=APPROVAL)
    with pytest.raises(env.mods.errors.NotFound):
        env.svc.propose_patch("globex", doc, OPS)
    with pytest.raises(env.mods.errors.TenantMismatch):
        env.svc.propose_patch("nobody", doc, OPS)
    assert env.backend.calls == []
