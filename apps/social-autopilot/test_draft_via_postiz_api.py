"""social.draft through Postiz's public API: plain-text caption, media fetched from Vibexgen, verified and uploaded.

Unit tests fake the two HTTP boundaries (Postiz, Vibexgen). The live test at the end runs against a real
Postiz and a real Vibexgen when SOCIAL_LIVE_POSTIZ_URL / _KEY and SOCIAL_LIVE_VIBEXGEN_URL / _ASSET are set."""
from __future__ import annotations

import hashlib
import importlib
import json
import os

import httpx
import pytest

core = importlib.import_module("social-autopilot.core")

FILE = b"\x00\x00\x00\x18ftypmp42 a small stand-in for an exported video"
REF = {"source": "vibexgen", "asset_id": "a" * 32, "kind": "video", "media_type": "video/mp4", "bytes": len(FILE),
       "sha256": hashlib.sha256(FILE).hexdigest(), "target": "instagram_reel", "ai_generated": False}
EXPORT = {"status": "ok", "target": "instagram_reel", "ai_generated": False, "validation": {"ok": True, "problems": []},
          "artifacts": [{"asset": REF["asset_id"], "kind": "video", "media_type": "video/mp4", "bytes": REF["bytes"], "sha256": REF["sha256"]}]}


class Wire:
    """Records every request and plays Postiz and Vibexgen."""

    def __init__(self, *, file=FILE, channels=None, fail=None):
        self.calls, self.file, self.fail = [], file, fail or {}
        self.channels = [{"id": "int-1", "identifier": "instagram", "disabled": False}] if channels is None else channels

    def _resp(self, status, body=None, content=b""):
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, content=content)

    def request(self, method, url, headers=None, timeout=None, **kw):
        path = url.split("/api/public/v1", 1)[1]
        self.calls.append((method, path, kw))
        assert headers == {"Authorization": "org-key"}
        if path in self.fail:
            return self._resp(self.fail[path])
        if path == "/integrations":
            return self._resp(200, self.channels)
        if path == "/upload":
            return self._resp(201, {"id": "media-1", "path": "http://postiz.test/uploads/x.mp4"})
        return self._resp(201, [{"postId": "cpost1", "integration": "int-1"}])

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, {"headers": headers}))
        return self._resp(self.fail.get("vibexgen", 200), content=self.file)


@pytest.fixture
def wire(monkeypatch):
    def _make(**kw):
        w = Wire(**kw)
        monkeypatch.setattr(core.httpx, "request", w.request)
        monkeypatch.setattr(core.httpx, "get", w.get)
        monkeypatch.setattr(core, "POSTIZ_API_KEY", "org-key")
        monkeypatch.setattr(core, "POSTIZ_API_URL", "http://postiz.test")
        monkeypatch.setattr(core, "VIBEXGEN_API_URL", "http://vibexgen.test")
        monkeypatch.setattr(core, "PG_PASSWORD", "a-real-secret")
        monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: pytest.fail("the API path must not touch psql"))
        return w
    return _make


def _posts(w):
    return [c for c in w.calls if c[0] == "POST" and c[1] == "/posts"]


def test_the_draft_is_plain_text_with_the_uploaded_file_attached(wire):
    w = wire()
    res = core.draft_visual({"topic": "pricing moved", "visual_export": EXPORT})
    assert res["status"] == "done" and res["staged_via"] == "postiz_api" and res["post_id"] == "cpost1" and res["visual_draft_staged"] is True
    assert [c[1] for c in w.calls] == ["/integrations", "http://vibexgen.test/api/assets/" + "a" * 32 + "/download", "/upload", "/posts"]
    body = _posts(w)[0][2]["json"]
    assert body["type"] == "draft"
    value = body["posts"][0]["value"][0]
    assert value["content"] == res["copy"] and not value["content"].startswith("[")      # a caption, not a JSON blob
    assert value["image"] == [{"id": "media-1", "path": "http://postiz.test/uploads/x.mp4"}]
    assert body["posts"][0]["settings"] == {"__type": "instagram", "post_type": "post"}
    assert w.calls[2][2]["files"]["file"][1] == FILE                                       # exactly the verified bytes
    assert res["postiz_media"][0]["sha256"] == REF["sha256"]


def test_a_text_only_draft_uses_the_api_and_fetches_nothing(wire):
    w = wire()
    res = core.draft({"topic": "year-end"})
    assert res["status"] == "done" and res["postiz_media"] == []
    assert [c[1] for c in w.calls] == ["/integrations", "/posts"]


@pytest.mark.parametrize("setup,why", [
    (dict(file=FILE + b"x"), "does not match"),                    # a different size
    (dict(file=FILE[::-1]), "does not match"),                     # same size, different content
    (dict(fail={"vibexgen": 404}), "HTTP 404 for the exported file"),
    (dict(channels=[]), "no connected channel"),
    (dict(channels=[{"id": "x", "identifier": "instagram", "disabled": True}]), "no connected channel"),
    (dict(fail={"/integrations": 401}), "HTTP 401"),
])
def test_nothing_reaches_postiz_when_the_media_or_channel_is_wrong(wire, setup, why):
    w = wire(**setup)
    res = core.draft_visual({"topic": "t", "visual_export": EXPORT})
    assert res["status"] == "error" and why in res["error"]
    assert not [c for c in w.calls if c[0] == "POST"]                                     # no upload, no draft


