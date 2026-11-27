"""Video, images and social posts for external agents, through the mounted Projects app: files belong to the tenant that
made them, a composed plan cannot reach another file or drop the AI label, and nothing reaches Postiz until a person
approved the exact file, channel and time. The media service and Postiz fakes speak their own HTTP routes, so the real
clients are what run."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.agent_gateway import media_capabilities as MC  # noqa: E402
from agentic_os.governed_writes import apps as apps_mod  # noqa: E402
from agentic_os.governed_writes import media as M  # noqa: E402
from agentic_os.mission.approval import verify_approval_token  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests.test_governed_writes import AMBIENT  # noqa: E402

BASE = "/api/agent-gateway/capabilities"
APPROVER = {"Authorization": "Bearer " + "A" * 40}
SECRET = "s" * 48
LIMITS = {"acme": {"policy_refs": ["app:vibexgen", "app:postiz", "writes:decide"]},
          "other": {"policy_refs": ["app:vibexgen", "app:postiz"]},
          "videoless": {"policy_refs": ["app:postiz", "writes:decide"]}}
ALL = ["media.read", "media.edit", "media.generate", "social.read", "social.propose"]


class Studio:
    """VibeXGen's operator app: an asset store with no tenants, lineage that carries the AI flag, export targets."""

    def __init__(self):
        self.files, self.meta, self.calls, self.keys, self.backend = {}, {}, [], {}, None
        self.tamper = False
        self.inference, self.downloaded_for = None, []      # what /health says about the models; whose downloads were asked
        self.on_health = None

    def add(self, data, ai, parents=()):
        aid = hashlib.md5(f"{len(self.files)}:{data!r}".encode()).hexdigest()
        self.files[aid], self.meta[aid] = data, {"ai": ai, "parents": list(parents)}
        return aid

    def ai(self, aid):
        return self.meta[aid]["ai"] or any(self.ai(p) for p in self.meta[aid]["parents"])

    def ref(self, aid):
        return {"asset": aid, "kind": "video", "media_type": "video/mp4", "bytes": len(self.files[aid]),
                "sha256": hashlib.sha256(self.files[aid]).hexdigest()}

    def handle(self, request):
        path = request.url.path
        if path == "/health":
            if self.on_health:
                self.on_health()
            facts = {} if self.inference is None else {"inference": {"video": self.inference, "image": self.inference}}
            return httpx.Response(200, json={"video_backend": self.backend, "image_backend": self.backend, "notices": [], **facts})
        if path.startswith("/assets/"):
            assert request.headers["authorization"] == "Bearer " + "d" * 40
            self.downloaded_for.append(request.headers.get("x-vibexgen-tenant"))
            data = self.files.get(path.split("/")[2])
            return httpx.Response(404) if data is None else httpx.Response(200, content=data + (b"x" if self.tamper else b""))
        assert path == "/invoke" and request.headers["authorization"] == "Bearer " + "c" * 40
        body = json.loads(request.content)
        cap, i, key = body["capability"], body["inputs"], body["idempotency_key"]
        self.calls.append((cap, i, body))
        if key in self.keys:
            return httpx.Response(200, json={"result": self.keys[key]})
        if cap == "visual.library":
            out = {"status": "ok", "items": [{"name": "film", "title": "Film", "ai_generated": True, "source": "test"}]}
        elif cap == "visual.import":
            out = {"status": "ok", "artifacts": [self.ref(self.add(b"FILM-" + i["item"].encode(), True))]}
        elif cap == "visual.compose":
            parents = list(i["inputs"].values())
            out = {"status": "ok", "artifacts": [self.ref(self.add(b"CUT" + json.dumps(i["plan"]).encode(), False, parents))]}
        elif cap == "visual.export":
            made = self.add(b"FINAL-" + i["target"].encode() + self.files[i["asset"]], False, [i["asset"]])
            out = {"status": "ok", "artifacts": [self.ref(made)], "ai_label": "burned" if self.ai(i["asset"]) else "not needed"}
        elif cap == "visual.inspect":
            out = {"status": "ok", "ai_generated": self.ai(i["asset"]), "media": {"duration": 30.0, "width": 1080, "height": 1920}}
        elif cap == "visual.generate":
            marker = verify_approval_token(SECRET, body.get("approval_token"), operator="vibexgen", capability="visual.generate", inputs=i,
                                           mission_id=body["mission_id"], node_id=body["node_id"])
            if not marker:
                return httpx.Response(200, json={"result": {"status": "pending_approval"}})
            self.approved_by = marker["by"]
            out = {"status": "ok", "artifacts": [self.ref(self.add(b"GEN" + i["prompt"].encode(), True))]}
        else:
            return httpx.Response(404)
        self.keys[key] = out
        return httpx.Response(200, json={"result": out})

    def client(self):
        return M.VibexgenOperator("http://studio.test", "c" * 40, "d" * 40, transport=httpx.MockTransport(self.handle))


