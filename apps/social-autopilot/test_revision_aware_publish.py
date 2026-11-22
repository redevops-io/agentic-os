"""social.publish bound to the revision of the draft a human approved (consumer #1 of agentic_os.mission.revisions).

Drives real MissionRuntime missions against a stateful fake Postiz: draft -> (revise) -> publish. Asserts on the Postiz writes, not on 'done'."""
from __future__ import annotations

import importlib
import json
import re

import pytest

from agentic_os.app_kit.decision_bridge import MissionRequest
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.revisions import RevisionLedger
from agentic_os.mission.runtime import ApprovalError, MissionRuntime
from agentic_os.mission.types import MissionState

core = importlib.import_module("social-autopilot.core")
t_op = importlib.import_module("social-autopilot.test_operator")
build_social_operator = importlib.import_module("social-autopilot.operator").build_social_operator


class _Postiz:
    """Stateful stand-in for the Post table. Everything not about a single post is delegated to the shared fake."""
    posts: dict = {}
    writes: list = []

    @classmethod
    def run(cls, args, text=None, capture_output=None, timeout=None, env=None):
        sql = args[-1]
        if 'INSERT INTO "Post"' in sql:
            pid = re.search(r"VALUES \('([^']+)'", sql).group(1)
            raw = re.search(r", '(\[.*\])', 0, '", sql).group(1).replace("''", "'")
            cls.posts[pid] = {"state": "DRAFT", "content": raw}
            cls.writes.append(("insert", pid))
            return t_op._Done("")
        m = re.search(r"WHERE p\.id = '([^']+)'", sql)
        if m and 'FROM "Post"' in sql:
            p = cls.posts.get(m.group(1))
            return t_op._Done(f"{m.group(1)}\x1f{p['state']}\x1fint-1\x1f{p['content']}\n" if p else "")
        if sql.startswith('UPDATE "Post" SET content'):
            pid = re.search(r"WHERE id = '([^']+)'", sql).group(1)
            cls.posts[pid]["content"] = re.search(r"SET content = '(.*)', \"updatedAt\"", sql).group(1).replace("''", "'")
            cls.writes.append(("revise", pid))
            return t_op._Done("")
        if sql.startswith('UPDATE "Post" SET state'):
            pid = re.search(r"WHERE id = '([^']+)'", sql).group(1)
            cls.posts[pid]["state"] = "QUEUE"
            cls.writes.append(("publish", pid))
            return t_op._Done("")
        return t_op._FakePsql.run(args, text=text, capture_output=capture_output, timeout=timeout, env=env)


@pytest.fixture(autouse=True)
def _postiz(monkeypatch):
    _Postiz.posts, _Postiz.writes = {}, []
    core._CACHE.update(ts=0.0, data=None)
    core._BINDINGS.clear()
    monkeypatch.setattr(core.subprocess, "run", _Postiz.run)
    monkeypatch.setattr(core, "PG_PASSWORD", "a-real-secret")
    monkeypatch.delenv("APP_DEMO_MODE", raising=False)


def _runtime(ledger="auto", aware=True):
    op = build_social_operator(revision_aware=aware)
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    led = RevisionLedger() if ledger == "auto" else ledger
    return MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})), revision_ledger=led), led


def _mission(rt, cap, inputs, gated=False):
    mid = MissionRuntimeLauncher(rt).launch(MissionRequest(goal=f"{cap}", capability=cap, required_capabilities=(cap,), inputs=inputs, gated=gated,
                                                           intervention_id="iv", opportunity_id="op", action_kind="x", subject="post"))
    rt.run(mid)
    return mid


def _draft(rt, topic="year-end checklist"):
    mid = _mission(rt, "social.draft", {"topic": topic})
    assert rt._missions[mid].state is MissionState.SUCCEEDED, rt._missions[mid].outcome
    return next(e for e in rt.store.for_mission(mid) if e.type == "NodeSucceeded").payload["result"]["post_id"]


def _events(rt, mid, typ):
    return [e for e in rt.store.for_mission(mid) if e.type == typ]


def _published():
    return [w for w in _Postiz.writes if w[0] == "publish"]


def test_draft_registers_revision_one_and_publish_binds_to_it():
    rt, led = _runtime()
    pid = _draft(rt)
    assert led.head(f"social/post/{pid}").rev == 1
    mid = _mission(rt, "social.publish", {"id": pid}, gated=True)
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN and not _published()
    b = _events(rt, mid, "NodeParked")[-1].payload["revision"]
    assert (b["artifact_key"], b["rev"], b["target"]) == (f"social/post/{pid}", 1, pid)
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve", actor="editor@example.com")
    assert _published() == [("publish", pid)]
    assert led.approval(b["approval_id"]).status == "CONSUMED"
    assert [h["type"] for h in led.history(f"social/post/{pid}")] == ["revision_created", "requested", "granted", "consumed"]


