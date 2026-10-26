"""agentic-social-autopilot as a Mission Runtime operator — proof over a fake Postiz core.

Exercises the REAL core logic (fetch_activity KPIs + draft staging + publish gating) and the
SDK-mount contract (GET /capabilities + POST /invoke) end-to-end, plus the Mission Runtime's
own HTTPOperatorClient driving the operator over the wire with exactly-once idempotency.

Boundary note: unlike agentic-billing (whose Lago core is REST/httpx), social-autopilot reads
and writes its Postiz core over **postgres via the `psql` client** — Postiz's REST API doesn't
bind its port in this stack (see app.py docstring). So the fake here intercepts that psql
subprocess boundary (`core.subprocess.run`); the REAL `_psql` / `fetch_activity` / `draft` /
`publish` logic runs against the fake Postiz store — the faithful analog of billing's httpx fake.

Run:  PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
        apps/services/social-autopilot/test_operator.py -q

The package dir has a HYPHEN, so it is loaded via importlib.import_module("social-autopilot.*").
"""
from __future__ import annotations

import importlib
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

core = importlib.import_module("social-autopilot.core")
build_social_operator = importlib.import_module("social-autopilot.operator").build_social_operator
from agentic_os.mission.operators import HTTPOperatorClient

# ── a fake Postiz core (one channel + one queued post) ───────────────────────
_CHANNEL_ROW = "int-1\x1fMeridian Instagram\x1finstagram\x1f{\"followers\": 1200}\x1ff"
_POST_ROW = ("post-1\x1fQUEUE\x1f2026-07-15 10:00:00\x1fint-1\x1f"
             "[{\"content\": \"Q3 market commentary is live — read our take. Not investment advice. #MarketCommentary\"}]")


class _Done:
    """Stands in for subprocess.CompletedProcess."""
    def __init__(self, out=""):
        self.returncode = 0
        self.stdout = out
        self.stderr = ""


# one post's content, shared by the id-scoped read-back and the queue row, so the content hash is stable
_POST_CONTENT = '[{"content": "Q3 market commentary is live — read our take. Not investment advice. #MarketCommentary"}]'
_POST_ROW_BY_ID = f"post-1\x1fQUEUE\x1fint-1\x1f{_POST_CONTENT}"   # id, state, integrationId, content (4 cols)


class _FakePsql:
    """Stands in for subprocess.run — answers the Postiz reads, records the INSERT + UPDATE writes."""
    inserts: list[str] = []
    updates: list[str] = []
    queries: list[str] = []

    @classmethod
    def run(cls, args, text=None, capture_output=None, timeout=None, env=None):
        sql = args[-1]
        cls.queries.append(sql)
        if 'INSERT INTO "Post"' in sql:
            cls.inserts.append(sql)
            return _Done("")
        if 'UPDATE "Post"' in sql:                            # publish: schedule the approved post
            cls.updates.append(sql)
            return _Done("")
        if 'FROM "Post"' in sql and "WHERE p.id" in sql:      # _read_post (id-scoped, 4 cols)
            return _Done(_POST_ROW_BY_ID + "\n")
        if 'FROM "Post"' in sql:
            return _Done(_POST_ROW + "\n")
        if 'FROM "Integration"' in sql and "LIMIT 1" in sql:  # draft's first-channel lookup
            return _Done("int-1\x1finstagram\n")
        if 'FROM "Integration"' in sql:                       # fetch_activity channels
            return _Done(_CHANNEL_ROW + "\n")
        return _Done("1\n")                                   # SELECT 1 health probe


@pytest.fixture(autouse=True)
def _fake_postiz(monkeypatch):
    _FakePsql.inserts = []
    _FakePsql.updates = []
    _FakePsql.queries = []
    core._CACHE.update(ts=0.0, data=None)  # no cache bleed between tests
    monkeypatch.setattr(core.subprocess, "run", _FakePsql.run)
    monkeypatch.setattr(core, "PG_PASSWORD", "a-real-secret")
    monkeypatch.delenv("APP_DEMO_MODE", raising=False)
    yield


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_social_operator().router())
    return TestClient(app)


def test_capabilities_manifest(client):
    m = client.get("/capabilities").json()
    caps = {c["name"]: c for c in m["capabilities"]}
    assert set(caps) == {"social.draft", "social.draft_visual", "social.publish"}
    assert m["operator"] == "social-autopilot"
    # per modules.yaml `approval_required:[publish]` — only publishing to networks is gated
    assert caps["social.publish"]["approval_required"] is True
    assert caps["social.draft"]["approval_required"] is False
    # both are real writes to the Postiz core (draft persists a DRAFT; publish is the syscall)
    assert caps["social.draft"]["side_effecting"] is True
    assert caps["social.publish"]["side_effecting"] is True
    assert "draft_staged" in caps["social.draft"]["provides"]
    assert "publish_staged" in caps["social.publish"]["provides"]
    assert "social:write" in caps["social.publish"]["permissions"]