class Social:
    """Postiz's public API."""

    def __init__(self):
        self.posts, self.uploads, self.fail, self.garble = [], [], {}, ""
        self.names_the_post, self.also = True, 0      # newer Postiz answers with the id; the old one with an empty 201
        self.channels = [{"id": "ig1", "name": "Brand IG", "identifier": "instagram", "disabled": False},
                         {"id": "li1", "name": "Brand LinkedIn", "identifier": "linkedin-page", "disabled": False},
                         {"id": "off1", "name": "Old", "identifier": "instagram", "disabled": True}]

    def handle(self, request):
        assert request.headers["authorization"] == "org-key"
        path = request.url.path.removeprefix("/public/v1")
        if path in self.fail and request.method == "POST":
            return httpx.Response(self.fail[path], json={})
        if path == "/integrations":
            return httpx.Response(200, json=self.channels)
        if path == "/upload":
            data = request.read()
            self.uploads.append(data)
            return httpx.Response(201, json={"id": f"m{len(self.uploads)}", "path": "http://postiz.test/uploads/x.mp4"})
        if path == "/posts" and request.method == "GET":
            return httpx.Response(200, json={"posts": self.posts})
        body = json.loads(request.content)
        post = body["posts"][0]
        self.posts.append({"id": f"p{len(self.posts) + 1}", "state": {"schedule": "QUEUE", "draft": "DRAFT"}[body["type"]],
                           "publishDate": body["date"], "integration": {"id": post["integration"]["id"]},
                           "content": self.garble or post["value"][0]["content"], "image": post["value"][0]["image"]})
        for _ in range(self.also):                    # someone else's identical post appears at the same moment
            self.posts.append({**self.posts[-1], "id": f"p{len(self.posts) + 1}"})
        if not self.names_the_post:
            return httpx.Response(201, content=b"")
        return httpx.Response(201, json=[{"postId": self.posts[-1]["id"], "integration": post["integration"]["id"]}])

    def client(self):
        return M.PostizClient("http://postiz.test/public/v1", "org-key", transport=httpx.MockTransport(self.handle))


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB"):
        monkeypatch.delenv(k, raising=False)
    studio, social = Studio(), Social()
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: {"vibexgen": studio.client(), "postiz": social.client()})
    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<html></html>")
    for k, v in {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
                 "AGENTIC_OS_APPROVAL_SECRET": SECRET, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
                 "AGENTIC_OS_APPROVER_TOKENS": "acme=" + "A" * 40, "PROJECTS_UI_DIR": str(tmp_path / "ui")}.items():
        monkeypatch.setenv(k, v)
    client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    count = iter(range(100))

    def agent(grants=ALL, tenant="acme"):
        return {"Authorization": "Bearer " + AT.issue(agents, f"m{next(count)}", grants, minutes=10, tenant=tenant, authority_db=db,
                                                      rate_per_minute=600)}

    who = agent()
    keys = iter(range(1000))

    def call(tool, /, as_=None, **args):
        if tool in ("media.import", "media.compose", "media.export", "media.request_generate", "social.request_post"):
            args.setdefault("idempotency_key", f"key-{next(keys):06d}")
        return client.post(f"{BASE}/{tool}/invoke", headers=as_ or who, json=args).json()

    def ok(tool, /, **args):
        out = call(tool, **args)
        assert out["status"] == "ok", (tool, out)
        return out["output"]

    def decide(asked, approved=True):
        shown = client.get(f"/api/writes/{asked['request_id']}", headers=APPROVER).json()
        return client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER,
                           json={"approved": approved, "digest": shown["digest"]}).json()

    def reel():
        src = ok("media.import", item="film")["asset"]
        cut = ok("media.compose", inputs={"film": src}, steps=[{"id": "s1", "tool": "trim", "params": {"input": "$film", "start": 0, "end": 30}}])["asset"]
        return ok("media.export", asset=cut, target="instagram_reel")

    return type("W", (), {"client": client, "studio": studio, "social": social, "agent": staticmethod(agent), "call": staticmethod(call),
                          "ok": staticmethod(ok), "decide": staticmethod(decide), "reel": staticmethod(reel), "who": who})


