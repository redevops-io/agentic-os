"""Signed content URLs: expiry, tamper-evidence, and the route that serves the editor."""
from __future__ import annotations

import pytest


# ── content registry / serving ───────────────────────────────────────────────────────────────────────────
def test_content_tokens_expire_and_cannot_be_tampered(mods):
    c = mods.content.ContentRegistry("http://c", secret="k")
    url = c.issue(kind="document", tenant="acme", doc_id="a.docx", version=1, sha256="a" * 64, backend="onlyoffice",
                  ttl_s=9999, now=1000)
    tok = url.rsplit("/", 1)[1]
    assert c.verify(tok, now=1000 + 299)["d"] == "a.docx"                   # ttl capped at 5 minutes
    with pytest.raises(mods.content.ContentTokenError):
        c.verify(tok, now=1000 + 301)
    p, s = tok.split(".")
    for bad in (p + "." + "A" * len(s), "x" + tok, tok + "x", "a.b", "", p):
        with pytest.raises(mods.content.ContentTokenError):
            c.verify(bad, now=1001)
    other = mods.content.ContentRegistry("http://c", secret="other")
    with pytest.raises(mods.content.ContentTokenError):
        other.verify(tok, now=1001)


def test_content_route_serves_only_the_bound_bytes(env, kit):
    import importlib
    from fastapi.testclient import TestClient
    appmod = importlib.import_module("document-editor.app")
    appmod.holder._svc = env.svc
    try:
        env.put("acme", "a.docx", kit.make_docx(["served"]), label="PUBLIC")
        ref_, data = env.svc._ref("acme", "a.docx", with_url=True)
        tok = ref_.content_url.rsplit("/", 1)[1]
        c = TestClient(appmod.app)
        r = c.get(f"/content/{tok}")
        assert r.status_code == 200 and r.content == data and r.headers["cache-control"] == "no-store"
        assert c.get("/content/garbage").status_code == 403
        assert c.get(f"/content/{tok}x").status_code == 403
        # the document changes after the token was issued: the old token no longer matches the bytes it was bound to
        (env.root / "acme" / "a.docx").write_bytes(kit.make_docx(["changed"]))
        env.store.put_version("acme", "a.docx", b"v2", expected_version=env.store.head("acme", "a.docx").version, actor="x", kind="edit")
        assert c.get(f"/content/{tok}").content == data              # the issued VERSION is immutable
    finally:
        appmod.holder._svc = None