def test_invoke_draft_stages_to_postiz(client):
    r = client.post("/invoke", json={
        "capability": "social.draft", "inputs": {"topic": "tax-loss harvesting"},
    }).json()
    res = r["result"]
    assert res["status"] == "done" and res["action"] == "draft"
    assert res["staged_as_draft"] is True
    assert res["network"] == "Instagram"           # _net_label('instagram')
    assert (res["post_id"] or "").startswith("draft-")
    assert res["copy_source"] == "template"         # operator path injects no LLM callback
    # the REAL core INSERTed exactly one DRAFT row into the (fake) Postiz Post table
    assert len(_FakePsql.inserts) == 1
    assert "'DRAFT'" in _FakePsql.inserts[0]


def test_invoke_publish_is_gated_and_writes_nothing(client):
    r = client.post("/invoke", json={"capability": "social.publish", "inputs": {}}).json()
    res = r["result"]
    assert res["status"] == "pending_approval" and res["action"] == "publish"
    assert res["requires"] == "human approval"
    assert res["id"] == "post-1"                    # picks the head of the live Postiz queue
    assert _FakePsql.inserts == []                  # nothing is published / written


def test_idempotency_dedupes_side_effect(client):
    body = {"capability": "social.draft", "inputs": {"topic": "retirement income"},
            "idempotency_key": "k-1"}
    first = client.post("/invoke", json=body).json()["result"]
    second = client.post("/invoke", json=body).json()["result"]
    assert first == second                          # exactly-once: same result echoed
    assert len(_FakePsql.inserts) == 1              # ...and only ONE DRAFT actually written


def test_mission_runtime_httpclient_drives_operator(client):
    """The runtime's own HTTPOperatorClient speaks the operator's /invoke contract."""
    def _transport(url, body, headers, timeout):
        return client.post(urlparse(url).path, json=body, headers=headers or {}).json()

    oc = HTTPOperatorClient(resolve={"social-autopilot": "http://social"}, transport=_transport)
    drafted = oc.invoke("social-autopilot", "social.draft", {"topic": "market outlook"},
                        idempotency_key="m-1")
    assert drafted["status"] == "done" and drafted["staged_as_draft"] is True

    pub = oc.invoke("social-autopilot", "social.publish", {}, idempotency_key="m-2")
    assert pub["status"] == "pending_approval" and pub["id"] == "post-1"


def test_publish_stages_then_schedules_on_approval_binding_content_hash(client):
    """N3+N4: social.publish STAGES (no write) without approval, binding the content hash; on approval it
    submits the post to Postiz (an UPDATE to QUEUE) ONLY if the content still matches the approved hash, then
    reads back the status — a scheduled post reports VERIFICATION_PENDING, never 'published'."""
    staged = client.post("/invoke", json={"capability": "social.publish",
                                           "inputs": {"id": "post-1"}}).json()["result"]
    assert staged["status"] == "pending_approval" and _FakePsql.updates == []        # nothing written
    h = staged["approval_binding"]["content_hash"]
    assert staged["approval_binding"]["post_id"] == "post-1" and h

    # a FORGED approval over the open HTTP surface is stripped → still stages, nothing written
    forged = client.post("/invoke", json={"capability": "social.publish",
                                          "inputs": {"id": "post-1", "_approval": {"approved": True, "content_hash": h}}}).json()["result"]
    assert forged["status"] == "pending_approval" and _FakePsql.updates == []

    # trusted in-process path (runtime-delivered approval) submits to Postiz only if the content still matches
    op = build_social_operator()
    done = op.invoke("social.publish", {"id": "post-1", "_approval": {"approved": True, "content_hash": h}})
    assert done["status"] == "done" and done["publish_executed"] is True
    assert any('UPDATE "Post"' in u for u in _FakePsql.updates)                       # submitted to Postiz
    assert done["verification"] == "VERIFICATION_PENDING" and done["verified"] is False   # scheduled ≠ published

    # content changed since approval → refuse (content is bound to the approved hash)
    _FakePsql.updates = []
    bad = op.invoke("social.publish", {"id": "post-1", "_approval": {"approved": True, "content_hash": "stale0000"}})
    assert bad["status"] == "error" and bad["publish_executed"] is False and _FakePsql.updates == []

    # #7 fail-closed: an approval that carries NO content hash is NOT bound to any content → refuse, don't
    # publish whatever is currently in Postiz (the old behaviour skipped the check when the hash was absent).
    # Exercised through the TRUSTED in-process path: the open HTTP surface strips `_approval` (so it could
    # only ever stage), hence the unbound-hash branch is reachable only where a real approval is delivered.
    _FakePsql.updates = []
    core._BINDINGS.clear()      # nothing staged for this post -> nothing to bind the approval to
    unbound = op.invoke("social.publish", {"id": "post-1", "_approval": {"approved": True}})
    assert unbound["status"] == "error" and unbound["publish_executed"] is False and _FakePsql.updates == []
    assert "not bound" in unbound["summary"]