def later(hours=48):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:00Z")


def names(w, who):
    return {c["name"] for c in w.client.get(BASE, headers=who).json()["capabilities"] if c["name"].split(".")[0] in ("media", "social")}


# ── the whole path ───────────────────────────────────────────────────────────────
def test_import_edit_export_then_a_post_scheduled_only_after_a_person_approved_those_bytes(world):
    w = world
    assert w.ok("media.library")["items"][0]["name"] == "film"
    final = w.reel()
    assert final["ai_generated"] is True and final["ai_label"] == "burned" and final["target"] == "instagram_reel" and final["how"] == "export"
    assert re.fullmatch(M.PUBLIC_ASSET, final["asset"]) and "sha256" not in final and "«" not in json.dumps(final)
    # the media service is told whose each call is, so it can hold tenants' files apart itself
    assert w.studio.calls and all(i.get("_tenant") == "acme" for _, i, _ in w.studio.calls), [(c, i.get("_tenant")) for c, i, _ in w.studio.calls]
    raw = M.asset_id(final["asset"])
    sha = hashlib.sha256(w.studio.files[raw]).hexdigest()
    assert w.ok("social.channels")["channels"][0]["id"] == "ig1"
    when = later()
    asked = w.ok("social.request_post", channel_id="ig1", caption="Meet the roos.", when=when, asset=final["asset"])
    assert asked["state"] == "pending" and not w.social.posts and not w.social.uploads and "sha256" not in json.dumps(asked)
    shown = w.client.get(f"/api/writes/{asked['request_id']}", headers=APPROVER).json()
    ev = shown["evidence"]
    assert ev["file"]["sha256"] == sha and ev["file"]["ai_generated"] is True and ev["file"]["sources"] == ["library item film"]
    text = json.dumps(ev["show"])
    assert sha in text and "Brand IG" in text and "burned the label" in text and asked["summary"].startswith("Schedule a post on Brand IG")
    done = w.decide(asked)
    assert done["state"] == "executed_verified", done
    (post,) = w.social.posts
    assert (post["state"], post["integration"]["id"], post["content"], post["publishDate"][:16]) == ("QUEUE", "ig1", "Meet the roos.", when[:16])
    assert post["image"] == [{"id": "m1", "path": "http://postiz.test/uploads/x.mp4"}] and w.studio.files[raw] in w.social.uploads[0]
    assert w.studio.downloaded_for == ["acme"]                       # the download names whose file it is
    status = w.ok("social.request_status", request_id=asked["request_id"])
    assert status["state"] == "executed_verified" and status["record"] == "p1" and status["decided_by"] == "approver:acme"
    assert w.ok("social.posts", month=int(when[5:7]), year=int(when[:4]))["posts"][0]["state"] == "QUEUE"


def test_a_draft_needs_approval_too_and_rejecting_leaves_postiz_untouched(world):
    w = world
    asked = w.ok("social.request_post", channel_id="li1", caption="Notes", when="draft")
    assert not w.social.posts
    assert w.decide(asked, approved=False)["state"] == "rejected" and not w.social.posts
    done = w.decide(w.ok("social.request_post", channel_id="li1", caption="Notes", when="draft"))
    assert done["state"] == "executed_verified" and w.social.posts[0]["state"] == "DRAFT", done


@pytest.mark.parametrize("change,why", [
    (dict(when="now"), "must be 'draft' or an ISO 8601"),
    (dict(when=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()), "at least 15 minutes ahead"),
    (dict(when="2020-01-01T00:00:00Z"), "at least 15 minutes ahead"),
    (dict(when="2027-01-01T10:00:00"), "needs a time zone"),
    (dict(channel_id="nope"), "no such channel"),
    (dict(channel_id="off1"), "disabled"),
    (dict(channel_id="li1"), "not for a linkedin channel"),
])
def test_what_cannot_be_posted_is_refused_at_the_proposal(world, change, why):
    w = world
    final = w.reel()
    out = w.call("social.request_post", **{"channel_id": "ig1", "caption": "x", "when": later(), "asset": final["asset"], **change})
    assert out["status"] == "error" and re.search(why, out["error"]), out
    assert not w.social.posts and not w.client.get("/api/writes", headers=APPROVER).json()["requests"]