def test_media_without_a_vibexgen_address_is_refused_not_dropped(wire, monkeypatch):
    w = wire()
    monkeypatch.setattr(core, "VIBEXGEN_API_URL", "")
    res = core.draft_visual({"topic": "t", "visual_export": EXPORT})
    assert res["status"] == "error" and "VIBEXGEN_API_URL is not set" in res["error"] and _posts(w) == []


def test_a_failed_upload_creates_no_draft_and_the_key_is_never_in_the_error(wire):
    w = wire(fail={"/upload": 500})
    res = core.draft_visual({"topic": "t", "visual_export": EXPORT})
    assert res["status"] == "error" and "HTTP 500 on POST /upload" in res["error"] and "org-key" not in json.dumps(res)
    assert _posts(w) == []


def test_without_an_api_key_the_database_path_is_unchanged(monkeypatch):
    seen = []

    class Done:
        returncode, stderr = 0, ""
        def __init__(self, out): self.stdout = out

    def run(args, **kw):
        seen.append(args[-1])
        return Done("int-1\x1finstagram\n" if "LIMIT 1" in args[-1] else "")
    monkeypatch.setattr(core, "POSTIZ_API_KEY", "")
    monkeypatch.setattr(core, "PG_PASSWORD", "a-real-secret")
    monkeypatch.setattr(core.subprocess, "run", run)
    monkeypatch.setattr(core.httpx, "request", lambda *a, **k: pytest.fail("no API call without a key"))
    res = core.draft({"topic": "year-end"})
    assert res["status"] == "done" and "staged_via" not in res and any('INSERT INTO "Post"' in s for s in seen)


LIVE = {k: os.environ.get(k, "") for k in ("SOCIAL_LIVE_POSTIZ_URL", "SOCIAL_LIVE_POSTIZ_KEY", "SOCIAL_LIVE_VIBEXGEN_URL", "SOCIAL_LIVE_VIBEXGEN_ASSET")}


@pytest.mark.skipif(not all(LIVE.values()), reason="set SOCIAL_LIVE_POSTIZ_URL/_KEY and SOCIAL_LIVE_VIBEXGEN_URL/_ASSET (opt-in live test; creates a draft)")
def test_live_a_real_export_becomes_a_real_postiz_draft_with_its_video(monkeypatch):
    monkeypatch.setattr(core, "POSTIZ_API_KEY", LIVE["SOCIAL_LIVE_POSTIZ_KEY"])
    monkeypatch.setattr(core, "POSTIZ_API_URL", LIVE["SOCIAL_LIVE_POSTIZ_URL"].rstrip("/"))
    monkeypatch.setattr(core, "VIBEXGEN_API_URL", LIVE["SOCIAL_LIVE_VIBEXGEN_URL"].rstrip("/"))
    monkeypatch.setattr(core, "PG_PASSWORD", "not-the-default")
    aid = LIVE["SOCIAL_LIVE_VIBEXGEN_ASSET"]
    data = httpx.get(f"{core.VIBEXGEN_API_URL}/api/assets/{aid}/download", timeout=60).content
    export = {**EXPORT, "artifacts": [{"asset": aid, "kind": "video", "media_type": "video/mp4", "bytes": len(data),
                                       "sha256": hashlib.sha256(data).hexdigest()}]}
    res = core.draft_visual({"topic": "live cross-app check", "visual_export": export})
    assert res["status"] == "done" and res["staged_via"] == "postiz_api", res
    h = {"Authorization": core.POSTIZ_API_KEY}
    posts = httpx.get(f"{core.POSTIZ_API_URL}/api/public/v1/posts", headers=h, timeout=30,
                      params={"startDate": "2000-01-01T00:00:00.000Z", "endDate": "2100-01-01T00:00:00.000Z"}).json()["posts"]
    mine = next(p for p in posts if p["id"] == res["post_id"])
    assert mine["state"] == "DRAFT" and mine["content"] == res["copy"]
    served = httpx.get(res["postiz_media"][0]["path"], timeout=60)
    assert served.status_code == 200 and hashlib.sha256(served.content).hexdigest() == export["artifacts"][0]["sha256"]
    tampered = {**export, "artifacts": [{**export["artifacts"][0], "sha256": "0" * 64}]}
    bad = core.draft_visual({"topic": "tampered", "visual_export": tampered})
    assert bad["status"] == "error" and "does not match" in bad["error"]
    after = httpx.get(f"{core.POSTIZ_API_URL}/api/public/v1/posts", headers=h, timeout=30,
                      params={"startDate": "2000-01-01T00:00:00.000Z", "endDate": "2100-01-01T00:00:00.000Z"}).json()["posts"]
    assert len(after) == len(posts)