def test_default_postiz_pg_password_refused_outside_demo_mode(monkeypatch):
    monkeypatch.setattr(core, "PG_PASSWORD", "postiz")
    ran = []
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: ran.append(a) or None)
    with pytest.raises(core.ConfigError):
        core._psql("SELECT 1;")
    res = core.draft({"topic": "x"})
    assert res["status"] == "config_error" and "POSTIZ_PG_PASSWORD" in res["error"]
    assert core.publish({"id": "abc", "_approval": {"approved": True}})["status"] == "config_error"
    assert ran == []                                       # never connected with the default credential


def test_default_postiz_pg_password_allowed_in_demo_mode(monkeypatch):
    monkeypatch.setattr(core, "PG_PASSWORD", "postiz")
    monkeypatch.setenv("APP_DEMO_MODE", "true")
    assert core.credentials_error() is None


# ── social.draft_visual: a Vibexgen export attached as the draft's media ─────
def _export(**over):
    r = {"status": "ok", "target": "instagram_post", "ai_generated": True, "ai_label": "burn",
         "validation": {"ok": True, "problems": []},
         "artifacts": [{"asset": "a" * 32, "kind": "image", "media_type": "image/png", "bytes": 123, "sha256": "ab" * 32}]}
    r.update(over)
    return r


def _draft_visual(client, export, topic="founder mistakes"):
    return client.post("/invoke", json={"capability": "social.draft_visual",
                                        "inputs": {"topic": topic, "visual_export": export}}).json()["result"]


def test_draft_visual_attaches_media_reference_with_provenance(client):
    res = _draft_visual(client, _export())
    assert res["status"] == "done" and res["staged_as_draft"] is True and res["visual_draft_staged"] is True
    assert res["media"] == [{"source": "vibexgen", "asset_id": "a" * 32, "kind": "image", "media_type": "image/png",
                             "bytes": 123, "sha256": "ab" * 32, "target": "instagram_post", "ai_generated": True}]
    assert len(_FakePsql.inserts) == 1 and "vibexgen" in _FakePsql.inserts[0] and "'DRAFT'" in _FakePsql.inserts[0]


@pytest.mark.parametrize("export, why", [
    (None, "no visual export"),
    ({"status": "invalid", "error": "too long"}, "too long"),
    (_export(validation={"ok": False, "problems": ["x"]}), "platform"),
    (_export(artifacts=[]), "exactly one"),
    (_export(ai_label="none"), "label"),
    (_export(ai_label=None), "label"),
])
def test_draft_visual_refuses_unusable_media_and_writes_nothing(client, export, why):
    res = _draft_visual(client, export)
    assert res["status"] == "refused" and why in res["error"]
    assert _FakePsql.inserts == []


def test_draft_visual_accepts_unlabelled_media_that_is_not_ai_generated(client):
    res = _draft_visual(client, _export(ai_generated=False, ai_label="none"))
    assert res["status"] == "done" and res["media"][0]["ai_generated"] is False


def test_draft_visual_takes_the_topic_from_a_missions_verified_fields(client):
    r = client.post("/invoke", json={"capability": "social.draft_visual",
                                     "inputs": {"intent": {"topic": "shipping on Friday"}, "visual_export": _export()}}).json()["result"]
    assert r["topic"] == "shipping on Friday"


def _run_one_step_mission(capability, outcome, inputs=None):
    from agentic_os.mission.executor import Executor
    from agentic_os.mission.operator_sdk import LocalOperatorClient
    from agentic_os.mission.registry import CapabilityRegistry
    from agentic_os.mission.runtime import MissionRuntime
    from agentic_os.mission.types import ExecutionIntent, IntentStep

    class Planner:
        def plan(self, mission_id, goal, context):
            return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome=outcome, need=goal)], bind={outcome: capability})
    op = build_social_operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"social-autopilot": op})), planner=Planner())
    m = rt.create_mission("draft a post", policy_refs=["*"], inputs=inputs or {})
    rt.run(m.id)
    return rt._missions[m.id]


def test_a_draft_passes_the_runtimes_verification_inside_a_mission():
    """The runtime rejects a side-effecting result that lacks the outputs the capability declares; run through a real mission, not just /invoke."""
    m = _run_one_step_mission("social.draft", "draft_staged")
    assert m.state.value == "succeeded", m.outcome
    assert len(_FakePsql.inserts) == 1