def test_only_an_exported_file_can_be_posted(world):
    w = world
    src = w.ok("media.import", item="film")["asset"]
    out = w.call("social.request_post", channel_id="ig1", caption="x", when="draft", asset=src)
    assert out["status"] == "error" and "only a file made by media.export" in out["error"] and not w.social.uploads


@pytest.mark.parametrize("change", [
    lambda w, f: setattr(w.studio, "tamper", True),                                   # the bytes are no longer the approved ones
    lambda w, f: w.studio.files.__setitem__(M.asset_id(f["asset"]), b"another cut"),
    lambda w, f: w.social.channels[0].update(name="Someone else's IG"),
    lambda w, f: w.social.channels[0].update(disabled=True),
    lambda w, f: w.social.fail.update({"/upload": 500}),
])
def test_what_changed_after_the_approval_card_stops_the_post(world, change):
    w = world
    final = w.reel()
    asked = w.ok("social.request_post", channel_id="ig1", caption="x", when=later(), asset=final["asset"])
    change(w, final)
    out = w.decide(asked)
    assert out["state"] == "failed" and "nothing was changed" in out["note"] and not w.social.posts, out


def test_an_unconfirmed_post_is_unknown_and_not_retried_and_a_different_read_back_is_not_verified(world):
    w = world
    w.social.fail = {"/posts": 500}
    asked = w.ok("social.request_post", channel_id="ig1", caption="Original", when="draft")
    out = w.decide(asked)
    assert out["state"] == "outcome_unknown", out
    again = w.client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER, json={"approved": True, "digest": "0" * 64})
    assert again.status_code == 409
    w.social.fail, w.social.garble = {}, "something else"
    out = w.decide(w.ok("social.request_post", channel_id="ig1", caption="Original", when="draft"))
    assert out["state"] == "executed_unverified" and "text differs" in out["note"] + out["read_back"], out


def test_a_postiz_that_does_not_name_the_post_is_read_back_to_the_one_new_entry_that_is_it(world):
    w = world
    w.social.names_the_post = False
    first = w.decide(w.ok("social.request_post", channel_id="ig1", caption="Same words", when="draft"))
    assert first["state"] == "executed_verified" and first["record"] == "p1", first
    second = w.decide(w.ok("social.request_post", channel_id="ig1", caption="Same words", when="draft"))
    assert second["state"] == "executed_verified" and second["record"] == "p2", second            # the earlier twin is not taken for it
    w.social.also = 1
    twin = w.decide(w.ok("social.request_post", channel_id="ig1", caption="Twice", when="draft"))
    assert twin["state"] == "outcome_unknown" and "2 new calendar entries" in twin["note"], twin
    w.social.also, w.social.garble = 0, "not what was sent"
    lost = w.decide(w.ok("social.request_post", channel_id="ig1", caption="Original", when="draft"))
    assert lost["state"] == "outcome_unknown" and "0 new calendar entries" in lost["note"], lost


# ── plans ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("step", [
    {"id": "s1", "tool": "export", "params": {"input": "$film", "target": "instagram_reel", "ai_label": "none"}},
    {"id": "s1", "tool": "trim", "params": {"input": "asset:" + "0" * 32, "start": 0, "end": 1}},
    {"id": "s1", "tool": "trim", "params": {"input": "asset:00000000-0000-0000-0000-000000000000", "start": 0, "end": 1}},
    {"id": "s1", "tool": "trim", "params": {"input": "0" * 32, "start": 0, "end": 1}},
    {"id": "s1", "tool": "concat", "params": {"inputs": ["$film", "asset:" + "0" * 32]}},
    {"id": "s1", "tool": "add_audio", "params": {"input": "$film", "audio": "/etc/passwd"}},
    {"id": "s1", "tool": "trim", "params": {"input": "$other", "start": 0, "end": 1}},
    {"id": "s1", "tool": "trim", "params": {"input": "@s9", "start": 0, "end": 1}},
    {"id": "s1", "tool": "t2v", "params": {"prompt": "x"}},
    {"id": "s1", "tool": "trim", "params": {"input": "$film", "start": 0, "end": 1, "ai_label": "none"}},
    {"id": "s1", "tool": "trim", "params": {"input": "$film", "start": "0", "end": 1}},
    {"id": "s1", "tool": "trim", "params": {"input": "$film", "start": 0, "end": 1}, "depends_on": ["x"]},
])
def test_a_plan_can_only_use_the_listed_steps_on_the_tenants_own_files(world, step):
    w = world
    src = w.ok("media.import", item="film")["asset"]
    before = len(w.studio.calls)
    out = w.call("media.compose", inputs={"film": src}, steps=[step])
    assert out["status"] == "error" and "invalid_arguments" in out["error"], out
    assert len(w.studio.calls) == before                                              # nothing was sent