def test_editing_the_draft_supersedes_the_pending_publish_approval():
    rt, led = _runtime()
    pid = _draft(rt)
    pub = _mission(rt, "social.publish", {"id": pid}, gated=True)
    old = _events(rt, pub, "NodeParked")[-1].payload["revision"]["approval_id"]
    rev = _mission(rt, "social.revise_draft", {"id": pid, "copy": "A completely different post"})
    assert rt._missions[rev].state is MissionState.SUCCEEDED, rt._missions[rev].outcome
    assert led.head(f"social/post/{pid}").rev == 2 and led.approval(old).status == "SUPERSEDED"
    assert rt._missions[pub].state is MissionState.CANCELLED and not rt.repo.pending_human(pub)
    assert _events(rt, pub, "ApprovalSuperseded")[-1].payload["superseded_by_rev"] == 2
    assert not _published()                                                       # the stale draft was never published
    with pytest.raises(ApprovalError):
        rt.approve(pub, "stale-click", "approve", actor="editor@example.com")
    # a fresh request binds to revision 2 and publishes the EDITED text
    pub2 = _mission(rt, "social.publish", {"id": pid}, gated=True)
    assert _events(rt, pub2, "NodeParked")[-1].payload["revision"]["rev"] == 2
    rt.approve(pub2, rt.repo.pending_human(pub2)["node_id"], "approve", actor="editor@example.com")
    assert _published() == [("publish", pid)] and "completely different" in _Postiz.posts[pid]["content"]


def test_approval_given_after_an_out_of_band_revision_is_refused():
    rt, led = _runtime()
    pid = _draft(rt)
    pub = _mission(rt, "social.publish", {"id": pid}, gated=True)
    node = rt.repo.pending_human(pub)["node_id"]
    led.register_revision(f"social/post/{pid}", "f" * 64, actor="another-worker", tenant="default")     # revised by a process this runtime did not see
    with pytest.raises(ApprovalError, match="superseded"):
        rt.approve(pub, node, "approve", actor="editor@example.com")
    assert not _published()


def test_postiz_content_edited_outside_the_ledger_is_still_refused_by_the_content_hash_check():
    """Defense in depth: an edit made in the Postiz UI never created a ledger revision. The pre-existing content-hash check still stops it."""
    rt, led = _runtime()
    pid = _draft(rt)
    pub = _mission(rt, "social.publish", {"id": pid}, gated=True)
    _Postiz.posts[pid]["content"] = json.dumps([{"content": "edited in the Postiz UI"}])
    rt.approve(pub, rt.repo.pending_human(pub)["node_id"], "approve", actor="editor@example.com")
    assert not _published()
    assert rt._missions[pub].state is not MissionState.SUCCEEDED


def test_publish_cannot_be_approved_twice():
    rt, led = _runtime()
    pid = _draft(rt)
    pub = _mission(rt, "social.publish", {"id": pid}, gated=True)
    node = rt.repo.pending_human(pub)["node_id"]
    rt.approve(pub, node, "approve", actor="editor@example.com")
    with pytest.raises(ApprovalError):
        rt.approve(pub, node, "approve", actor="editor@example.com")
    assert len(_published()) == 1


def test_revision_aware_publish_without_a_ledger_fails_closed():
    rt, _ = _runtime(ledger=None)
    pid = _draft(rt)
    pub = _mission(rt, "social.publish", {"id": pid}, gated=True)
    assert rt._missions[pub].state is MissionState.FAILED and not _published()
    assert _events(rt, pub, "RevisionApprovalRefused")[-1].payload["code"] == "no_ledger"


def test_default_operator_is_unchanged_and_needs_no_ledger():
    """Opt-in: without AGENTIC_OS_REVISION_LEDGER / revision_aware=True nothing about the operator's capabilities or behaviour changes."""
    op = build_social_operator(revision_aware=False)
    caps = {c.name: c for c in op.manifest.capabilities}
    assert set(caps) == {"social.draft", "social.draft_visual", "social.publish"}
    assert all(c.revision_bound is None and c.revision_produces is None for c in caps.values())
    rt, _ = _runtime(ledger=None, aware=False)
    pid = _draft(rt)
    pub = _mission(rt, "social.publish", {"id": pid}, gated=True)
    rt.approve(pub, rt.repo.pending_human(pub)["node_id"], "approve", actor="editor@example.com")
    assert _published() == [("publish", pid)]


def test_aware_operator_declares_the_metadata_and_the_revise_producer():
    caps = {c.name: c for c in build_social_operator(revision_aware=True).manifest.capabilities}
    assert caps["social.publish"].revision_bound["digest"] == "head" and caps["social.publish"].approval_required
    assert caps["social.draft"].revision_produces["artifact"] == "social/post/{post_id}"
    assert "social.revise_draft" in caps and caps["social.revise_draft"].revision_produces


def test_revise_draft_only_touches_a_draft():
    rt, _ = _runtime()
    pid = _draft(rt)
    _Postiz.posts[pid]["state"] = "QUEUE"
    out = core.revise_draft({"id": pid, "copy": "too late"})
    assert out["status"] == "error" and "only a DRAFT" in out["error"] and not [w for w in _Postiz.writes if w[0] == "revise"]
    assert core.revise_draft({"id": "x'; DROP TABLE", "copy": "y"})["status"] == "error"          # id is validated before it reaches SQL