def test_one_tenant_cannot_use_see_or_post_anothers_file_even_by_the_same_key(world):
    w = world
    mine = w.reel()["asset"]
    other = w.agent(tenant="other")
    for tool, args in (("media.inspect", {"asset": mine}), ("media.export", {"asset": mine, "target": "square_video", "idempotency_key": "o-1-000000"}),
                       ("media.compose", {"inputs": {"a": mine}, "steps": [{"id": "s1", "tool": "reverse", "params": {"input": "$a"}}],
                                          "idempotency_key": "o-2-000000"})):
        out = w.call(tool, as_=other, **args)
        assert out["status"] == "error" and "of yours" in out["error"], (tool, out)
    assert w.call("media.assets", as_=other)["output"]["assets"] == []
    posted = w.call("social.request_post", as_=other, channel_id="ig1", caption="x", when="draft", asset=mine, idempotency_key="o-3-000000")
    assert posted["status"] == "error", posted
    a = w.call("media.import", item="film", idempotency_key="same-key-0001")["output"]["asset"]
    b = w.call("media.import", as_=other, item="film", idempotency_key="same-key-0001")["output"]["asset"]
    assert a != b                                                                     # the operator's cache is not shared across callers
    again = w.call("media.import", item="film", idempotency_key="same-key-0001")["output"]["asset"]
    assert again == a                                                                 # but a retry is the same file


# ── generation ───────────────────────────────────────────────────────────────────
def test_generation_is_refused_without_a_model_and_otherwise_runs_only_with_the_persons_approval_token(world):
    w = world
    out = w.call("media.request_generate", kind="video", prompt="a roo in a beret")
    assert out["status"] == "error" and "no video generation model is configured" in out["error"], out
    w.studio.backend = "local-model"
    asked = w.ok("media.request_generate", kind="video", prompt="a roo in a beret")
    assert asked["state"] == "pending" and not [c for c in w.studio.calls if c[0] == "visual.generate"]
    done = w.decide(asked)
    assert done["state"] == "executed_verified" and w.studio.approved_by == "approver:acme", done
    mine = w.ok("media.assets")["assets"][0]
    assert mine["asset"] == done["record"] and re.fullmatch(M.PUBLIC_ASSET, done["record"]) and mine["how"] == "generate" and mine["ai_generated"] is True
    assert w.ok("media.request_status", request_id=asked["request_id"])["state"] == "executed_verified"


# ── who gets what ────────────────────────────────────────────────────────────────
def test_each_tool_needs_its_grant_and_the_tenants_application(world):
    w = world
    assert names(w, w.agent(["media.read"])) == {"media.library", "media.assets", "media.inspect"}
    assert names(w, w.agent(["social.read"])) == {"social.channels", "social.posts"}
    assert names(w, w.agent(["projects.read"])) == set()
    assert {"media.compose", "social.request_post", "media.request_generate"} <= names(w, w.who)
    reader = w.agent(["media.read", "social.read"])
    for tool, args in (("media.import", {"item": "film"}), ("social.request_post", {"channel_id": "ig1", "caption": "x", "when": "draft"})):
        assert w.call(tool, as_=reader, **args)["status"] != "ok"
    out = w.call("media.library", as_=w.agent(tenant="videoless"))
    assert out["status"] == "error" and "tenant_not_granted" in out["error"], out


def test_a_tenant_with_no_approver_may_edit_but_not_propose_a_post(world):
    w = world
    who = w.agent(tenant="other")
    assert w.call("media.import", as_=who, item="film", idempotency_key="edit-no-approver-1")["status"] == "ok"
    out = w.call("social.request_post", as_=who, channel_id="ig1", caption="x", when="draft", idempotency_key="post-no-approver-1")
    assert out["status"] == "error" and "no_approver" in out["error"], out


def test_the_step_list_is_closed_and_has_no_export_or_model_step():
    assert "export" not in MC.STEPS and not {"t2i", "i2i_edit", "t2v", "i2v", "fl2v", "ref2v", "auto_subtitles"} & set(MC.STEPS)
    assert all(not {"ai_label", "target"} & {*refs, *scalars} for refs, scalars, _ in MC.STEPS.values())
